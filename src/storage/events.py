"""Every event of the bus, in var/home.db (data memory of what happened in the house).

    events(id, ts, type, severity, room, room_name, device_id, data JSON, message)

"tick" events (periodic snapshot, every 90 s) are not stored: the readings table has that.
When the API and `python -m src.storage.recorder` run together, only the process that holds the
history writer lease stores events (pass `is_writer`), so nothing is stored twice.
"""

import time
from typing import Callable, Iterable

from ..perception.state import Event
from .db import SqliteStore, dumps, loads

SKIP_TYPES = frozenset({"tick"})


class EventStore(SqliteStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS events (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, type TEXT NOT NULL, severity TEXT NOT NULL,
        room TEXT, room_name TEXT, device_id TEXT, data TEXT, message TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
    CREATE INDEX IF NOT EXISTS events_room_ts ON events (room, ts);
    CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts);
    """

    def __init__(self, path, readonly: bool = False, is_writer: Callable[[], bool] | None = None):
        super().__init__(path, readonly)
        self.is_writer = is_writer

    def add(self, e: Event) -> bool:
        if e.type in SKIP_TYPES:
            return False
        return self.execute(
            "INSERT OR IGNORE INTO events (id, ts, type, severity, room, room_name, device_id, data, message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (e.id, e.ts, e.type, e.severity, e.room, e.room_name, e.device_id, dumps(e.data), e.message)) > 0

    def on_event(self, e: Event) -> None:
        """Bus subscriber."""
        if self.is_writer is None or self.is_writer():
            self.add(e)

    def recent(self, n: int = 20, room: str | None = None, types: Iterable[str] | None = None,
               since: float | None = None, device_id: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if room is not None:
            sql += " AND room = ?"
            args.append(room)
        if device_id is not None:
            sql += " AND device_id = ?"
            args.append(device_id)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        types = list(types or [])
        if types:
            sql += f" AND type IN ({','.join('?' * len(types))})"
            args += types
        rows = self.rows(sql + " ORDER BY ts DESC LIMIT ?", args + [int(n)])
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows

    def count(self, since: float | None = None) -> int:
        r = self.row("SELECT COUNT(*) AS n FROM events WHERE ts >= ?", (since or 0,))
        return r["n"] if r else 0

    def by_day(self, type_: str, days: int = 14, device_id: str | None = None) -> list[dict]:
        """Events of one type in the last `days` days, oldest first (for the insight detectors)."""
        sql, args = "SELECT * FROM events WHERE type = ? AND ts >= ?", [type_, time.time() - days * 86400]
        if device_id is not None:
            sql += " AND device_id = ?"
            args.append(device_id)
        rows = self.rows(sql + " ORDER BY ts", args)
        for r in rows:
            r["data"] = loads(r["data"], {})
        return rows
