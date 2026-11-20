import json
import tempfile
import unittest
from pathlib import Path

from src.config import ROOT, get_settings
from src.home_config import HomeConfig, HomeConfigError, load_home_config, room_settings
from src.perception.sources import HybridSource, SimSource
from src.perception.state import diff, normalize
from src.sim.home import SimHome
from src.writes import UnknownDevice, route_write

CAPS = ROOT / "docs" / "capabilities"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"

TEST_CONFIG = {
    "rooms": {"Quarto": {"setpoint_c": 21.5}},
    "simulated_rooms": [{
        "name": "Cozinha", "type": "kitchen", "devices": ["temperature", "contact", "plug", "smoke"],
        "setpoint_c": 19.0, "plug_power_change_w": 100, "initial_temperature_c": 19.5,
    }],
}


def types(events):
    return [e.type for e in events]


class FakeMcp:
    """Answers the two read tools of McpSource from the 18/11 dump; records every call."""

    def __init__(self):
        self.calls = []

    def call(self, tool, **args):
        self.calls.append((tool, args))
        if tool == "get_home":
            return json.loads((CAPS / "get_home.json").read_text(encoding="utf-8"))["data"]
        if tool == "get_home_security_alarm_history":
            return {"history": []}
        return {"message": "device updated"}


class RoomsTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Path(self.tmp.name) / "home.json"
        cfg.write_text(json.dumps(TEST_CONFIG), encoding="utf-8")
        self.s = get_settings().model_copy(update={
            "sim_state_file": Path(self.tmp.name) / "sim.json", "home_config_file": cfg,
            "simulate": True})
        self.cfg = load_home_config(cfg)
        self.home = SimHome(self.s.sim_state_file, self.cfg.simulated_rooms)
        self.home.reset()

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self):
        return normalize(SimSource(self.s, self.cfg).fetch(), self.s)


class TestHomeConfig(unittest.TestCase):
    def test_repo_config_is_valid(self):
        cfg = load_home_config(ROOT / "config" / "home.json")
        self.assertEqual(cfg.sim_room_ids, [])  # the house as it is in the 3D: Sala and Quarto only
        self.assertIn("Quarto", cfg.rooms)

    def test_missing_file_is_empty(self):
        self.assertEqual(load_home_config(ROOT / "nao-existe.json"), HomeConfig())

    def test_invalid(self):
        bad = [
            {"simulated_rooms": [{"name": "X", "devices": ["fridge"]}]},
            {"simulated_rooms": [{"name": "X", "devices": []}]},
            {"simulated_rooms": [{"name": "X", "id": "cozinha", "devices": ["plug"]}]},
            {"simulated_rooms": [{"name": "X", "devices": ["plug"]}, {"name": "x", "devices": ["plug"]}]},
            {"rooms": {"Quarto": {"setpoint": 21}}},
            {"rooms": {"Quarto": {"setpoint_c": 80}}},
            {"unknown": 1},
        ]
        for data in bad:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as tmp:
                p = Path(tmp) / "home.json"
                p.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(HomeConfigError):
                    load_home_config(p)

    def test_room_overrides_env(self):
        cfg = HomeConfig.model_validate(TEST_CONFIG)
        s = get_settings()
        self.assertEqual(room_settings(s, "sim-cozinha", "Cozinha", cfg).plug_power_change_w, 100)
        self.assertEqual(room_settings(s, QUARTO, "quarto", cfg).setpoint_c, 21.5)
        self.assertEqual(room_settings(s, "outra", "Sala", cfg).plug_power_change_w, s.plug_power_change_w)


class TestSimulatedRooms(RoomsTestCase):
    def test_generated_devices(self):
        rooms = {r["id"]: r for r in self.home.snapshot()["home"]["rooms"]}
        self.assertIn("sim-cozinha", rooms)
        devs = rooms["sim-cozinha"]["devices"]
        self.assertEqual([d["id"] for d in devs], ["sim-cozinha-temperature", "sim-cozinha-contact",
                                                   "sim-cozinha-plug", "sim-cozinha-smoke"])
        for d in devs:  # same JSON shape as the MCP devices
            self.assertEqual(d["roomName"], "Cozinha")
            self.assertIsInstance(d["capabilities"], list)
            self.assertGreater(d["lastCommunicationTime"], 0)

    def test_normalized(self):
        st = self.snap()
        self.assertEqual(len(st.rooms), 3)
        cozinha = st.room("Cozinha")
        self.assertTrue(cozinha.simulated)
        self.assertFalse(st.room("Quarto").simulated)
        self.assertEqual(cozinha.setpoint_c, 19.0)
        self.assertEqual(st.room("Quarto").setpoint_c, 21.5)
        self.assertEqual(st.cap("temperature", "temperatureValue", room="cozinha"), 19.5)
        self.assertTrue(all(d.source == "sim" for d in st.devices.values()))  # SIMULATE=true
        self.assertTrue(st.summary()["divisoes"]["Cozinha"]["simulado"])
        # default room for helpers stays the first real one
        self.assertEqual(st.by_role("temperature").room_name, "Quarto")

    def test_room_argument(self):
        a = self.snap()
        self.home.set_temperature(18.0, room="cozinha")
        self.home.set_contact(True, room="sim-cozinha")
        ev = diff(a, self.snap(), self.s)
        self.assertEqual({(e.type, e.room_name) for e in ev},
                         {("temperature_changed", "Cozinha"), ("contact_opened", "Cozinha")})
        self.assertEqual(self.snap().cap("temperature", "temperatureValue", room="Quarto"), 21.0)
        with self.assertRaises(KeyError):
            self.home.set_temperature(20, room="Sala")  # no temperature sensor there
        with self.assertRaises(KeyError):
            self.home.set_temperature(20, room="Garagem")

    def test_per_room_power_threshold(self):
        a = self.snap()
        self.home.set_plug(True, 60, room="Cozinha")  # Cozinha threshold 100 W
        self.home.set_plug(True, 60, room="Quarto")  # .env threshold 50 W
        changed = [e.room_name for e in diff(a, self.snap(), self.s) if e.type == "plug_power_changed"]
        self.assertEqual(changed, ["Quarto"])

    def test_config_change_syncs_state_file(self):
        plain = SimHome(self.s.sim_state_file)  # config without simulated rooms
        self.assertNotIn("sim-cozinha", plain.room_ids())
        self.home.set_temperature(20.0)  # real rooms untouched by the sync
        again = SimHome(self.s.sim_state_file, self.cfg.simulated_rooms)
        self.assertIn("sim-cozinha", again.room_ids())
        self.assertEqual(again.get_temperature("Quarto"), 20.0)


