import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import create_app
from src.config import ROOT, get_settings
from src.coordinator import Coordinator
from src.memory import SharedMemory
from src.perception.sources import SimSource
from src.perception.state import normalize

PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"
LIGHT = "b667d87c-2566-440a-afce-02d9ec82df1b"
HUB = "ef8015f6-d7af-4da6-b48a-f1d0e7630b3e"


class FakeMcp:
    def __init__(self):
        self.calls = []

    def call(self, tool, **args):
        self.calls.append((tool, args))
        return {"message": "device updated"}


class CoordinatorTestCase(unittest.TestCase):
    dry_run = True
    simulate = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "simulate": True, "dry_run": self.dry_run, "sim_state_file": tmp / "sim.json",
            "home_config_file": tmp / "none.json", "history_db": tmp / "h.db",
            "layout_file": ROOT / "config" / "layout.json"})
        self.source = SimSource(self.s)
        self.home = self.source.home
        self.home.reset()
        self.home.set_plug(True)
        self.memory = SharedMemory()
        self.refresh()
        self.mcp = FakeMcp()
        self.coord = Coordinator(self.memory, self.s, mcp=self.mcp, sim=self.home)
        self.client = TestClient(create_app(self.memory, self.s, self.coord))

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def refresh(self):
        raw = self.source.fetch()
        if not self.simulate:
            raw["source"] = "mcp"  # pretend these devices are the real MCP ones
        self.memory.update_state(normalize(raw, self.s))

    def post(self, device_id, action):
        return self.client.post("/coordinator/request", json={"device_id": device_id, "action": action})


class TestDryRun(CoordinatorTestCase):
    def test_dry_run_records_and_writes_nothing(self):
        r = self.post(PLUG, "off")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["status"], "dry-run")
        self.assertEqual(d["agent"], "Coordenador")
        self.assertIn("desligar a tomada do Quarto", d["message"])
        self.assertIn("nada foi enviado", d["message"])
        self.refresh()
        self.assertTrue(self.memory.state.plug_on)  # unchanged
        self.assertEqual(self.mcp.calls, [])
        twin = self.client.get("/twin/state").json()
        self.assertEqual(twin["decisions"][-1]["status"], "dry-run")
        self.assertTrue(twin["dry_run"])
        self.assertEqual(set(twin["actions"]), {"light", "plug"})

    def test_unknown_device(self):
        self.assertEqual(self.post("inventado", "on").status_code, 404)

    def test_security_and_other_devices_refused(self):
        d = self.post(HUB, "off").json()
        self.assertEqual(d["status"], "rejeitada")
        self.assertIn("não permitida", d["message"])

    def test_offline_device_refused(self):
        self.home.set_device_online("bulb", False)
        self.refresh()
        self.assertEqual(self.post(LIGHT, "off").json()["status"], "rejeitada")

    def test_bad_action_is_422(self):
        self.assertEqual(self.post(PLUG, "arm").status_code, 422)

    def test_no_state_yet(self):
        self.memory.update_state(None)
        self.assertEqual(self.post(PLUG, "off").status_code, 503)


class TestSimWrite(CoordinatorTestCase):
    dry_run = False

    def test_sim_device_written_to_simhome(self):
        d = self.post(PLUG, "off").json()
        self.assertEqual(d["status"], "executada")
        self.refresh()
        self.assertFalse(self.memory.state.plug_on)
        self.assertEqual(self.mcp.calls, [])


class TestMcpWrite(CoordinatorTestCase):
    dry_run = False
    simulate = False

    def test_mcp_device_goes_through_the_mcp_tool(self):
        d = self.post(LIGHT, "off").json()
        self.assertEqual(d["status"], "executada")
        self.assertEqual(self.mcp.calls, [("update_device_capabilities",
                                           {"deviceId": LIGHT, "body": {"type": "power", "value": "off"}})])



class BulbMcp(FakeMcp):
    """Real-bulb-like reads: reports `reported` (colour, brightness) for the light."""

    def __init__(self, reported):
        super().__init__()
        self.reported = reported

    def call(self, tool, **args):
        self.calls.append((tool, args))
        if tool == "get_device_details":
            rgb, b = self.reported
            return {"data": {"id": args["deviceId"], "capabilities": [
                {"type": "color_rgb", "value": ",".join(map(str, rgb))}, {"type": "brightness", "value": b}]}}
        return {"message": "device updated"}


class TestLightSequence(CoordinatorTestCase):
    dry_run = False
    simulate = False

    def test_brightness_before_colour(self):
        from src.perception.state import Event
        self.coord._check_lights_later = lambda: None
        d = self.coord.intrusion(Event(type="security_alarm", message="x", data={"kind": "intrusion"}))
        types = [s["type"] for s in d.action["passos"]]
        self.assertEqual(types, ["power", "brightness", "color_rgb"])
        writes = [a["body"]["type"] for tool, a in self.mcp.calls if tool == "update_device_capabilities"]
        self.assertEqual(writes, ["power", "brightness", "color_rgb"])

    def test_stale_colour_is_re_sent(self):
        light = self.memory.state.devices[LIGHT]
        bulb = BulbMcp(((0, 255, 0), 100))  # brightness took, colour did not (seen on the real bulb)
        self.coord._mcp = bulb
        sent = self.coord._check_lights([(light, (255, 0, 0), 100)], attempts=1, wait_s=0)
        self.assertEqual(sent, 2)
        bodies = [a["body"] for tool, a in bulb.calls if tool == "update_device_capabilities"]
        self.assertEqual(bodies, [{"type": "brightness", "value": 100},
                                  {"type": "color_rgb", "redValue": 255, "greenValue": 0, "blueValue": 0}])

    def test_nothing_re_sent_when_right(self):
        light = self.memory.state.devices[LIGHT]
        self.coord._mcp = BulbMcp(((255, 0, 0), 100))
        self.assertEqual(self.coord._check_lights([(light, (255, 0, 0), 100)], attempts=2, wait_s=0), 0)

if __name__ == "__main__":
    unittest.main()
