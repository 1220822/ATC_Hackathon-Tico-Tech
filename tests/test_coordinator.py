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


if __name__ == "__main__":
    unittest.main()
