"""History of numeric readings in SQLite (stdlib sqlite3, file var/history.db).

    readings(ts REAL, room TEXT, device_id TEXT, metric TEXT, value REAL)
    index (room, metric, ts)

`room` is the room id. Metrics (one row per device and metric, online devices only):
    temperature  °C          (temperatureSensor.temperatureValue)
    humidity     %           (temperatureSensor.humidityValue)
    power        W           (plug.instantPower)
    contact      1 = open    (contactSensor.sensorState == "alarm")
    light        1 = on      (bulb.power == "on")

A HistoryRecorder thread writes the current HomeState every STORE_INTERVAL_S seconds (30 by
default). Queries open their own connection, so the API and the agents can read while the
recorder writes (WAL mode). Agent-facing functions open the file read-only (see tools.py).
"""

import logging
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable

from ..perception.state import HomeState, _num

log = logging.getLogger(__name__)

METRICS = ("temperature", "humidity", "power", "contact", "light")
AGGS = {"avg": "AVG", "sum": "SUM", "min": "MIN", "max": "MAX"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts REAL NOT NULL, room TEXT NOT NULL, device_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_room_metric_ts ON readings (room, metric, ts);
"""


def readings_from_state(state: HomeState) -> list[tuple[float, str, str, str, float]]:
    """(ts, room, device_id, metric, value) rows for the numeric values of a state."""
    rows = []
    for d in state.devices.values():
        if not d.online:
            continue
        values: list[tuple[str, float | None]] = []
        if d.role == "temperature":
            values += [("temperature", _num(d.caps.get("temperatureValue"))),
                       ("humidity", _num(d.caps.get("humidityValue")))]
        elif d.role == "plug":
            values.append(("power", _num(d.caps.get("instantPower"))))
        elif d.role == "contact" and d.caps.get("sensorState") is not None:
            values.append(("contact", 1.0 if d.caps.get("sensorState") == "alarm" else 0.0))
        elif d.role == "light" and d.caps.get("power") is not None:
            values.append(("light", 1.0 if d.caps.get("power") == "on" else 0.0))
        rows += [(state.ts, d.room_id, d.id, m, v) for m, v in values if v is not None]
    return rows


class History:
    def __init__(self, path: Path | str, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db, db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
        return sqlite3.connect(self.path, timeout=5)

    # -- write ---------------------------------------------------------------
    def insert(self, rows: Iterable[tuple[float, str, str, str, float]]) -> int:
        rows = list(rows)
        if rows:
            with closing(self._connect()) as db, db:
                db.executemany("INSERT INTO readings (ts, room, device_id, metric, value) "
                               "VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def record(self, state: HomeState) -> int:
        return self.insert(readings_from_state(state))

    # -- read ----------------------------------------------------------------
    def query(self, room: str, metric: str, start: float | None = None,
              end: float | None = None) -> list[dict]:
        """Raw readings of one room and metric, oldest first: [{ts, device_id, value}]."""
        _check_metric(metric)
        start, end = _range(start, end)
        with closing(self._connect()) as db:
            cur = db.execute("SELECT ts, device_id, value FROM readings WHERE room = ? AND metric = ? "
                             "AND ts >= ? AND ts < ? ORDER BY ts", (room, metric, start, end))
            return [{"ts": ts, "device_id": dev, "value": v} for ts, dev, v in cur]

    def aggregate(self, room: str, metric: str, period_s: float, agg: str = "avg",
                  start: float | None = None, end: float | None = None) -> list[dict]:
        """Buckets of floor(ts / period_s) * period_s: [{ts (bucket start), value, n}]."""
        _check_metric(metric)
        if agg not in AGGS:
            raise ValueError(f"agg tem de ser um de {sorted(AGGS)}")
        if not period_s or period_s <= 0:
            raise ValueError("period_s tem de ser > 0")
        start, end = _range(start, end)
        sql = (f"SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, {AGGS[agg]}(value), COUNT(*) "
               "FROM readings WHERE room = ? AND metric = ? AND ts >= ? AND ts < ? "
               "GROUP BY bucket ORDER BY bucket")
        with closing(self._connect()) as db:
            cur = db.execute(sql, (period_s, period_s, room, metric, start, end))
            return [{"ts": float(b), "value": v, "n": n} for b, v, n in cur]

    def rooms(self) -> list[str]:
        with closing(self._connect()) as db:
            return [r for (r,) in db.execute("SELECT DISTINCT room FROM readings ORDER BY room")]


def _check_metric(metric: str) -> None:
    if metric not in METRICS:
        raise ValueError(f"métrica desconhecida {metric!r}; usa uma de {list(METRICS)}")


def _range(start: float | None, end: float | None) -> tuple[float, float]:
    end = time.time() + 1 if end is None else float(end)
    start = end - 24 * 3600 if start is None else float(start)
    return start, end


class HistoryRecorder:
    """Every interval_s, stores the latest state from SharedMemory (skips if it has not changed)."""

    def __init__(self, memory, history: History, interval_s: float):
        self.memory, self.history, self.interval_s = memory, history, interval_s
        self.last_ts: float | None = None

    def step(self) -> int:
        state = self.memory.state
        if state is None or state.ts == self.last_ts:
            return 0
        self.last_ts = state.ts
        return self.history.record(state)

    def run(self, stop: threading.Event) -> None:
        log.info("history recorder started (%s, every %.0fs)", self.history.path, self.interval_s)
        while not stop.wait(self.interval_s):
            try:
                self.step()
            except sqlite3.Error as e:
                log.warning("history write failed: %s", e)

    def start_thread(self) -> tuple[threading.Thread, threading.Event]:
        stop = threading.Event()
        t = threading.Thread(target=self.run, args=(stop,), name="history", daemon=True)
        t.start()
        return t, stop
