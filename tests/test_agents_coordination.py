import json
import tempfile
import time
import unittest
from pathlib import Path

from src.agents import bridge as bridge_mod
from src.agents.bridge import AgentBridge, build_prompt, route
from src.agents.specs import AgentReply
from src.config import get_settings
from src.coordinator import Coordinator
from src.memory import EventBus, SharedMemory
from src.perception.sources import SimSource
from src.perception.state import Event, diff, normalize
from tests.agents_support import A2AServer, FakeChat, free_port

PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
LIGHT = "b667d87c-2566-440a-afce-02d9ec82df1b"
HUB = "ef8015f6-d7af-4da6-b48a-f1d0e7630b3e"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"


def reply(*props, resumo="Janela aberta na Sala com o aquecedor do Quarto ligado."):
    return AgentReply.model_validate({"resumo": resumo, "propostas": [
        {"device_id": d, "acao": a, "motivo": "teste"} for d, a in props]})


def dropping():
    return Event(type="temperature_dropping", severity="warning", room=QUARTO, room_name="Quarto",
                 message="A temperatura no Quarto está a descer: 21,5 °C → 20,9 °C em 10 min "
                         "(janela aberta na Sala; aquecedor ligado no Quarto).",
                 data={"window_open": True, "heater_on": True})


class HouseTestCase(unittest.TestCase):
    dry_run = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "simulate": True, "dry_run": self.dry_run, "sim_state_file": tmp / "sim.json",
            "home_config_file": tmp / "none.json", "history_db": tmp / "h.db", "agent_timeout_s": 15,
            "agent_cooldown_s": 60})
        self.source = SimSource(self.s)
        self.home = self.source.home
        self.home.reset()
        self.home.set_plug(True)
        self.home.set_contact(True)
        self.memory, self.bus = SharedMemory(), EventBus()
        self.refresh()
        self.coord = Coordinator(self.memory, self.s, sim=self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self):
        prev = self.memory.state
        st = normalize(self.source.fetch(), self.s)
        self.memory.update_state(st)
        events = diff(prev, st, self.s)
        self.memory.add_events(events)
        self.bus.publish(events)
        return events

    def decisions(self, agent=None):
        return [d for d in self.memory.recent_decisions(100) if agent is None or d.agent == agent]


class TestRoute(HouseTestCase):
    def test_routing(self):
        st = self.memory.state
        self.assertEqual(route(dropping(), st), ["energia", "conforto"])
        self.assertEqual(route(Event(type="smoke_detected", message="x"), st), [])  # emergency path
        self.assertEqual(route(Event(type="plug_switched", message="x"), st), ["energia"])
        self.assertEqual(route(Event(type="contact_opened", message="x"), st), ["energia"])  # heater on, disarmed
        self.assertEqual(route(Event(type="camera_motion", message="x"), st), ["seguranca"])
        self.assertEqual(route(Event(type="hub_alarm", message="x"), st, emergency_active=True), [])
        near = Event(type="temperature_changed", message="x", room=QUARTO, data={"after": 21.3})
        far = Event(type="temperature_changed", message="x", room=QUARTO, data={"after": 19.5})
        self.assertEqual(route(near, st), [])
        self.assertEqual(route(far, st), ["conforto"])

    def test_armed_contact_goes_to_security(self):
        self.home.set_security("away")
        self.refresh()
        self.assertIn("seguranca", route(Event(type="contact_opened", message="x"), self.memory.state))

    def test_prompt(self):
        text = build_prompt([dropping()], self.memory.state)
        self.assertIn("janela aberta na Sala; aquecedor ligado no Quarto", text)
        self.assertIn(f"- {PLUG}: Tomada (aquecedor) do Quarto, ligada, 1500 W", text)
        self.assertIn(f"- {LIGHT}: Luz do Quarto, ligada", text)
        self.assertNotIn(HUB, text)  # the hub is never actionable
        self.assertIn('"Sala"', text)


class TestConsider(HouseTestCase):
    def test_approved_in_dry_run(self):
        out = self.coord.consider("Energia", reply((PLUG, "off")), [dropping()])
        self.assertEqual(out[0].agent, "Energia")
        self.assertEqual(out[0].status, "proposta")
        self.assertIn("Proponho desligar a tomada do Quarto", out[0].message)
        self.assertEqual((out[1].agent, out[1].status), ("Coordenador", "dry-run"))
        self.assertIn("Aprovo a proposta da Energia", out[1].message)
        self.refresh()
        self.assertTrue(self.memory.state.plug_on)  # dry-run: nothing written

    def test_invalid_proposals_rejected(self):
        out = self.coord.consider("Segurança", reply(("inventado", "on"), (HUB, "off")))
        self.assertEqual([d.status for d in out[1:]], ["rejeitada", "rejeitada"])
        self.assertIn("dispositivo desconhecido", out[1].message)
        self.assertIn("não permitida", out[2].message)

    def test_no_op(self):
        out = self.coord.consider("Conforto", reply((PLUG, "on")))
        self.assertEqual(out[1].status, "informação")
        self.assertIn("já está ligada", out[1].message)

    def test_analysis_without_proposals(self):
        out = self.coord.consider("Segurança", reply(resumo="Casa desarmada, sem risco."), [dropping()])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].status, "informação")
        self.assertEqual(out[0].room_name, "Quarto")

    def test_priority_conflict(self):
        self.coord.consider("Energia", reply((PLUG, "off")))
        self.home.set_plug(False)
        self.refresh()
        out = self.coord.consider("Conforto", reply((PLUG, "on")))  # lower priority, opposite action
        self.assertEqual(out[1].status, "rejeitada")
        self.assertIn("Energia decidiu o contrário", out[1].message)
        out = self.coord.consider("Segurança", reply((PLUG, "on")))  # higher priority: allowed
        self.assertEqual(out[1].status, "dry-run")

    def test_emergency_blocks_switching_on(self):
        self.coord.emergency_active = True
        self.home.set_plug(False)
        self.refresh()
        out = self.coord.consider("Conforto", reply((PLUG, "on")))
        self.assertIn("emergência de fumo", out[1].message)


