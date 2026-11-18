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
FIXTURES = ROOT / "tests" / "fixtures"


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


SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"


def load_dump(name, folder=CAPS):
    return json.loads((folder / name).read_text(encoding="utf-8"))["data"]


class TestRealDump(unittest.TestCase):
    """Dump of 2026-11-18: devices live in rooms[].devices."""

    def setUp(self):
        self.s = get_settings()
        hist = load_dump("get_home_security_alarm_history.json")
        self.state = normalize({"home": load_dump("get_home.json"), "alarm_history": hist["history"]},
                               self.s, now=1794997700)

    def test_rooms_and_devices(self):
        st = self.state
        self.assertEqual(len(st.devices), 7)
        self.assertEqual({r.name for r in st.rooms.values()}, {"Sala", "Quarto"})
        self.assertEqual(st.rooms[SALA].type, "livingRoom")
        self.assertEqual(st.rooms[QUARTO].type, "bedroom")
        self.assertEqual(len(st.rooms[SALA].device_ids), 4)
        self.assertEqual(len(st.rooms[QUARTO].device_ids), 3)
        # sensors of scenario 1 are in different rooms
        self.assertEqual(st.by_role("contact").room_name, "Sala")
        self.assertEqual(st.by_role("temperature").room_id, QUARTO)
        self.assertEqual(st.by_role("plug").room_name, "Quarto")

    def test_values(self):
        st = self.state
        self.assertEqual(st.security_status, "DISARMED")
        self.assertTrue(st.contact_open)  # sensorState == "alarm" in the dump
        self.assertAlmostEqual(st.temperature, 20.9)
        self.assertAlmostEqual(st.humidity, 71.1)
        self.assertTrue(st.plug_on)
        self.assertTrue(all(d.online for d in st.devices.values()))
        self.assertEqual(st.alarms[1].detail_type, "EMERGENCY")
        self.assertAlmostEqual(st.energy.yesterday_consumption_kwh, 0.004)

    def test_room_filters(self):
        st = self.state
        self.assertIsNone(st.by_role("temperature", room="Sala"))
        self.assertEqual(st.cap("temperature", "temperatureValue", room="quarto"), 20.9)
        self.assertEqual(st.cap("contact", "sensorState", room=SALA), "alarm")
        self.assertEqual(len(st.devices_by_role("plug")), 1)
        self.assertEqual(st.devices_by_role("plug", room="Cozinha"), [])

    def test_summary_has_rooms(self):
        rooms = self.state.summary()["divisoes"]
        self.assertEqual(set(rooms), {"Sala", "Quarto"})
        self.assertTrue(rooms["Sala"]["janela_aberta"])
        self.assertNotIn("temperatura_c", rooms["Sala"])
        self.assertAlmostEqual(rooms["Quarto"]["temperatura_c"], 20.9)
        self.assertEqual(len(rooms["Sala"]["dispositivos"]) + len(rooms["Quarto"]["dispositivos"]), 7)


class TestOldFlatFormat(unittest.TestCase):
    """Dump of 2026-11-17: flat top-level devices, roomName = home name, no rooms."""

    def test_fallback(self):
        s = get_settings()
        state = normalize({"home": load_dump("get_home_flat_2026-11-17.json", FIXTURES)}, s,
                          now=1794933600)
        self.assertEqual(len(state.devices), 7)
        self.assertEqual(len(state.rooms), 1)
        self.assertEqual(next(iter(state.rooms.values())).name, "Hack 7")
        self.assertEqual(state.by_role("temperature").room_name, "Hack 7")
        self.assertTrue(state.contact_open)
        self.assertAlmostEqual(state.temperature, 21.6)
        self.assertTrue(state.plug_on)
        self.assertFalse(state.by_role("light").online)  # silent since 2026-11-04
        self.assertTrue(state.by_role("contact").online)

    def test_sim_resets_old_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sim.json"
            path.write_text(json.dumps({"home": load_dump("get_home_flat_2026-11-17.json", FIXTURES),
                                        "alarm_history": []}), encoding="utf-8")
            data = SimHome(path).snapshot()
            self.assertIn("rooms", data["home"])
            self.assertNotIn("devices", data["home"])
            self.assertEqual(len(normalize(data, get_settings()).devices), 7)


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

    def test_events_carry_room(self):
        a = self.snap()
        self.home.set_contact(True)
        self.home.set_temperature(20.4)
        ev = {e.type: e for e in diff(a, self.snap(), self.s)}
        self.assertEqual(ev["contact_opened"].room, SALA)
        self.assertEqual(ev["contact_opened"].room_name, "Sala")
        self.assertEqual(ev["contact_opened"].message, "Janela da Sala aberta.")
        self.assertEqual(ev["temperature_changed"].room, QUARTO)
        self.assertEqual(ev["temperature_changed"].message, "Temperatura no Quarto: 21,0 °C → 20,4 °C.")

    def test_smoke_without_device_goes_to_only_smoke_sensor(self):
        a = self.snap()
        self.home.smoke()  # real behaviour: EMERGENCY with an empty device
        ev = next(e for e in diff(a, self.snap(), self.s) if e.type == "smoke_detected")
        self.assertEqual(ev.room_name, "Sala")
        self.assertTrue(ev.data["room_inferred"])
        self.assertIn("na Sala", ev.message)

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
        self.assertEqual(out[0].room_name, "Quarto")

    def test_scenario1_across_rooms(self):
        """Window (Sala) open + heater (Quarto) on + Quarto temperature falling."""
        trend = TemperatureTrend(self.s)
        now = time.time()
        self.home.set_plug(True)
        self.home.set_contact(True)
        out = []
        for i, t in enumerate([21.5, 21.3, 21.1, 20.9]):
            self.home.set_temperature(t)
            out += trend.update(normalize(self.home.snapshot(), self.s, now + i * 10))
        self.assertEqual(len(out), 1)
        d = out[0].data
        self.assertTrue(d["window_open"])
        self.assertTrue(d["heater_on"])
        self.assertEqual(d["open_windows"], ["Sala"])
        self.assertEqual(d["heaters_on"], ["Quarto"])
        self.assertEqual(out[0].message, "A temperatura no Quarto está a descer: 21,5 °C → 20,9 °C "
                                         "em 10 min (janela aberta na Sala; aquecedor ligado no Quarto).")

    def test_one_track_per_sensor(self):
        """A second temperature sensor in another room has its own trend."""
        raw = self.home.snapshot()
        sala = next(r for r in raw["home"]["rooms"] if r["id"] == SALA)
        clone = json.loads(json.dumps(next(d for r in raw["home"]["rooms"] for d in r["devices"]
                                           if d["type"] == "temperatureSensor")))
        clone.update(id="sala-temp", roomName="Sala")
        sala["devices"].append(clone)
        self.home.save(raw)
        trend = TemperatureTrend(self.s)
        now = time.time()
        out = []
        for i, t in enumerate([21.5, 21.2, 20.9]):  # set_temperature hits the first sensor: the Sala clone
            self.home.set_temperature(t)
            out += trend.update(normalize(self.home.snapshot(), self.s, now + i * 10))
        self.assertEqual([e.room_name for e in out], ["Sala"])
        self.assertEqual(set(trend.tracks), {"sala-temp", self.snap().by_role("temperature", "Quarto").id})


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
