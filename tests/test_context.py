"""build_context (two memories) and decide() (preferences break Energia/Conforto ties)."""

import json
import tempfile
import time
import unittest
from pathlib import Path

from src.agents.specs import AgentReply, CoordinatorReply, Proposal, Verdict
from src.config import get_settings
from src.context import Memories, build_context, render_context
from src.coordinator import Coordinator
from src.memory import SharedMemory
from src.perception.state import Event, normalize
from src.sim.home import SimHome

SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
TEMP = "54e1ea8e-9ca3-4933-a4dc-a1eb37e6990c"


def approve_all(prompt: str) -> CoordinatorReply:
    block = prompt.split("Propostas a decidir:\n")[1].split("\n\n")[0]
    n = len(block.splitlines())
    return CoordinatorReply(decisoes=[Verdict(n=i, aprovar=True, motivo="ok") for i in range(1, n + 1)])


class MemCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json",
            "history_db": tmp / "home.db", "dry_run": True})
        self.home = SimHome(self.s.sim_state_file)
        self.home.reset()
        self.mem = Memories.open(self.s.history_db)
        self.memory = SharedMemory()
        self.refresh()

    def refresh(self):
        self.state = normalize(self.home.snapshot(), self.s)
        self.memory.update_state(self.state)
        return self.state

    def tearDown(self):
        self.tmp.cleanup()


class TestBuildContext(MemCase):
    def test_filters_by_room_and_device(self):
        now = time.time()
        rows = []
        for i in range(8):
            rows.append((now - 7000 + i * 900, QUARTO, TEMP, "temperature", 20.0 + i * 0.1))
            rows.append((now - 7000 + i * 900, SALA, "c1", "open", 0.0))
        self.mem.history.insert(rows)
        p_room = self.mem.prefs.set(f"room:{QUARTO}", "humidity_max_pct", {"pct": 65}, original_text="no máximo 65 %")
        p_dev = self.mem.prefs.set(f"device:{PLUG}", "note", {"text": "aquecedor antigo"})
        p_glob = self.mem.prefs.set("global", "quiet_hours", {"start": "23:00", "end": "07:00"})
        p_other = self.mem.prefs.set(f"room:{SALA}", "note", {"text": "sala"})
        self.mem.decisions.add(trigger={"type": "humidity_changed"}, proposals=[], decision={},
                               explanation="humidade alta", status="pending", room=QUARTO)
        trig = Event(type="humidity_changed", message="Humidade no Quarto: 60% → 72%.", room=QUARTO,
                     room_name="Quarto", device_id=TEMP)
        ctx = build_context("conforto", trig, self.state, self.mem, now=now)
        ids = {p["id"] for p in ctx["preferencias"]}
        self.assertEqual(ids, {p_room["id"], p_dev["id"], p_glob["id"]})
        self.assertNotIn(p_other["id"], ids)
        self.assertIn("Quarto", ctx["dados"]["divisoes"])
        self.assertNotIn("Sala", ctx["dados"]["divisoes"])
        self.assertTrue(ctx["dados"]["divisoes"]["Quarto"]["tendencia_2h_15min"]["temperature"])
        self.assertEqual(ctx["decisoes_semelhantes"][0]["utilizador"], "sem resposta")
        self.assertIn("ontem_kwh_casa", ctx["dados"]["energia"])
        self.assertLess(len(render_context(ctx)), 2500)  # compact: the LLM may be local
        json.loads(render_context(ctx))


class TestDecide(MemCase):
    def coordinator(self):
        c = Coordinator(self.memory, self.s, sim=self.home, arbiter=approve_all, memories=self.mem)
        return c

    def test_setpoint_preference_settles_energia_vs_conforto(self):
        self.home.set_temperature(18.0)
        self.home.set_plug(True)
        self.refresh()
        sp = self.mem.prefs.set(f"room:{QUARTO}", "setpoint_c", {"c": 21}, original_text="quarto a 21")
        c = self.coordinator()
        energia = c.decide("Energia", Proposal(device_id=PLUG, acao="off", motivo="poupar"), self.state.devices[PLUG], self.state)
        self.assertTrue(energia.ok)
        self.assertEqual([r["id"] for r in energia.conflicts], [sp["id"]])
        conforto = c.decide("Conforto", Proposal(device_id=PLUG, acao="on", motivo="frio"), self.state.devices[PLUG], self.state)
        self.assertTrue(conforto.ok)  # lower priority, but respects the preference
        self.assertTrue(conforto.override)
        self.assertIn(sp["id"], " ".join(conforto.notes))

    def test_without_preferences_priority_wins(self):
        self.home.set_plug(True)
        self.refresh()
        c = self.coordinator()
        c.decide("Energia", Proposal(device_id=PLUG, acao="off"), self.state.devices[PLUG], self.state)
        conforto = c.decide("Conforto", Proposal(device_id=PLUG, acao="on"), self.state.devices[PLUG], self.state)
        self.assertFalse(conforto.ok)
        self.assertIn("prioridade", " ".join(conforto.notes))

    def test_invented_preference_ids_are_ignored(self):
        self.home.set_plug(True)
        self.refresh()
        c = self.coordinator()
        v = c.decide("Energia", Proposal(device_id=PLUG, acao="off", conflicts_with_preferences=["inventado"]),
                     self.state.devices[PLUG], self.state)
        self.assertEqual(v.conflicts, [])

    def test_proposal_against_preference_is_never_automatic(self):
        self.home.set_temperature(18.0)
        self.home.set_plug(True)
        self.refresh()
        self.mem.prefs.set(f"room:{QUARTO}", "setpoint_c", {"c": 21})
        self.mem.prefs.set("global", "autonomy", {"category": "energia", "level": "auto"})
        c = self.coordinator()
        c.consider("Energia", AgentReply(resumo="Janela aberta.", propostas=[Proposal(device_id=PLUG, acao="off")]))
        [d] = self.mem.decisions.recent()
        self.assertEqual(d["status"], "pending")
        self.assertIn("Vai contra a tua preferência", d["explanation"])


if __name__ == "__main__":
    unittest.main()
