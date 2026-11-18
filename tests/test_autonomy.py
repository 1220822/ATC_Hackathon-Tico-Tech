"""Autonomy levels per category and the safety veto above every preference."""

import tempfile
import unittest
from pathlib import Path

from src.agents.specs import AgentReply, CoordinatorReply, Proposal, Verdict
from src.config import get_settings
from src.context import Memories
from src.coordinator import Coordinator, RequestRefused
from src.memory import SharedMemory
from src.perception.state import Event, normalize
from src.sim.home import SimHome

PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
LIGHT = "b667d87c-2566-440a-afce-02d9ec82df1b"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"


def approve(prompt):
    return CoordinatorReply(decisoes=[Verdict(n=1, aprovar=True, motivo="janela aberta")])


class AutonomyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json",
            "history_db": tmp / "home.db", "dry_run": False, "simulate": True})
        self.home = SimHome(self.s.sim_state_file)
        self.home.reset()
        self.home.set_contact(True)
        self.home.set_plug(True)
        self.memory = SharedMemory()
        self.mem = Memories.open(self.s.history_db)
        self.coord = Coordinator(self.memory, self.s, sim=self.home, arbiter=approve, memories=self.mem)
        self.refresh()

    def refresh(self):
        self.memory.update_state(normalize(self.home.snapshot(), self.s))

    def tearDown(self):
        self.tmp.cleanup()

    def propose_off(self):
        self.coord.consider("Energia", AgentReply(resumo="Janela aberta com o aquecedor ligado.",
                                                  propostas=[Proposal(device_id=PLUG, acao="off", motivo="desperdício")]),
                            [Event(type="contact_opened", message="Janela da Sala aberta.")])
        return self.mem.decisions.recent()[0]

    def plug_on(self) -> bool:
        return self.home.snapshot()["home"]["rooms"][1]["devices"][1]["capabilities"][4]["value"] == "on"


class TestLevels(AutonomyCase):
    def plug_power(self):
        return normalize(self.home.snapshot(), self.s).cap("plug", "power")

    def test_default_is_ask(self):
        d = self.propose_off()
        self.assertEqual((d["status"], d["autonomy"]), ("pending", "perguntar"))
        self.assertEqual(self.plug_power(), "on")  # nothing happened in the house

    def test_auto_executes(self):
        self.mem.prefs.set("global", "autonomy", {"category": "energia", "level": "auto"})
        d = self.propose_off()
        self.assertEqual((d["status"], d["autonomy"]), ("executed", "auto"))
        self.assertEqual(self.plug_power(), "off")

    def test_auto_respects_dry_run(self):
        self.coord.s = self.s.model_copy(update={"dry_run": True})
        self.mem.prefs.set("global", "autonomy", {"category": "energia", "level": "auto"})
        self.assertEqual(self.propose_off()["status"], "dry_run")
        self.assertEqual(self.plug_power(), "on")

    def test_never_only_records(self):
        self.mem.prefs.set("global", "autonomy", {"category": "energia", "level": "nunca"})
        d = self.propose_off()
        self.assertEqual((d["status"], d["autonomy"]), ("rejected", "nunca"))
        self.assertIn("nunca", d["explanation"])
        self.assertEqual(self.plug_power(), "on")

    def test_category_never_beats_learned_auto(self):
        kind = f"contact_opened|{PLUG}|power=off"
        self.mem.prefs.set("global", "autonomy", {"category": "energia", "level": "auto", "kind": kind},
                           source="learned_confirmed")
        self.assertEqual(self.mem.prefs.autonomy("energia", kind), "auto")
        self.assertEqual(self.mem.prefs.autonomy("energia", "outra"), "perguntar")
        self.mem.prefs.set("global", "autonomy", {"category": "energia", "level": "nunca"})
        self.assertEqual(self.mem.prefs.autonomy("energia", kind), "nunca")

    def test_one_question_per_device_and_action(self):
        self.propose_off()
        self.coord.consider("Energia", AgentReply(resumo="A temperatura desce.", propostas=[Proposal(device_id=PLUG, acao="off")]),
                            [Event(type="temperature_dropping", message="A temperatura no Quarto está a descer.")])
        self.assertEqual(len(self.mem.decisions.pending()), 1)

    def test_setpoint_outside_its_window_is_not_cited(self):
        self.mem.prefs.set(f"room:{QUARTO}", "setpoint_c", {"periods": [{"start": "03:00", "end": "03:01", "c": 25}]})
        sp = self.mem.prefs.get_active(key="setpoint_c")[0]
        state = self.memory.state
        r, c = self.coord.evaluate_prefs("Energia", Proposal(device_id=PLUG, acao="off", respects_preferences=[sp["id"]]),
                                         state.devices[PLUG], state)
        self.assertEqual((r, c), ([], []))

    def test_approving_a_pending_decision_executes_it(self):
        d = self.propose_off()
        done = self.coord.resolve_pending(d["id"], "approved")
        self.assertEqual((done["status"], done["user_response"]), ("executed", "approved"))
        self.assertEqual(self.plug_power(), "off")
        with self.assertRaises(RequestRefused):
            self.coord.resolve_pending(d["id"], "approved")  # only once


class TestNoRealWrites(unittest.TestCase):
    def test_mcp_write_tools_are_blocked_in_tests(self):
        from src.mcp_client import McpClient
        with self.assertRaises(PermissionError):
            McpClient(allow_write=True).call("update_device_capabilities", deviceId=PLUG,
                                             body={"type": "power", "value": "off"})


class TestSafetyVeto(AutonomyCase):
    def test_smoke_ignores_every_preference(self):
        for cat in ("energia", "luz", "conforto", "seguranca"):
            self.mem.prefs.set("global", "autonomy", {"category": cat, "level": "nunca"})
        self.mem.prefs.set(f"room:{QUARTO}", "setpoint_c", {"c": 25})
        self.mem.prefs.set("global", "quiet_hours", {"start": "00:00", "end": "23:59"})
        self.home.smoke()
        self.refresh()
        self.coord.on_event(Event(type="smoke_detected", message="Fumo!", severity="critical"))
        st = normalize(self.home.snapshot(), self.s)
        self.assertEqual(st.cap("plug", "power"), "off")
        self.assertEqual(st.light_hue_rgb, (255, 0, 0))
        d = self.mem.decisions.recent()[0]
        self.assertEqual((d["status"], d["autonomy"]), ("executed", "emergencia"))

    def test_no_switching_on_during_smoke_even_if_approved(self):
        self.home.set_plug(False)
        self.refresh()
        self.coord.emergency_active = True
        self.mem.prefs.set("global", "autonomy", {"category": "conforto", "level": "auto"})
        self.coord.consider("Conforto", AgentReply(resumo="Frio.", propostas=[Proposal(device_id=PLUG, acao="on")]))
        self.assertEqual(self.mem.decisions.recent()[0]["status"], "vetoed")

    def test_arm_disarm_needs_confirmation(self):
        with self.assertRaises(RequestRefused):
            self.coord.set_security("AWAY", confirmed=False)


if __name__ == "__main__":
    unittest.main()
