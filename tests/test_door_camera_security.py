"""Contact sensor on a door (names in events, agents, rules), camera covering its whole room, and
arming the house (MCP payload; an MCP failure becomes a chat answer, not a 500)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.chat.service import ChatService
from src.config import ROOT, get_settings
from src.context import Memories
from src.coordinator import Coordinator, RequestRefused
from src.memory import SharedMemory
from src.perception.state import diff, normalize
from src.sim.home import SimHome
from src.twin.layout import camera_view, load_layout, resolve_layout

SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
CONTACT = "20d98aec-c266-4707-bd27-d06bb51733b7"
CAMERA = "cf105207-a096-4ac1-a891-4a5a15023df6"


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "none.json", "history_db": tmp / "home.db",
            "layout_file": ROOT / "config" / "layout.json", "dry_run": True, "simulate": True})
        self.home = SimHome(self.s.sim_state_file)
        self.home.reset()
        self.memory = SharedMemory()
        self.mem = Memories.open(self.s.history_db)
        self.coord = Coordinator(self.memory, self.s, memories=self.mem)
        self.chat = ChatService(self.memory, self.s, self.coord, self.mem)
        self.refresh()

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self):
        st = normalize(self.home.snapshot(), self.s)
        self.memory.update_state(st)
        return st

    def sensor_on_door(self):
        """As if the user moved the contact sensor to the Sala's door in the 3D."""
        data = json.loads((ROOT / "config" / "layout.json").read_text(encoding="utf-8"))
        data["devices"][CONTACT]["opening"] = "porta-sala"
        data["devices"][CONTACT]["mount"] = "door"
        (Path(self.s.history_db).parent / "layout_user.json").write_text(json.dumps(data), encoding="utf-8")


class TestDoorSensor(Case):
    def test_default_is_window(self):
        st = self.refresh()
        self.assertEqual(st.devices[CONTACT].opening_noun, "janela")
        self.assertIn("janela_aberta", st.room_summary(SALA))

    def test_door_everywhere(self):
        self.sensor_on_door()
        before = self.refresh()
        self.assertEqual(before.devices[CONTACT].opening, "door")
        self.home.set_contact(True)
        after = self.refresh()
        summary = after.room_summary(SALA)
        self.assertTrue(summary["porta_aberta"])
        self.assertNotIn("janela_aberta", summary)
        [ev] = [e for e in diff(before, after, self.s) if e.type == "contact_opened"]
        self.assertEqual(ev.message, "Porta da Sala aberta.")
        self.assertIn("A porta da Sala está aberta", self.chat.handle("A porta da sala está aberta?")[-1]["text"])

    def test_rule_named_after_the_door(self):
        self.sensor_on_door()
        self.refresh()
        msgs = self.chat.handle("Se abrir a porta com o aquecedor ligado, avisa-me")
        self.assertIn("TT_porta_aberta_aquecedor_ligado", msgs[-1]["text"])
        self.assertIn("a porta da Sala abrir", msgs[-1]["text"])


class TestCamera(Case):
    def test_camera_view_covers_the_room(self):
        room = {"width": 5.0, "depth": 4.0}
        target, rot, radius = camera_view(room, (0.25, 2.35, 0.25))
        self.assertEqual(target, (2.5, 0.0, 2.0))
        self.assertAlmostEqual(rot, 52.1, places=1)
        self.assertAlmostEqual(radius, (2.5 ** 2 + 2.0 ** 2) ** 0.5, places=3)

    def test_resolved_camera_always_looks_at_the_room_centre(self):
        layout = load_layout(ROOT / "config" / "layout.json")
        dl = layout.devices[CAMERA].model_copy(update={"target": (0.5, 1.0, 3.9), "position": (4.7, 2.3, 3.7)})
        layout = layout.model_copy(update={"devices": {**layout.devices, CAMERA: dl}})
        out = resolve_layout(layout, self.memory.state)["devices"][CAMERA]
        room = layout.rooms[SALA]
        self.assertEqual(out["target"], [room.x + 2.5, 0.0, room.z + 2.0])
        self.assertIn("view_radius", out)

    def test_twin_state_sends_the_view_radius(self):
        from src.twin.state import twin_state
        [cam] = [d for d in twin_state(self.memory, self.s)["devices"] if d["id"] == CAMERA]
        self.assertAlmostEqual(cam["view_radius"], (2.5 ** 2 + 2.0 ** 2) ** 0.5, places=3)

    def test_moving_the_camera_by_chat_keeps_it_on_the_whole_room(self):
        from src.chat.intents import LayoutRequest
        from src.twin.edit import LayoutEditor
        ed = LayoutEditor(self.memory, self.s, self.mem.prefs)
        _, after, _ = ed.apply(LayoutRequest(alvo="câmara", relacao="direita", referencia="sala"), self.memory.state)
        cam = after.devices[CAMERA]
        self.assertEqual(cam.target, (2.5, 0.0, 2.0))
        self.assertLess(cam.rotation_y, 0)  # on the right wall, turned left towards the centre


