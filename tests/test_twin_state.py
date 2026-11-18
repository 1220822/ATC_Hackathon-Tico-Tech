import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import create_app
from src.config import ROOT, get_settings
from src.memory import Decision, SharedMemory
from src.perception.sources import SimSource
from src.perception.state import diff, normalize
from src.twin.state import twin_state, twin_stream

SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
LIGHT = "b667d87c-2566-440a-afce-02d9ec82df1b"
SMOKE = "726d443d-502c-41ab-b9c1-f1a84a02a10c"


class TwinTestCase(unittest.TestCase):
    config: dict | None = None  # config/home.json for the test (None = no simulated rooms)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        cfg = tmp / "home.json"
        if self.config is not None:
            cfg.write_text(json.dumps(self.config), encoding="utf-8")
        self.s = get_settings().model_copy(update={
            "simulate": True, "sim_state_file": tmp / "sim.json", "home_config_file": cfg,
            "history_db": tmp / "history.db", "layout_file": ROOT / "config" / "layout.json"})
        self.source = SimSource(self.s)
        self.home = self.source.home
        self.home.reset()
        self.memory = SharedMemory()
        self.refresh()

    def tearDown(self):
        self.tmp.cleanup()

    def refresh(self):
        prev = self.memory.state
        st = normalize(self.source.fetch(), self.s)
        self.memory.update_state(st)
        self.memory.add_events(diff(prev, st, self.s))
        return st

    def twin(self, **kw):
        return twin_state(self.memory, self.s, **kw)

    def dev(self, twin, dev_id):
        return next(d for d in twin["devices"] if d["id"] == dev_id)


class TestTwinState(TwinTestCase):
    def test_rooms_and_devices(self):
        t = self.twin()
        self.assertEqual([r["name"] for r in t["rooms"]], ["Sala", "Quarto"])
        self.assertEqual(len(t["devices"]), 7)
        self.assertEqual(t["warnings"], [])
        light = self.dev(t, LIGHT)
        self.assertEqual((light["room"], light["room_name"], light["type"]), (QUARTO, "Quarto", "bulb"))
        self.assertEqual(light["mount"], "ceiling")
        self.assertTrue(light["online"])
        self.assertEqual(t["security"], {"status": "DISARMED", "label": "desarmada", "armed": False,
                                         "is_alarmed": False})
        self.assertEqual(t["mode"], "simulado")
        quarto = next(r for r in t["rooms"] if r["id"] == QUARTO)
        self.assertEqual(quarto["temperature_c"], 21.0)

    def test_values(self):
        self.home.set_contact(True)
        self.home.set_plug(True)
        self.home.set_temperature(20.9)
        self.home.set_humidity(71)
        self.refresh()
        t = self.twin()
        by_role = {d["role"]: d["values"] for d in t["devices"]}
        self.assertEqual(by_role["temperature"], {"temperature_c": 20.9, "humidity_pct": 71.0, "battery": 100})
        self.assertTrue(by_role["contact"]["open"])
        self.assertEqual(by_role["contact"]["battery"], "high")
        self.assertEqual((by_role["plug"]["on"], by_role["plug"]["power_w"]), (True, 1500.0))
        self.assertFalse(by_role["smoke"]["alarm"])
        self.assertFalse(by_role["camera"]["motion"])
        self.assertEqual(by_role["hub"]["battery"], 100)

    def test_light_colour_is_hue_normalised(self):
        self.home.apply_capability(LIGHT, {"type": "brightness", "value": 40})
        self.home.apply_capability(LIGHT, {"type": "color_rgb", "redValue": 255, "greenValue": 140, "blueValue": 0})
        self.refresh()
        v = self.dev(self.twin(), LIGHT)["values"]
        self.assertEqual(v["color_rgb"], [255, 140, 0])
        self.assertEqual(v["color_rgb_reported"], "102,56,0")
        self.assertEqual(v["brightness"], 40)
        self.assertTrue(v["on"])

    def test_smoke_alarm(self):
        self.home.smoke()  # real behaviour: EMERGENCY with an empty device
        self.refresh()
        t = self.twin()
        self.assertTrue(t["alarm"]["smoke"])
        self.assertEqual(t["alarm"]["smoke_room"], SALA)
        self.assertTrue(self.dev(t, SMOKE)["values"]["alarm"])
        self.assertTrue(next(r for r in t["rooms"] if r["id"] == SALA)["smoke"])
        self.assertIn("smoke_detected", [e["type"] for e in t["events"]])
        self.assertEqual(next(e for e in t["events"] if e["type"] == "smoke_detected")["area"], "Emergência")
        self.home.cancel_alarm()
        self.refresh()
        self.assertFalse(self.twin()["alarm"]["smoke"])

    def test_camera_motion_and_offline(self):
        self.home.camera(motion=True)
        self.home.set_device_online("bulb", False)
        self.refresh()
        t = self.twin()
        self.assertTrue(next(d for d in t["devices"] if d["role"] == "camera")["values"]["motion"])
        self.assertFalse(self.dev(t, LIGHT)["online"])
        self.assertIsNotNone(self.dev(t, LIGHT)["last_seen"])

    def test_events_and_decisions(self):
        self.home.set_contact(True)
        self.refresh()
        self.memory.add_decision(Decision(agent="Energia", message="janela aberta com o aquecedor ligado: "
                                                                 "proponho desligar", device_id=PLUG))
        t = self.twin(n=10)
        self.assertEqual([e["message"] for e in t["events"]], ["Janela da Sala aberta."])
        self.assertEqual(t["events"][0]["area"], "Segurança")
        self.assertEqual(t["decisions"][0]["agent"], "Energia")
        self.assertEqual(t["decisions"][0]["kind"], "decision")


