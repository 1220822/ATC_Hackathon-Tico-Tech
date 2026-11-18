import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.config import get_settings
from src.memory import SharedMemory
from src.perception.state import normalize
from src.sim.home import SimHome
from src.storage.history import History, HistoryRecorder, readings_from_state
from src.storage.tools import HistoryTools

SALA = "e50aa04b-3ca3-443e-9d54-3e504bc7d7a1"
QUARTO = "bd792398-7e60-40e9-9b62-11c4255f6c72"


class HistoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.s = get_settings().model_copy(update={
            "sim_state_file": tmp / "sim.json", "home_config_file": tmp / "no-config.json",
            "history_db": tmp / "history.db"})
        self.home = SimHome(self.s.sim_state_file)
        self.home.reset()
        self.h = History(self.s.history_db)

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self, now=None):
        return normalize(self.home.snapshot(), self.s, now)


class TestRecord(HistoryTestCase):
    def test_metrics_per_room(self):
        self.home.set_contact(True)
        self.home.set_plug(True)
        rows = {(room, metric): v for _, room, _, metric, v in readings_from_state(self.snap())}
        self.assertEqual(rows, {
            (SALA, "contact"): 1.0,
            (QUARTO, "temperature"): 21.0, (QUARTO, "humidity"): 60.0,
            (QUARTO, "power"): 1500.0, (QUARTO, "light"): 1.0,
        })

    def test_offline_devices_skipped(self):
        self.home.set_device_online("bulb", False)
        metrics = {m for *_, m, _ in readings_from_state(self.snap())}
        self.assertNotIn("light", metrics)

    def test_recorder_writes_once_per_state(self):
        mem = SharedMemory()
        rec = HistoryRecorder(mem, self.h, 30)
        self.assertEqual(rec.step(), 0)  # no state yet
        mem.update_state(self.snap())
        self.assertEqual(rec.step(), 5)
        self.assertEqual(rec.step(), 0)  # same state, not stored again
        self.assertEqual(len(self.h.query(QUARTO, "temperature")), 1)


class TestQueries(HistoryTestCase):
    def setUp(self):
        super().setUp()
        base = 1_800_000_000  # multiple of 3600
        self.base = base
        # temperature every 10 min for 2 h: 20.0, 20.1, ... ; power 1000 W in hour 1, 0 W in hour 2
        rows = []
        for i in range(12):
            ts = base + i * 600
            rows.append((ts, QUARTO, "t1", "temperature", 20.0 + i / 10))
            rows.append((ts, QUARTO, "p1", "power", 1000.0 if i < 6 else 0.0))
        self.h.insert(rows)

    def test_query_range(self):
        got = self.h.query(QUARTO, "temperature", self.base, self.base + 1800)
        self.assertEqual([r["value"] for r in got], [20.0, 20.1, 20.2])
        self.assertEqual(self.h.query(SALA, "temperature", self.base, self.base + 7200), [])

    def test_aggregate(self):
        b = self.base
        avg = self.h.aggregate(QUARTO, "temperature", 3600, "avg", b, b + 7200)
        self.assertEqual([r["ts"] for r in avg], [b, b + 3600])
        self.assertAlmostEqual(avg[0]["value"], 20.25)
        self.assertEqual(avg[0]["n"], 6)
        self.assertAlmostEqual(self.h.aggregate(QUARTO, "temperature", 3600, "max", b, b + 7200)[1]["value"], 21.1)
        self.assertAlmostEqual(self.h.aggregate(QUARTO, "temperature", 3600, "min", b, b + 7200)[1]["value"], 20.6)
        self.assertEqual([r["value"] for r in self.h.aggregate(QUARTO, "power", 3600, "sum", b, b + 7200)],
                         [6000.0, 0.0])
        # buckets are floor(ts / period) * period
        self.assertEqual(len(self.h.aggregate(QUARTO, "temperature", 1800, "avg", b, b + 7200)), 4)

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            self.h.aggregate(QUARTO, "temperature", 3600, "median")
        with self.assertRaises(ValueError):
            self.h.aggregate(QUARTO, "temperature; DROP TABLE readings", 3600)
        with self.assertRaises(ValueError):
            self.h.aggregate(QUARTO, "temperature", 0)

    def test_readonly_cannot_write(self):
        ro = History(self.s.history_db, readonly=True)
        self.assertEqual(len(ro.query(QUARTO, "power", self.base, self.base + 7200)), 12)
        with self.assertRaises(sqlite3.OperationalError):
            ro.insert([(time.time(), QUARTO, "x", "power", 1.0)])


class TestAgentTools(HistoryTestCase):
    def setUp(self):
        super().setUp()
        now = time.time()
        hour = now // 3600 * 3600
        rows = []
        for i in range(13):  # last 2 hours, every 10 min: temperature falling 0.1 °C per sample
            ts = now - 7200 + i * 600
            rows.append((ts, QUARTO, "t1", "temperature", 21.0 - i * 0.1))
            rows.append((ts, QUARTO, "p1", "power", 1500.0 if ts < hour else 500.0))
        self.h.insert(rows)
        state = self.snap()
        self.tools = HistoryTools(self.s.history_db, lambda: state, interval_s=600)

    def test_schemas_per_agent(self):
        self.assertEqual([t["function"]["name"] for t in self.tools.schemas("energia")], ["consumo_por_hora"])
        self.assertEqual([t["function"]["name"] for t in self.tools.schemas("conforto")], ["tendencia_temperatura"])

    def test_consumo_por_hora(self):
        out = self.tools.call("consumo_por_hora", json.dumps({"horas": 3}))
        self.assertEqual(list(out["divisoes"]), ["Quarto"])  # only room with a plug
        horas = out["divisoes"]["Quarto"]["horas"]
        self.assertAlmostEqual(horas[-2]["kwh"], 1.5)  # full past hour at 1500 W
        self.assertLessEqual(horas[-1]["kwh"], 0.5)  # current hour, partial

    def test_gaps_are_not_consumption(self):
        # recorder every 30 s: samples 600 s apart count only 60 s each
        tools = HistoryTools(self.s.history_db, self.tools.state, interval_s=30)
        horas = tools.call("consumo_por_hora", {"horas": 3})["divisoes"]["Quarto"]["horas"]
        self.assertAlmostEqual(horas[-2]["kwh"], 1500 * 6 * 60 / 3.6e6, places=4)

    def test_tendencia_temperatura(self):
        out = self.tools.call("tendencia_temperatura", {"divisao": "quarto", "horas": 3})
        self.assertEqual(out["divisao_id"], QUARTO)
        self.assertAlmostEqual(out["tendencia_c_por_hora"], -0.6, places=2)
        self.assertAlmostEqual(out["atual_c"], 19.8)
        self.assertEqual(out["setpoint_c"], self.s.setpoint_c)

    def test_errors_are_returned(self):
        self.assertIn("erro", self.tools.call("tendencia_temperatura", {"divisao": "Garagem"}))
        self.assertIn("erro", self.tools.call("apagar_tudo", {}))


if __name__ == "__main__":
    unittest.main()
