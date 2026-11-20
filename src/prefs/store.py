"""Versioned preferences of the user, in var/home.db (local only).

    preferences(id, ts, scope, key, value JSON, source, original_text, active, superseded_by,
                mcp_scenario_id)

A preference is never deleted by set()/revoke(): changing one inserts a new row and deactivates
the previous one (superseded_by = new id); revoking only sets active = 0. The only way to delete
rows is erase_all(), the explicit "apagar tudo" of "O que a casa sabe de ti" (RGPD erasure).

scope: global | room:<id> | device:<id>; source: explicit | learned_confirmed.
"""

import time
import uuid
from datetime import datetime
from typing import Iterable

from ..storage.db import SqliteStore, dumps, loads
from .models import (CATEGORIES, DEFAULT_LEVEL, SetpointValue, QuietHoursValue, identity,
                     validate_scope, validate_value)

SOURCES = ("explicit", "learned_confirmed")


class PrefStore(SqliteStore):
    # optional (scope, key, value) -> id: the scenario mode reproduces the ids seen in the recordings
    id_factory = None

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS preferences (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, scope TEXT NOT NULL, key TEXT NOT NULL,
        value TEXT NOT NULL, source TEXT NOT NULL, original_text TEXT,
        active INTEGER NOT NULL DEFAULT 1, superseded_by TEXT, mcp_scenario_id TEXT
    );
    CREATE INDEX IF NOT EXISTS preferences_active ON preferences (active, key, scope);
    """

    @staticmethod
    def _decode(r: dict | None) -> dict | None:
        if r is not None:
            r["value"] = loads(r["value"], {})
            r["active"] = bool(r["active"])
        return r

    # -- write ---------------------------------------------------------------------
    def set(self, scope: str, key: str, value: dict, source: str = "explicit", original_text: str | None = None,
            mcp_scenario_id: str | None = None, replaces: str | None = None, now: float | None = None) -> dict:
        """Validate and store a preference. The active row with the same identity (or `replaces`)
        is deactivated and points to the new one."""
        scope = validate_scope(scope)
        value = validate_value(key, value)
        if source not in SOURCES:
            raise ValueError(f"source inválido: {source}")
        new_id = (self.id_factory(scope, key, value) if self.id_factory else None) or uuid.uuid4().hex[:12]
        ts = time.time() if now is None else now
        ident = identity(scope, key, value)
        with self.connect() as db:
            old_ids = []
            if replaces:
                old_ids.append(replaces)
            if ident is not None:
                for r in db.execute("SELECT id, scope, key, value FROM preferences WHERE active = 1 AND key = ? "
                                    "AND scope = ?", (key, scope)):
                    if identity(r["scope"], r["key"], loads(r["value"], {})) == ident:
                        old_ids.append(r["id"])
            db.execute("INSERT INTO preferences (id, ts, scope, key, value, source, original_text, active, "
                       "superseded_by, mcp_scenario_id) VALUES (?, ?, ?, ?, ?, ?, ?, 1, NULL, ?)",
                       (new_id, ts, scope, key, dumps(value), source, original_text, mcp_scenario_id))
            for old in dict.fromkeys(old_ids):
                db.execute("UPDATE preferences SET active = 0, superseded_by = ? WHERE id = ? AND active = 1",
                           (new_id, old))
        db.close()
        return self.get(new_id)

    def revoke(self, pref_id: str) -> dict | None:
        self.execute("UPDATE preferences SET active = 0 WHERE id = ?", (pref_id,))
        return self.get(pref_id)

    def erase_all(self) -> int:
        """RGPD: the user asked to delete everything the house knows about them."""
        return self.execute("DELETE FROM preferences")

    # -- read ----------------------------------------------------------------------
    def get(self, pref_id: str) -> dict | None:
        return self._decode(self.row("SELECT * FROM preferences WHERE id = ?", (pref_id,)))

    def get_active(self, scope: str | Iterable[str] | None = None, key: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM preferences WHERE active = 1", []
        if isinstance(scope, str):
            scope = [scope]
        if scope is not None:
            scope = list(scope)
            sql += f" AND scope IN ({','.join('?' * len(scope))})"
            args += scope
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        return [self._decode(r) for r in self.rows(sql + " ORDER BY ts", args)]

    def history(self, key: str | None = None, scope: str | None = None, n: int = 200) -> list[dict]:
        sql, args = "SELECT * FROM preferences WHERE 1=1", []
        if key is not None:
            sql += " AND key = ?"
            args.append(key)
        if scope is not None:
            sql += " AND scope = ?"
            args.append(scope)
        return [self._decode(r) for r in self.rows(sql + " ORDER BY ts DESC LIMIT ?", args + [n])]

    def applicable(self, room_id: str | None = None, device_ids: Iterable[str] = ()) -> list[dict]:
        """Active preferences that apply to a room and its devices, plus the global ones."""
        scopes = ["global"] + ([f"room:{room_id}"] if room_id else []) + [f"device:{d}" for d in device_ids]
        return self.get_active(scopes)

    # -- typed helpers ---------------------------------------------------------------
    def autonomy(self, category: str, kind: str | None = None) -> str:
        """auto | perguntar | nunca for a category (default: perguntar). A level learned for this exact
        kind of decision wins over the category level, except that "nunca" for the category always wins."""
        p = self.autonomy_pref(category, kind)
        return p["value"]["level"] if p else DEFAULT_LEVEL

    def autonomy_pref(self, category: str, kind: str | None = None) -> dict | None:
        if category not in CATEGORIES:
            return None
        rows = list(reversed(self.get_active("global", "autonomy")))
        general = next((p for p in rows if p["value"].get("category") == category and not p["value"].get("kind")), None)
        if general and general["value"]["level"] == "nunca":
            return general
        if kind:
            specific = next((p for p in rows if p["value"].get("kind") == kind), None)
            if specific:
                return specific
        return general

    def setpoint(self, room_id: str, when: datetime | None = None) -> tuple[float | None, dict | None]:
        """(target °C now, preference row) for a room; room scope wins over global."""
        when = when or datetime.now()
        for scope in (f"room:{room_id}", "global"):
            for p in reversed(self.get_active(scope, "setpoint_c")):
                c = SetpointValue.model_validate(p["value"]).target(when)
                if c is not None:
                    return c, p
        return None, None

    def humidity_max(self, room_id: str) -> tuple[float | None, dict | None]:
        for scope in (f"room:{room_id}", "global"):
            found = self.get_active(scope, "humidity_max_pct")
            if found:
                return float(found[-1]["value"]["pct"]), found[-1]
        return None, None

    def comfort_range(self, room_id: str) -> tuple[tuple[float, float] | None, dict | None]:
        for scope in (f"room:{room_id}", "global"):
            found = self.get_active(scope, "comfort_range_c")
            if found:
                v = found[-1]["value"]
                return (float(v["min_c"]), float(v["max_c"])), found[-1]
        return None, None

    def quiet_now(self, when: datetime | None = None) -> bool:
        when = when or datetime.now()
        return any(QuietHoursValue.model_validate(p["value"]).active(when)
                   for p in self.get_active("global", "quiet_hours"))

    def muted(self, insight_key: str) -> bool:
        return any(p["value"].get("insight_key") == insight_key
                   for p in self.get_active(None, "suggestion_muted"))
