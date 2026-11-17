import json
import tempfile
import time
import unittest
from pathlib import Path

from src.config import ROOT, get_settings
from src.memory import EventBus, SharedMemory
from src.perception.poller import Perception
from src.perception.sources import SimSource
from src.perception.state import TemperatureTrend, diff, normalize
from src.sim.home import SimHome

CAPS = ROOT / "docs" / "capabilities"


def types(events):
    return [e.type for e in events]


class SimTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = get_settings().model_copy(update={
            "sim_state_file": Path(self.tmp.name) / "sim.json", "tick_interval_s": 1e9})
        self.home = SimHome(self.s.sim_state_file)
        self.home.reset()

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self):
        return normalize(self.home.snapshot(), self.s)


class TestRealDump(unittest.TestCase):
    def test_normalize_real_account(self):
        s = get_settings()
        home = json.loads((CAPS / "get_home.json").read_text(encoding="utf-8"))["data"]
        hist = json.loads((CAPS / "get_home_security_alarm_history.json").read_text(encoding="utf-8"))["data"]
        state = normalize({"home": home, "alarm_history": hist["history"]}, s, now=1794933600)
        self.assertEqual(state.security_status, "DISARMED")
        self.assertTrue(state.contact_open)  # sensorState == "alarm" in the dump
        self.assertAlmostEqual(state.temperature, 21.6)
        self.assertTrue(state.plug_on)
        self.assertFalse(state.by_role("light").online)  # silent since 2026-11-04
        self.assertTrue(state.by_role("contact").online)
        self.assertEqual(state.alarms[1].detail_type, "EMERGENCY")


class TestDiff(SimTestCase):
    def test_first_snapshot_has_no_events(self):
        self.assertEqual(diff(None, self.snap(), self.s), [])

    def test_contact_open_disarmed(self):
        a = self.snap()
        self.home.set_contact(True)
        ev = diff(a, self.snap(), self.s)
        self.assertEqual(types(ev), ["contact_opened"])
        self.assertFalse(ev[0].data["armed"])
        self.assertEqual(ev[0].severity, "info")

    def test_contact_open_while_armed_is_intrusion(self):
        self.home.set_security("away")
        a = self.snap()
        self.home.set_contact(True)
        ev = diff(a, self.snap(), self.s)
        t = types(ev)
        self.assertIn("contact_opened", t)
        self.assertIn("security_alarm", t)
        opened = next(e for e in ev if e.type == "contact_opened")
        self.assertTrue(opened.data["armed"])
        self.assertEqual(opened.severity, "warning")
        alarm = next(e for e in ev if e.type == "security_alarm")
        self.assertEqual(alarm.data["kind"], "intrusion")

    def test_smoke_detected_once(self):
        a = self.snap()
        self.home.smoke()
        b = self.snap()
        ev = diff(a, b, self.s)
        self.assertIn("smoke_detected", types(ev))
        self.assertEqual(next(e for e in ev if e.type == "smoke_detected").severity, "critical")
        self.assertNotIn("smoke_detected", types(diff(b, self.snap(), self.s)))
        self.home.cancel_alarm()
        self.assertIn("alarm_cancelled", types(diff(b, self.snap(), self.s)))

    def test_temperature_and_plug(self):
        a = self.snap()
        self.home.set_temperature(20.5)
        self.home.set_plug(True)
        t = types(diff(a, self.snap(), self.s))
        self.assertIn("temperature_changed", t)
        self.assertIn("plug_switched", t)
        self.assertIn("plug_power_changed", t)

    def test_small_temperature_change_ignored(self):
        a = self.snap()
        self.home.set_temperature(21.1)
        self.assertEqual(diff(a, self.snap(), self.s), [])

    def test_device_offline(self):
        a = self.snap()
        self.home.set_device_online("bulb", False)
        self.assertEqual(types(diff(a, self.snap(), self.s)), ["device_offline"])

    def test_sim_light_write(self):
        a = self.snap()
        light = a.by_role("light")
        self.home.apply_capability(light.id, {"type": "color_rgb", "redValue": 255,
                                              "greenValue": 140, "blueValue": 0})
        b = self.snap()
        self.assertEqual(b.cap("light", "color_rgb"), "255,140,0")
        self.assertEqual(types(diff(a, b, self.s)), ["light_changed"])

    def test_sim_light_colour_scaled_by_brightness(self):
        # Mirrors the real Light (2026-11-17): 255,140,0 at brightness 40 reads back 102,56,0.
        light = self.snap().by_role("light")
        self.home.apply_capability(light.id, {"type": "brightness", "value": 40})
        self.home.apply_capability(light.id, {"type": "color_rgb", "redValue": 255,
                                              "greenValue": 140, "blueValue": 0})
        s = self.snap()
        self.assertEqual(s.cap("light", "color_rgb"), "102,56,0")
        self.assertEqual(s.light_hue_rgb, (255, 140, 0))
        self.home.apply_capability(light.id, {"type": "brightness", "value": 100})
        self.assertEqual(self.snap().cap("light", "color_rgb"), "255,140,0")


class TestTrend(SimTestCase):
    def test_drop_emits_once(self):
        trend = TemperatureTrend(self.s)
        now = time.time()
        out = []
        for i, t in enumerate([21.5, 21.3, 21.1, 20.9, 20.7, 20.5]):
            self.home.set_temperature(t)
            out += trend.update(normalize(self.home.snapshot(), self.s, now + i * 10))
        self.assertEqual(types(out), ["temperature_dropping"])
        self.assertAlmostEqual(out[0].data["drop"], 0.6, places=2)


class TestPerceptionLoop(SimTestCase):
    def test_step_publishes_and_stores(self):
        bus, mem = EventBus(), SharedMemory()
        got = []
        bus.subscribe(got.append, types=["contact_opened"])
        p = Perception(SimSource(self.s), bus, mem, self.s)
        p.step()
        self.home.set_contact(True)
        self.home.set_temperature(19.0)
        p.step()
        self.assertEqual(types(got), ["contact_opened"])
        self.assertTrue(mem.state.contact_open)
        self.assertIn("temperature_changed", types(mem.recent_events()))


if __name__ == "__main__":
    unittest.main()
