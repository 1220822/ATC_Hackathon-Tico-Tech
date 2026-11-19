import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import create_app
from src.config import get_settings
from src.memory import SharedMemory
from src.perception.sources import SimSource
from src.perception.state import normalize
from src.storage.history import History

SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"
CONFIG = {"simulated_rooms": [{"name": "Cozinha", "type": "kitchen",
                               "devices": ["temperature", "contact", "plug"]}]}


class TestApi(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        (tmp / "home.json").write_text(json.dumps(CONFIG), encoding="utf-8")
        self.s = get_settings().model_copy(update={
            "simulate": True, "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "home.json",
            "history_db": tmp / "history.db"})
        self.source = SimSource(self.s)
        self.source.home.set_plug(True, room="Quarto")
        self.source.home.set_device_online("bulb", False)
        self.memory = SharedMemory()
        self.client = TestClient(create_app(self.memory, self.s))
        self.memory.update_state(normalize(self.source.fetch(), self.s))
        base = 1_800_000_000
        History(self.s.history_db).insert(
            [(base + i * 600, QUARTO, "t1", "temperature", 20.0 + i / 10) for i in range(12)])
        self.base = base

    def tearDown(self):
        self.client.close()
        self.tmp.cleanup()

    def test_ui_files_are_revalidated(self):
        r = self.client.get("/twin/ui/twin.js")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-cache")

    def test_503_before_first_poll(self):
        self.memory.update_state(None)
        self.assertEqual(self.client.get("/rooms").status_code, 503)

    def test_rooms(self):
        rooms = {r["nome"]: r for r in self.client.get("/rooms").json()}
        self.assertEqual(set(rooms), {"Sala", "Quarto", "Cozinha"})
        self.assertTrue(rooms["Cozinha"]["simulado"])
        self.assertFalse(rooms["Sala"]["simulado"])
        self.assertEqual(rooms["Quarto"]["offline"], ["Light"])
        self.assertFalse(rooms["Quarto"]["online"])
        self.assertTrue(rooms["Quarto"]["sensores"]["aquecedor_ligado"])
        self.assertEqual(len(rooms["Sala"]["dispositivos"]), 4)

    def test_room_by_id_or_name(self):
        self.assertEqual(self.client.get(f"/rooms/{QUARTO}").json()["nome"], "Quarto")
        self.assertEqual(self.client.get("/rooms/cozinha").json()["id"], "sim-cozinha")
        self.assertEqual(self.client.get("/rooms/Garagem").status_code, 404)

    def test_status(self):
        status = {r["nome"]: r for r in self.client.get("/rooms/status").json()}
        light = next(d for d in status["Quarto"]["dispositivos"] if d["papel"] == "light")
        self.assertFalse(light["online"])
        self.assertTrue(status["Sala"]["online"])

    def test_historic(self):
        r = self.client.get(f"/historic/rooms/Quarto/temperature?start={self.base}&end={self.base + 1800}")
        self.assertEqual([x["value"] for x in r.json()["leituras"]], [20.0, 20.1, 20.2])
        self.assertEqual(self.client.get("/historic/rooms/Quarto/pressao").status_code, 400)

    def test_historic_aggregated(self):
        r = self.client.get(f"/historic/aggregated/max/rooms/{QUARTO}/temperature"
                            f"?period_s=3600&start={self.base}&end={self.base + 7200}").json()
        self.assertEqual([round(b["value"], 1) for b in r["buckets"]], [20.5, 21.1])
        self.assertEqual(self.client.get("/historic/aggregated/median/rooms/Quarto/temperature").status_code, 400)
        self.assertEqual(self.client.get("/historic/aggregated/avg/rooms/Quarto/temperature?period_s=0")
                         .status_code, 422)

    def test_energy(self):
        e = self.client.get("/home/energy").json()
        by_room = {x["nome"]: x for x in e["divisoes"]}
        self.assertEqual(set(by_room), {"Quarto", "Cozinha"})
        self.assertEqual(by_room["Quarto"]["potencia_w"], 1500)
        self.assertEqual(e["potencia_total_w"], 1500)
        self.assertAlmostEqual(e["consumo_ontem_kwh"], 0.004)

    def test_ui(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("simulado", r.text)


if __name__ == "__main__":
    unittest.main()