class TestEmergency(HouseTestCase):
    def test_smoke_runs_without_llm(self):
        self.bus.subscribe(self.coord.on_event, types=["smoke_detected", "alarm_cancelled"])
        self.home.smoke()
        self.refresh()
        em = self.decisions("Emergência")
        self.assertEqual(len(em), 1)
        self.assertEqual(em[0].status, "dry-run")
        self.assertIn("Fumo na Sala: protocolo de emergência (sem LLM)", em[0].message)
        self.assertIn("desligar a tomada", em[0].message)
        self.assertIn("luz a vermelho", em[0].message)
        self.assertTrue(self.coord.emergency_active)
        self.home.cancel_alarm()
        self.refresh()
        self.assertFalse(self.coord.emergency_active)


class TestEmergencyWrites(HouseTestCase):
    dry_run = False

    def test_smoke_switches_heater_off_and_light_red(self):
        self.bus.subscribe(self.coord.on_event, types=["smoke_detected"])
        self.home.smoke()
        self.refresh()
        self.assertEqual(self.decisions("Emergência")[0].status, "executada")
        self.refresh()
        st = self.memory.state
        self.assertFalse(st.plug_on)
        self.assertEqual(st.light_hue_rgb, (255, 0, 0))
        self.assertEqual(st.cap("light", "brightness"), 100)


class TestBridgeOverA2A(HouseTestCase):
    """Real A2A (HTTP JSON-RPC) to an agent server backed by a fake chat client."""

    def setUp(self):
        super().setUp()
        self._batch = bridge_mod.BATCH_S
        bridge_mod.BATCH_S = 0.05

    def tearDown(self):
        bridge_mod.BATCH_S = self._batch
        super().tearDown()

    def start_bridge(self, url):
        return AgentBridge(self.memory, self.s, self.coord, self.bus, urls={"energia": url}).start()

    def wait(self, cond, timeout=15):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if cond():
                return True
            time.sleep(0.05)
        return False

    def test_event_to_decision(self):
        chat = FakeChat(json.dumps({"resumo": "Aquecedor do Quarto ligado com a janela da Sala aberta.",
                                    "propostas": [{"device_id": PLUG, "acao": "off", "motivo": "desperdício"}]},
                                   ensure_ascii=False))
        with A2AServer("energia", chat) as srv:
            br = self.start_bridge(srv.url)
            self.bus.publish([dropping()])
            self.assertTrue(self.wait(lambda: self.decisions("Coordenador")))
            br.stop()
        self.assertIn("A temperatura no Quarto está a descer", chat.prompts[0])
        self.assertEqual(self.decisions("Energia")[0].status, "proposta")
        self.assertEqual(self.decisions("Coordenador")[0].status, "dry-run")
        status = {a["key"]: a for a in self.memory.agent_status()}["energia"]
        self.assertTrue(status["online"])
        self.assertFalse(status["busy"])

    def test_failed_task_is_reported_not_acted_on(self):
        with A2AServer("energia", FakeChat(error="modelo indisponível")) as srv:
            br = self.start_bridge(srv.url)
            self.bus.publish([dropping()])
            self.assertTrue(self.wait(lambda: self.decisions("Energia")))
            br.stop()
        d = self.decisions("Energia")[0]
        self.assertEqual(d.status, "erro")
        self.assertIn("modelo indisponível", d.message)
        self.assertEqual(self.decisions("Coordenador"), [])

    def test_agent_down(self):
        br = self.start_bridge(f"http://127.0.0.1:{free_port()}/")
        self.bus.publish([dropping()])
        self.assertTrue(self.wait(lambda: self.decisions("Sistema")))
        br.stop()
        self.assertIn("Agente Energia indisponível", self.decisions("Sistema")[0].message)
        self.assertFalse({a["key"]: a for a in self.memory.agent_status()}["energia"]["online"])

    def test_cooldown(self):
        chat = FakeChat()
        with A2AServer("energia", chat) as srv:
            br = self.start_bridge(srv.url)
            e = Event(type="plug_switched", message="Aquecedor (tomada) do Quarto ligado.", room=QUARTO)
            self.bus.publish([e])
            self.assertTrue(self.wait(lambda: len(chat.prompts) == 1))
            self.bus.publish([e.model_copy(update={"id": "outro"})])  # same type+room, info: not re-sent
            time.sleep(0.5)
            br.stop()
        self.assertEqual(len(chat.prompts), 1)


if __name__ == "__main__":
    unittest.main()