class TestSecurity(Case):
    def test_payload_matches_the_mcp_schema(self):
        self.s = self.s.model_copy(update={"dry_run": False})
        self.coord = Coordinator(self.memory, self.s, memories=self.mem)
        with patch.object(self.coord, "execute_tool", wraps=self.coord.execute_tool) as ex:
            self.coord.set_security("STAY", confirmed=True)
        ex.assert_called_once_with("patch_home_security_status",
                                   {"body": {"status": "stay", "armDelay": [], "alarmDelay": []}})

    def test_mcp_failure_is_a_chat_answer(self):
        self.chat.coordinator = self.coord
        msgs = self.chat.handle("arma a casa")
        self.assertIn("Confirmas?", msgs[-1]["text"])
        with patch.object(self.coord, "execute_tool", side_effect=RuntimeError("isError: bad request")):
            out = self.chat.action("confirm", id=msgs[-1]["confirm_id"])
        self.assertIn("Não consegui", out[-1]["text"])
        with patch.object(self.coord, "execute_tool", side_effect=RuntimeError("x")):
            with self.assertRaises(RequestRefused):
                self.coord.set_security("AWAY", confirmed=True)


if __name__ == "__main__":
    unittest.main()


class TestArmAndRedLight(Case):
    RED = "255,0,0"

    def test_arm_away_and_mode_only_messages(self):
        from src.chat.intents import classify_rules
        for text, acao in [("arma a casa em modo away", "armar_ausente"), ("Quero em modo away", "armar_ausente"),
                           ("arma a casa, vou sair", "armar_ausente"), ("arma a casa", "armar_casa"),
                           ("desliga o alarme, sou eu", "desarmar"), ("liga a luz da sala", "ligar")]:
            i = classify_rules(text, ["Sala", "Quarto"])
            self.assertEqual((i.intent, i.comando.acao), ("comando_dispositivo", acao), text)
        msgs = self.chat.handle("arma a casa em modo ausente")
        self.assertIn("modo ausente", msgs[-1]["text"])

    def test_disarm_status_from_mcp_is_disarmed(self):
        snap = self.home.snapshot()
        snap["home"]["security"]["status"] = "DISARM"
        self.assertEqual(normalize(snap, self.s).security_status, "DISARMED")

    def test_late_intrusion_alarm_while_disarmed_does_nothing(self):
        from src.perception.state import Event
        self.coord.on_event(Event(type="security_alarm", message="x", data={"kind": "intrusion"}))
        self.assertEqual(self.mem.decisions.recent(), [])

    def _person_seen(self, security: str, has_person=True, confidence=0.98):
        from src.perception.state import Event
        snap = self.home.snapshot()
        snap["home"]["security"]["status"] = security
        self.memory.update_state(normalize(snap, self.s))
        data = {"status": "ok", "has_person": has_person, "person_count": int(has_person),
                "overall_confidence": confidence, "trigger_type": "contact_opened"}
        with patch.object(self.coord, "execute", return_value={"dry_run": True}) as ex:
            self.coord.on_event(Event(type="camera_analysis", message="x", room=SALA, room_name="Sala",
                                      data=data))
        return [c.args[2] for c in ex.call_args_list]

    def test_person_on_camera_while_away_turns_the_light_red(self):
        bodies = self._person_seen("AWAY")
        self.assertIn({"type": "color_rgb", "redValue": 255, "greenValue": 0, "blueValue": 0}, bodies)
        self.assertIn("Intrusão", self.memory.recent_decisions()[-1].message)

    def test_person_on_camera_not_an_intrusion_when_home_or_unsure(self):
        self.assertEqual(self._person_seen("STAY"), [])
        self.assertEqual(self._person_seen("AWAY", has_person=False), [])
        self.assertEqual(self._person_seen("AWAY", confidence=0.3), [])

    def test_false_alarm_in_chat_disarms_and_stops_the_alarm(self):
        """Armed AWAY, door opened -> hub alarm + red light; "fui eu, foi falso alarme" + Confirmar ->
        disarmed, hub back to normal, light back as it was, the twin no longer shows the alarm."""
        from src.perception.state import Event
        from src.twin.state import twin_state
        self.s = self.s.model_copy(update={"dry_run": False})
        self.coord = Coordinator(self.memory, self.s, memories=self.mem)
        self.chat.coordinator = self.coord
        self.home.set_security("AWAY")
        self.refresh()
        self.home.set_contact(True)
        st = self.refresh()
        self.assertTrue(st.is_alarmed)
        self.coord.on_event(Event(type="security_alarm", message="x", data={"kind": "intrusion"}))
        light = self.refresh().by_role("light")
        self.assertEqual(light.caps.get("color_rgb").split(",")[1:], ["0", "0"])
        self.assertTrue(twin_state(self.memory, self.s)["alarm"]["any"])

        msgs = self.chat.handle("fui eu, foi falso alarme")
        self.assertIn("desarmar a casa e desligar o alarme", msgs[-1]["text"])
        out = self.chat.action("confirm", id=msgs[-1]["confirm_id"])
        self.assertIsNone(self.chat.confirm)
        snap = self.home.snapshot()
        self.assertEqual(snap["home"]["security"]["status"], "DISARMED")
        self.assertFalse(snap["home"]["security"]["isAlarmed"])
        self.assertIn("255,255,255", json.dumps(snap))  # the light is white again
        st = self.refresh()
        self.assertEqual(st.by_role("light").caps.get("color_rgb"), "255,255,255")
        self.assertFalse(twin_state(self.memory, self.s)["alarm"]["any"])

    def test_alarm_banner_button_asks_once(self):
        """The 3D alarm banner sends «Foi falso alarme, desliga o alarme»: two parts, one request."""
        msgs = self.chat.handle("Foi falso alarme, desliga o alarme")
        self.assertEqual(sum(1 for m in msgs if m.get("confirm_id")), 1)
        out = self.chat.action("confirm", id=msgs[-1]["confirm_id"])
        self.assertFalse(any(m.get("confirm_id") for m in out))  # no second «Confirmas?» after accepting
        self.assertIsNone(self.chat.confirm)

    def test_late_camera_analysis_after_disarm_does_not_bring_the_red_back(self):
        from src.perception.state import Event
        snap = self.home.snapshot()
        snap["home"]["security"]["status"] = "AWAY"
        self.memory.update_state(normalize(snap, self.s))  # the snapshot predates the disarm
        self.coord.stop_alarm()
        data = {"status": "ok", "has_person": True, "overall_confidence": 0.98}
        with patch.object(self.coord, "execute", return_value={"dry_run": True}) as ex:
            self.coord.on_event(Event(type="camera_analysis", message="x", data=data))
        ex.assert_not_called()

    def test_twin_hides_an_alarm_the_user_stopped_while_the_hub_still_reports_it(self):
        from src.twin.state import twin_state
        self.home.set_security("AWAY")
        self.refresh()
        self.home.set_contact(True)
        self.refresh()
        self.coord.stop_alarm()  # the MCP still says isAlarmed (this snapshot predates the disarm)
        self.assertFalse(twin_state(self.memory, self.s)["alarm"]["any"])
        self.home.set_contact(False)
        self.home.set_contact(True)  # a new alarm entry is a new alarm
        self.refresh()
        self.assertTrue(twin_state(self.memory, self.s)["alarm"]["any"])

    def test_red_light_goes_white_when_disarmed(self):
        from src.perception.state import Event
        light = self.memory.state.by_role("light")
        snap = self.home.snapshot()
        for room in snap["home"]["rooms"]:
            for d in room["devices"]:
                if d["id"] == light.id:
                    for c in d["capabilities"]:
                        if c["type"] == "color_rgb":
                            c["value"] = self.RED
                        if c["type"] == "power":
                            c["value"] = "on"
        self.memory.update_state(normalize(snap, self.s))
        with patch.object(self.coord, "execute", return_value={"dry_run": True}) as ex:
            self.coord.on_event(Event(type="tick", message="t"))
        bodies = [c.args[2] for c in ex.call_args_list]
        self.assertTrue(any(b.get("type") == "color_rgb" or "255,255,255" in json.dumps(b) for b in bodies), bodies)