class TestDeviceWithoutLayout(TwinTestCase):
    def setUp(self):
        super().setUp()
        layout = json.loads((ROOT / "config" / "layout.json").read_text(encoding="utf-8"))
        del layout["devices"][PLUG]
        path = Path(self.tmp.name) / "layout.json"
        path.write_text(json.dumps(layout), encoding="utf-8")
        self.s = self.s.model_copy(update={"layout_file": path})

    def test_api_still_shows_it(self):
        with TestClient(create_app(self.memory, self.s)) as client:
            t = client.get("/twin/state").json()
        self.assertEqual(len(t["devices"]), 7)
        plug = self.dev(t, PLUG)
        self.assertEqual((plug["placed"], plug["room"]), ("default", QUARTO))
        self.assertTrue(any("Plug" in w for w in t["warnings"]))


class TestSimulatedRoomsExcluded(TwinTestCase):
    config = {"simulated_rooms": [{"name": "Cozinha", "devices": ["temperature", "plug"]}]}

    def test_only_mcp_rooms(self):
        self.assertIsNotNone(self.memory.state.room("Cozinha"))  # perception still has it
        t = self.twin()
        self.assertEqual([r["name"] for r in t["rooms"]], ["Sala", "Quarto"])
        self.assertEqual(len(t["devices"]), 7)


class TestApiAndStream(TwinTestCase):
    def test_503_before_first_poll(self):
        self.memory.update_state(None)
        with TestClient(create_app(self.memory, self.s)) as client:
            self.assertEqual(client.get("/twin/state").status_code, 503)

    def test_stream_snapshot_then_state_and_events(self):
        async def run():
            gen = twin_stream(self.memory, self.s, check_s=0.01, max_messages=3)
            first = await anext(gen)
            self.home.set_contact(True)
            self.refresh()  # one perception poll: new state + contact_opened
            rest = [await anext(gen), await anext(gen)]
            return [first] + rest

        msgs = asyncio.run(asyncio.wait_for(run(), 5))
        kinds = [m.split("\n", 1)[0] for m in msgs]
        self.assertEqual(kinds[0], "event: snapshot")
        self.assertEqual(sorted(kinds[1:]), ["event: event", "event: state"])
        state = json.loads(next(m for m in msgs if m.startswith("event: state")).split("data: ", 1)[1])
        self.assertNotIn("events", state)  # events travel separately
        contact = next(d for d in state["devices"] if d["role"] == "contact")
        self.assertTrue(contact["values"]["open"])
        event = json.loads(next(m for m in msgs if m.startswith("event: event")).split("data: ", 1)[1])
        self.assertEqual(event["message"], "Janela da Sala aberta.")

    def test_stream_waits_for_first_state(self):
        self.memory.update_state(None)

        async def run():
            gen = twin_stream(self.memory, self.s, check_s=0.01, heartbeat_s=0.05, max_messages=1)
            return await anext(gen)

        self.assertEqual(asyncio.run(asyncio.wait_for(run(), 5)), ": ping\n\n")


if __name__ == "__main__":
    unittest.main()
