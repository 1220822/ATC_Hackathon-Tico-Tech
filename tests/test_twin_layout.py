import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from src.config import ROOT, get_settings
from src.perception.state import normalize
from src.twin.layout import Layout, LayoutError, auto_layout, load_layout, resolve_layout, rooms_from_get_home

CAPS = ROOT / "docs" / "capabilities"
SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
LIGHT = "b667d87c-2566-440a-afce-02d9ec82df1b"
CONTACT = "20d98aec-c266-4707-bd27-d06bb51733b7"
PLUG = "a82edb80-d92f-44bb-8df0-8e10beeea621"


def dump_home():
    return json.loads((CAPS / "get_home.json").read_text(encoding="utf-8"))["data"]


def dump_state(home=None, **raw):
    s = get_settings().model_copy(update={"home_config_file": ROOT / "nao-existe.json"})
    return normalize({"home": home or dump_home(), **raw}, s, now=1794997700)


class TestAutoLayout(unittest.TestCase):
    def setUp(self):
        self.layout = auto_layout(rooms_from_get_home(dump_home()))

    def test_rooms_side_by_side(self):
        self.assertEqual(list(self.layout.rooms), [SALA, QUARTO])
        sala, quarto = self.layout.rooms[SALA], self.layout.rooms[QUARTO]
        self.assertEqual(quarto.x, sala.x + sala.width)
        self.assertNotIn("west", [w.side for w in quarto.walls])  # shared wall drawn once

    def test_typical_positions(self):
        d = self.layout.devices
        self.assertEqual(len(d), 7)
        self.assertEqual(d[LIGHT].mount, "ceiling")
        self.assertEqual(d[LIGHT].room_id, QUARTO)
        self.assertEqual(d[CONTACT].mount, "window")
        self.assertEqual(d[CONTACT].opening, "janela-sala")
        cam = next(v for v in d.values() if v.name == "Camera")
        self.assertEqual(cam.mount, "corner")
        self.assertIsNotNone(cam.target)
        smoke = next(v for v in d.values() if v.name == "Smoke sensor")
        self.assertEqual((smoke.mount, smoke.room_id), ("ceiling", SALA))

    def test_repo_layout_covers_every_mcp_device(self):
        layout = load_layout(ROOT / "config" / "layout.json")
        self.assertEqual(set(layout.rooms), {SALA, QUARTO})
        self.assertEqual(set(layout.devices), {d["id"] for r in dump_home()["rooms"] for d in r["devices"]})

    def test_init_script_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "layout.json"
            cmd = [sys.executable, str(ROOT / "scripts" / "layout_init.py"), "--from-dump", "--out", str(out)]
            self.assertEqual(subprocess.run(cmd, capture_output=True).returncode, 0)
            out.write_text(out.read_text(encoding="utf-8").replace('"Sala"', '"Sala editada"'), encoding="utf-8")
            again = subprocess.run(cmd, capture_output=True)
            self.assertNotEqual(again.returncode, 0)
            self.assertIn("Sala editada", out.read_text(encoding="utf-8"))
            self.assertEqual(subprocess.run(cmd + ["--force"], capture_output=True).returncode, 0)
            self.assertNotIn("Sala editada", out.read_text(encoding="utf-8"))


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.base = auto_layout(rooms_from_get_home(dump_home())).model_dump()

    def assert_invalid(self, mutate):
        data = copy.deepcopy(self.base)
        mutate(data)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "layout.json"
            p.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(LayoutError):
                load_layout(p)

    def test_device_outside_room(self):
        self.assert_invalid(lambda d: d["devices"][LIGHT].update(position=[2.5, 9.0, 2.0]))

    def test_unknown_room(self):
        self.assert_invalid(lambda d: d["devices"][LIGHT].update(room_id="cozinha"))

    def test_unknown_opening(self):
        self.assert_invalid(lambda d: d["devices"][CONTACT].update(opening="janela-x"))

    def test_opening_outside_wall(self):
        self.assert_invalid(lambda d: d["rooms"][SALA]["walls"][0]["openings"][0].update(offset=4.8))

    def test_duplicate_opening_ids(self):
        def dup(d):
            d["rooms"][QUARTO]["walls"][0]["openings"].append(
                {"id": "janela-sala", "type": "window", "offset": 2, "width": 1, "height": 1, "sill": 1})
        self.assert_invalid(dup)

    def test_unknown_key(self):
        self.assert_invalid(lambda d: d["devices"][LIGHT].update(colour="red"))

    def test_missing_file_is_empty(self):
        self.assertEqual(load_layout(ROOT / "nao-existe.json"), Layout())


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.layout = auto_layout(rooms_from_get_home(dump_home()))

    def test_all_placed_from_layout(self):
        out = resolve_layout(self.layout, dump_state())
        self.assertEqual([r["name"] for r in out["rooms"]], ["Sala", "Quarto"])
        self.assertEqual(len(out["devices"]), 7)
        self.assertEqual(out["warnings"], [])
        self.assertTrue(all(d["placed"] == "layout" for d in out["devices"].values()))
        self.assertEqual(out["devices"][LIGHT]["position"], [7.5, 2.52, 2.0])  # Quarto starts at x 5

    def test_device_missing_from_layout_gets_default(self):
        layout = self.layout.model_copy(deep=True)
        del layout.devices[PLUG]
        out = resolve_layout(layout, dump_state())
        plug = out["devices"][PLUG]
        self.assertEqual(plug["placed"], "default")
        self.assertEqual(plug["room"], QUARTO)
        self.assertEqual(plug["position"], [7.5, 1.0, 2.0])  # centre of the Quarto
        self.assertTrue(any("Plug" in w and "não está no layout" in w for w in out["warnings"]))

    def test_room_missing_from_layout_is_added(self):
        layout = self.layout.model_copy(deep=True)
        del layout.rooms[QUARTO]
        layout.devices = {k: v for k, v in layout.devices.items() if v.room_id != QUARTO}
        out = resolve_layout(layout, dump_state())
        quarto = next(r for r in out["rooms"] if r["id"] == QUARTO)
        self.assertGreaterEqual(quarto["x"], 5.0)
        self.assertEqual(len(out["devices"]), 7)
        self.assertTrue(any("Quarto" in w for w in out["warnings"]))

    def test_mcp_room_wins_over_layout(self):
        home = dump_home()
        sala, quarto = home["rooms"]
        plug = next(d for d in quarto["devices"] if d["id"] == PLUG)
        quarto["devices"].remove(plug)
        sala["devices"].append(plug)  # moved in the Altice app
        out = resolve_layout(self.layout, dump_state(home))
        self.assertEqual(out["devices"][PLUG]["room"], SALA)
        self.assertEqual(out["devices"][PLUG]["placed"], "default")
        self.assertTrue(any("MCP diz Sala" in w for w in out["warnings"]))

    def test_simulated_rooms_are_not_in_the_twin(self):
        home = dump_home()
        home["rooms"].append({"id": "sim-cozinha", "name": "Cozinha", "type": "kitchen", "devices": [
            {"id": "sim-cozinha-plug", "name": "Tomada", "type": "plug", "capabilities": [],
             "lastCommunicationTime": 1794997600000}]})
        out = resolve_layout(self.layout, dump_state(home, sim_rooms=["sim-cozinha"]))
        self.assertEqual([r["name"] for r in out["rooms"]], ["Sala", "Quarto"])
        self.assertNotIn("sim-cozinha-plug", out["devices"])
        self.assertEqual(out["warnings"], [])


if __name__ == "__main__":
    unittest.main()
