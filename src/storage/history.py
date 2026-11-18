"""History of the home in SQLite (stdlib sqlite3). File var/home.db (var/home_sim.db when
SIMULATE=true, because simulated Sala/Quarto reuse the real room ids).

    readings(ts REAL, room TEXT, resource_id TEXT, metric TEXT, value REAL)
    index (room, metric, ts) and (resource_id, metric, ts)
    meta(key TEXT PRIMARY KEY, value TEXT)        writer lease, one-off migrations

`room` is the room id ("home" for house-level values). Metrics (online devices only):
    temperature           °C     temperatureSensor.temperatureValue
    humidity              %      temperatureSensor.humidityValue
    power_w               W      plug.instantPower
    energy_today_kwh      kWh    plug.todayConsumption
    energy_yesterday_kwh  kWh    plug.yesterdayConsumption; room "home": get_home.energy.yesterdayConsumption
    open                  1 = open   contactSensor.sensorState == "alarm"
    on                    1 = on     plug.power / bulb.power == "on"
    battery               %      batteryLevel; batteryStatus high/medium/low -> 100/50/10 (approximation)

Old names are still accepted when reading: power -> power_w, contact -> open, light -> on.

A HistoryRecorder thread stores the latest HomeState every STORE_INTERVAL_S (30 s). Only one
process stores readings at a time (lease in `meta`): if `python -m src.storage.recorder` is running,
the API's recorder stands by, and takes over ~90 s after the recorder stops. Queries open their own
connection (WAL mode), so readers never block the writer. Agent tools open the file read-only.
"""

import json
import logging
import os
import socket
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Iterable

from ..perception.state import HomeState, _num

log = logging.getLogger(__name__)

METRICS = ("temperature", "humidity", "power_w", "energy_today_kwh", "energy_yesterday_kwh",
           "open", "on", "battery")
ALIASES = {"power": "power_w", "contact": "open", "light": "on"}
AGGS = {"avg": "AVG", "sum": "SUM", "min": "MIN", "max": "MAX"}
HOME_ROOM = "home"
BATTERY_STATUS = {"high": 100.0, "medium": 50.0, "middle": 50.0, "low": 10.0}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    ts REAL NOT NULL, room TEXT NOT NULL, resource_id TEXT NOT NULL, metric TEXT NOT NULL, value REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS readings_room_metric_ts ON readings (room, metric, ts);
CREATE INDEX IF NOT EXISTS readings_resource_metric_ts ON readings (resource_id, metric, ts);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

Row = tuple[float, str, str, str, float]


def metric_name(metric: str) -> str:
    m = ALIASES.get(metric, metric)
    if m not in METRICS:
        raise ValueError(f"métrica desconhecida {metric!r}; usa uma de {list(METRICS)}")
    return m


def _battery(caps: dict) -> float | None:
    level = _num(caps.get("batteryLevel"))
    if level is not None:
        return level
    status = caps.get("batteryStatus")
    return BATTERY_STATUS.get(str(status).casefold()) if status is not None else None


def _on(value) -> float | None:
    return None if value is None else (1.0 if value == "on" else 0.0)


def readings_from_state(state: HomeState) -> list[Row]:
    """(ts, room, resource_id, metric, value) rows for the numeric values of a state."""
    rows: list[Row] = []
    for d in state.devices.values():
        if not d.online:
            continue
        c = d.caps
        values: list[tuple[str, float | None]] = []
        if d.role == "temperature":
            values += [("temperature", _num(c.get("temperatureValue"))),
                       ("humidity", _num(c.get("humidityValue")))]
        elif d.role == "plug":
            values += [("power_w", _num(c.get("instantPower"))), ("on", _on(c.get("power"))),
                       ("energy_today_kwh", _num(c.get("todayConsumption"))),
                       ("energy_yesterday_kwh", _num(c.get("yesterdayConsumption")))]
        elif d.role == "contact" and c.get("sensorState") is not None:
            values.append(("open", 1.0 if c.get("sensorState") == "alarm" else 0.0))
        elif d.role == "light":
            values.append(("on", _on(c.get("power"))))
        values.append(("battery", _battery(c)))
        rows += [(state.ts, d.room_id, d.id, m, v) for m, v in values if v is not None]
    if state.energy.yesterday_consumption_kwh is not None:
        rows.append((state.ts, HOME_ROOM, HOME_ROOM, "energy_yesterday_kwh", state.energy.yesterday_consumption_kwh))
    return rows