class TestHybridSource(RoomsTestCase):
    def test_real_only_by_default(self):
        src = HybridSource(self.s.model_copy(update={"simulate": False, "sim_rooms": False}), client=FakeMcp(),
                           config=self.cfg)
        self.assertEqual(src.name, "mcp")
        self.assertIsNone(src.sim)
        names = {r.name for r in normalize(src.fetch(), self.s).rooms.values()}
        self.assertNotIn("Cozinha", names)

    def test_real_plus_simulated(self):
        mcp = FakeMcp()
        src = HybridSource(self.s.model_copy(update={"simulate": False, "sim_rooms": True}), client=mcp, config=self.cfg)
        self.assertEqual(src.name, "mcp+sim")
        st = normalize(src.fetch(), self.s)
        self.assertEqual([r.name for r in st.rooms.values()], ["Sala", "Quarto", "Cozinha"])
        self.assertEqual(len(st.devices), 11)
        self.assertEqual(st.by_role("contact", "Sala").source, "mcp")
        self.assertEqual(st.by_role("contact", "Cozinha").source, "sim")
        self.assertEqual({t for t, _ in mcp.calls}, {"get_home", "get_home_security_alarm_history"})

    def test_simulated_alarm_is_merged(self):
        src = HybridSource(self.s.model_copy(update={"simulate": False, "sim_rooms": True}), client=FakeMcp(), config=self.cfg)
        a = normalize(src.fetch(), self.s)
        self.home.smoke(room="Cozinha")
        ev = next(e for e in diff(a, normalize(src.fetch(), self.s), self.s) if e.type == "smoke_detected")
        self.assertEqual(ev.room_name, "Cozinha")
        self.assertFalse(ev.data["room_inferred"])

    def test_simulate_true_is_all_sim(self):
        src = HybridSource(self.s, client=FakeMcp(), config=self.cfg)
        self.assertEqual(src.name, "sim")
        self.assertIsNone(src.mcp)


class TestRouteWrite(RoomsTestCase):
    def setUp(self):
        super().setUp()
        self.mcp = FakeMcp()
        src = HybridSource(self.s.model_copy(update={"simulate": False, "sim_rooms": True}), client=self.mcp, config=self.cfg)
        self.state = normalize(src.fetch(), self.s)
        self.mcp.calls.clear()

    def test_unknown_device_rejected(self):
        with self.assertRaises(UnknownDevice):
            route_write(self.state, "inventado", {"type": "power", "value": "on"}, self.s, mcp=self.mcp)

    def test_dry_run_writes_nothing(self):
        plug = self.state.by_role("plug", "Quarto")
        out = route_write(self.state, plug.id, {"type": "power", "value": "off"},
                          self.s.model_copy(update={"dry_run": True}), mcp=self.mcp)
        self.assertFalse(out["executed"])
        self.assertEqual(out["source"], "mcp")
        self.assertEqual(self.mcp.calls, [])

    def test_mcp_device_goes_to_mcp(self):
        plug = self.state.by_role("plug", "Quarto")
        body = {"type": "power", "value": "off"}
        out = route_write(self.state, plug.id, body, self.s, mcp=self.mcp, dry_run=False)
        self.assertTrue(out["executed"])
        self.assertEqual(self.mcp.calls, [("update_device_capabilities", {"deviceId": plug.id, "body": body})])

    def test_sim_device_goes_to_simhome(self):
        out = route_write(self.state, "sim-cozinha-plug", {"type": "power", "value": "on"}, self.s,
                          mcp=self.mcp, sim=self.home, dry_run=False)
        self.assertTrue(out["executed"])
        self.assertEqual(self.mcp.calls, [])
        self.assertEqual(self.snap().cap("plug", "power", room="Cozinha"), "on")


if __name__ == "__main__":
    unittest.main()