class History:
    def __init__(self, path: Path | str, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self._connect()) as db, db:
                db.execute("PRAGMA journal_mode=WAL")
                _migrate_columns(db)
                db.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        if self.readonly:
            return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        return sqlite3.connect(self.path, timeout=10)

    # -- write ---------------------------------------------------------------
    def insert(self, rows: Iterable[Row]) -> int:
        rows = [(ts, room, rid, metric_name(m), v) for ts, room, rid, m, v in rows]
        if rows:
            with closing(self._connect()) as db, db:
                db.executemany("INSERT INTO readings (ts, room, resource_id, metric, value) "
                               "VALUES (?, ?, ?, ?, ?)", rows)
        return len(rows)

    def record(self, state: HomeState) -> int:
        return self.insert(readings_from_state(state))

    # -- meta / writer lease ------------------------------------------------------
    def get_meta(self, key: str) -> str | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO meta (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def claim_writer(self, owner: str, ttl_s: float, now: float | None = None) -> bool:
        """Take or renew the readings-writer lease. False if another owner renewed it within ttl_s."""
        now = time.time() if now is None else now
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM meta WHERE key = 'writer'").fetchone()
            cur = json.loads(row[0]) if row else None
            if cur and cur.get("owner") != owner and now - float(cur.get("ts", 0)) < ttl_s:
                db.rollback()
                return False
            db.execute("INSERT INTO meta (key, value) VALUES ('writer', ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                       (json.dumps({"owner": owner, "ts": now}),))
            db.commit()
            return True
        finally:
            db.close()

    def writer(self) -> dict | None:
        raw = self.get_meta("writer")
        return json.loads(raw) if raw else None

    # -- read ----------------------------------------------------------------
    def query(self, room: str | None, metric: str, start: float | None = None,
              end: float | None = None, resource_id: str | None = None) -> list[dict]:
        """Raw readings, oldest first: [{ts, room, resource_id, device_id, value}].
        room None = every room; resource_id narrows to one device."""
        metric = metric_name(metric)
        start, end = _range(start, end)
        sql = "SELECT ts, room, resource_id, value FROM readings WHERE metric = ? AND ts >= ? AND ts < ?"
        args: list = [metric, start, end]
        if room is not None:
            sql += " AND room = ?"
            args.append(room)
        if resource_id is not None:
            sql += " AND resource_id = ?"
            args.append(resource_id)
        with closing(self._connect()) as db:
            cur = db.execute(sql + " ORDER BY ts", args)
            return [{"ts": ts, "room": r, "resource_id": rid, "device_id": rid, "value": v}
                    for ts, r, rid, v in cur]

    def aggregate(self, room: str | None, metric: str, period_s: float, agg: str = "avg",
                  start: float | None = None, end: float | None = None) -> list[dict]:
        """Buckets of floor(ts / period_s) * period_s: [{ts (bucket start), value, n}]."""
        metric = metric_name(metric)
        if agg not in AGGS:
            raise ValueError(f"agg tem de ser um de {sorted(AGGS)}")
        if not period_s or period_s <= 0:
            raise ValueError("period_s tem de ser > 0")
        start, end = _range(start, end)
        sql = (f"SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, {AGGS[agg]}(value), COUNT(*) "
               "FROM readings WHERE metric = ? AND ts >= ? AND ts < ?")
        args: list = [period_s, period_s, metric, start, end]
        if room is not None:
            sql += " AND room = ?"
            args.append(room)
        with closing(self._connect()) as db:
            cur = db.execute(sql + " GROUP BY bucket ORDER BY bucket", args)
            return [{"ts": float(b), "value": v, "n": n} for b, v, n in cur]

    def energy_wh(self, room: str | None = None, start: float | None = None, end: float | None = None,
                  resource_id: str | None = None, max_gap_s: float = 120.0) -> float:
        """Energy (Wh) from power_w samples, trapezoidal rule per device. Intervals longer than
        max_gap_s (recorder stopped, MCP down) are not integrated: no data is not consumption."""
        rows = self.query(room, "power_w", start, end, resource_id)
        return round(sum(trapezoid_wh(s, max_gap_s) for s in _by_resource(rows).values()), 4)

    def energy_wh_by_period(self, room: str | None, period_s: float, start: float | None = None,
                            end: float | None = None, max_gap_s: float = 120.0) -> dict[float, float]:
        """Wh per bucket (each trapezoid is split at bucket boundaries)."""
        out: dict[float, float] = {}
        for samples in _by_resource(self.query(room, "power_w", start, end)).values():
            for a, b in zip(samples, samples[1:]):
                dt = b["ts"] - a["ts"]
                if dt <= 0 or dt > max_gap_s:
                    continue
                t = a["ts"]
                while t < b["ts"]:
                    bucket = t // period_s * period_s
                    t2 = min(b["ts"], bucket + period_s)
                    pa = _lerp(a, b, t)
                    pb = _lerp(a, b, t2)
                    out[bucket] = out.get(bucket, 0.0) + (pa + pb) / 2 * (t2 - t) / 3600
                    t = t2
        return dict(sorted(out.items()))

    def latest(self, room: str, metric: str, resource_id: str | None = None) -> dict | None:
        metric = metric_name(metric)
        sql = "SELECT ts, resource_id, value FROM readings WHERE room = ? AND metric = ?"
        args: list = [room, metric]
        if resource_id is not None:
            sql += " AND resource_id = ?"
            args.append(resource_id)
        with closing(self._connect()) as db:
            row = db.execute(sql + " ORDER BY ts DESC LIMIT 1", args).fetchone()
        return {"ts": row[0], "resource_id": row[1], "value": row[2]} if row else None

    def rooms(self) -> list[str]:
        with closing(self._connect()) as db:
            return [r for (r,) in db.execute("SELECT DISTINCT room FROM readings ORDER BY room")]

    def summary(self) -> list[dict]:
        """Rows, first and last timestamp per room and metric (for --status)."""
        with closing(self._connect()) as db:
            cur = db.execute("SELECT room, metric, COUNT(*), MIN(ts), MAX(ts) FROM readings "
                             "GROUP BY room, metric ORDER BY room, metric")
            return [{"room": r, "metric": m, "n": n, "first": a, "last": b} for r, m, n, a, b in cur]


def _lerp(a: dict, b: dict, t: float) -> float:
    span = b["ts"] - a["ts"]
    return a["value"] if span <= 0 else a["value"] + (b["value"] - a["value"]) * (t - a["ts"]) / span


def _by_resource(rows: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in rows:
        out.setdefault(r["resource_id"], []).append(r)
    return out


def trapezoid_wh(samples: list[dict], max_gap_s: float) -> float:
    """∫ power dt in Wh over consecutive samples [{ts, value}] closer than max_gap_s."""
    wh = 0.0
    for a, b in zip(samples, samples[1:]):
        dt = b["ts"] - a["ts"]
        if 0 < dt <= max_gap_s:
            wh += (a["value"] + b["value"]) / 2 * dt / 3600
    return wh


def _migrate_columns(db: sqlite3.Connection) -> None:
    """Old schema (device_id column, metrics power/contact/light) -> current one, in place."""
    cols = [r[1] for r in db.execute("PRAGMA table_info(readings)")]
    if "device_id" in cols and "resource_id" not in cols:
        db.execute("DROP INDEX IF EXISTS readings_room_metric_ts")
        db.execute("ALTER TABLE readings RENAME COLUMN device_id TO resource_id")
        for old, new in ALIASES.items():
            db.execute("UPDATE readings SET metric = ? WHERE metric = ?", (new, old))


def import_legacy(history: History, legacy: Path | str) -> int:
    """Copy the readings of the old var/history.db once (real rooms only: ids starting with "sim-"
    were the simulated rooms of SIM_ROOMS=true). Returns how many rows were copied."""
    legacy = Path(legacy)
    if history.get_meta("legacy_imported") or not legacy.exists() or legacy.resolve() == history.path.resolve():
        return 0
    with closing(sqlite3.connect(f"{legacy.resolve().as_uri()}?mode=ro", uri=True)) as src:
        cols = [r[1] for r in src.execute("PRAGMA table_info(readings)")]
        rid = "resource_id" if "resource_id" in cols else "device_id"
        rows = src.execute(f"SELECT ts, room, {rid}, metric, value FROM readings "
                           "WHERE room NOT LIKE 'sim-%' ORDER BY ts").fetchall()
    n = history.insert(rows)
    history.set_meta("legacy_imported", json.dumps({"from": str(legacy), "rows": n, "ts": time.time()}))
    log.info("imported %d readings from %s", n, legacy)
    return n


def _range(start: float | None, end: float | None) -> tuple[float, float]:
    end = time.time() + 1 if end is None else float(end)
    start = end - 24 * 3600 if start is None else float(start)
    return start, end


class HistoryRecorder:
    """Every interval_s, stores the latest state from SharedMemory (skips if it has not changed or
    another process holds the writer lease)."""

    def __init__(self, memory, history: History, interval_s: float, owner: str | None = None):
        self.memory, self.history, self.interval_s = memory, history, interval_s
        self.owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        self.last_ts: float | None = None
        self.rows_written = 0
        self.standby = False

    def step(self) -> int:
        state = self.memory.state
        if state is None or state.ts == self.last_ts:
            return 0
        if not self.history.claim_writer(self.owner, ttl_s=max(3 * self.interval_s, 60)):
            if not self.standby:
                log.info("outro processo está a gravar o histórico (%s); fico em espera",
                         (self.history.writer() or {}).get("owner"))
            self.standby = True
            return 0
        self.standby = False
        self.last_ts = state.ts
        n = self.history.record(state)
        self.rows_written += n
        return n

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
