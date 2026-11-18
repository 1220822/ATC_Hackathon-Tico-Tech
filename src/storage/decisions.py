"""Decisions of the Coordinator and how the user answered them, in var/home.db.

    decisions(id, ts, trigger JSON, proposals JSON, decision JSON, explanation, autonomy, status,
              user_response, user_comment, responded_at,
              kind, category, room, room_name, device_id, agent)

status:         executed | pending | rejected | dry_run | vetoed
                (vetoed = stopped by a guardrail: unknown/offline device, smoke emergency, ...)
user_response:  approved | rejected | modified | NULL
kind:           "same type of decision in the same context" for the learning rule of step 5:
                "<trigger type>|<device id>|<capability>=<value>", e.g.
                "contact_opened|a82edb80-...|power=off"
category:       energia | conforto | seguranca | luz | emergencia | utilizador
"""

import time
import uuid
from typing import Any, Literal

from .db import SqliteStore, dumps, loads

Status = Literal["executed", "pending", "rejected", "dry_run", "vetoed"]
UserResponse = Literal["approved", "rejected", "modified"]
STATUSES = ("executed", "pending", "rejected", "dry_run", "vetoed")
RESPONSES = ("approved", "rejected", "modified")
_JSON = ("trigger", "proposals", "decision")


def decision_kind(trigger_type: str | None, device_id: str | None, body: dict | None) -> str:
    action = ""
    if body:
        action = f"{body.get('type')}={body.get('value')}"
    return f"{trigger_type or 'manual'}|{device_id or '-'}|{action}"


class DecisionStore(SqliteStore):
    SCHEMA = """
    CREATE TABLE IF NOT EXISTS decisions (
        id TEXT PRIMARY KEY, ts REAL NOT NULL, trigger TEXT, proposals TEXT, decision TEXT,
        explanation TEXT, autonomy TEXT, status TEXT NOT NULL,
        user_response TEXT, user_comment TEXT, responded_at REAL,
        kind TEXT, category TEXT, room TEXT, room_name TEXT, device_id TEXT, agent TEXT
    );
    CREATE INDEX IF NOT EXISTS decisions_ts ON decisions (ts);
    CREATE INDEX IF NOT EXISTS decisions_kind_ts ON decisions (kind, ts);
    CREATE INDEX IF NOT EXISTS decisions_status ON decisions (status);
    """

    def add(self, *, trigger: dict | None, proposals: list | None, decision: dict | None,
            explanation: str, status: Status, autonomy: str | None = None, kind: str | None = None,
            category: str | None = None, room: str | None = None, room_name: str | None = None,
            device_id: str | None = None, agent: str | None = None, ts: float | None = None,
            id: str | None = None) -> dict:
        if status not in STATUSES:
            raise ValueError(f"status inválido: {status}")
        row = {"id": id or uuid.uuid4().hex[:12], "ts": time.time() if ts is None else ts,
               "trigger": trigger, "proposals": proposals or [], "decision": decision or {},
               "explanation": explanation, "autonomy": autonomy, "status": status,
               "user_response": None, "user_comment": None, "responded_at": None,
               "kind": kind, "category": category, "room": room, "room_name": room_name,
               "device_id": device_id, "agent": agent}
        cols = list(row)
        self.execute(f"INSERT INTO decisions ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [dumps(row[c]) if c in _JSON else row[c] for c in cols])
        return row

    @staticmethod
    def _decode(r: dict | None) -> dict | None:
        if r is None:
            return None
        for c in _JSON:
            r[c] = loads(r[c], [] if c == "proposals" else {})
        return r

    def get(self, id: str) -> dict | None:
        return self._decode(self.row("SELECT * FROM decisions WHERE id = ?", (id,)))

    def set_status(self, id: str, status: Status, explanation: str | None = None,
                   decision: dict | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"status inválido: {status}")
        sets, args = ["status = ?"], [status]
        if explanation is not None:
            sets.append("explanation = ?")
            args.append(explanation)
        if decision is not None:
            sets.append("decision = ?")
            args.append(dumps(decision))
        self.execute(f"UPDATE decisions SET {', '.join(sets)} WHERE id = ?", args + [id])

    def respond(self, id: str, response: UserResponse, comment: str | None = None,
                now: float | None = None) -> dict | None:
        if response not in RESPONSES:
            raise ValueError(f"resposta inválida: {response}")
        self.execute("UPDATE decisions SET user_response = ?, user_comment = ?, responded_at = ? WHERE id = ?",
                     (response, comment, time.time() if now is None else now, id))
        return self.get(id)

    def recent(self, n: int = 20, status: str | None = None, since: float | None = None) -> list[dict]:
        sql, args = "SELECT * FROM decisions WHERE 1=1", []
        if status is not None:
            sql += " AND status = ?"
            args.append(status)
        if since is not None:
            sql += " AND ts >= ?"
            args.append(since)
        return [self._decode(r) for r in self.rows(sql + " ORDER BY ts DESC LIMIT ?", args + [int(n)])]

    def pending(self) -> list[dict]:
        return [self._decode(r) for r in
                self.rows("SELECT * FROM decisions WHERE status = 'pending' AND user_response IS NULL ORDER BY ts")]

    def similar(self, kind: str | None = None, room: str | None = None, device_id: str | None = None,
                n: int = 3, trigger_type: str | None = None) -> list[dict]:
        """Latest decisions of the same kind (else same trigger type in the room, same device, same
        room), newest first."""
        if kind:
            found = self.rows("SELECT * FROM decisions WHERE kind = ? ORDER BY ts DESC LIMIT ?", (kind, n))
            if found:
                return [self._decode(r) for r in found]
        if trigger_type:
            sql, args = "SELECT * FROM decisions WHERE json_extract(trigger, '$.type') = ?", [trigger_type]
            if room:
                sql += " AND room = ?"
                args.append(room)
            found = self.rows(sql + " ORDER BY ts DESC LIMIT ?", args + [n])
            if found:
                return [self._decode(r) for r in found]
        if device_id:
            found = self.rows("SELECT * FROM decisions WHERE device_id = ? ORDER BY ts DESC LIMIT ?", (device_id, n))
            if found:
                return [self._decode(r) for r in found]
        if room:
            return [self._decode(r) for r in
                    self.rows("SELECT * FROM decisions WHERE room = ? ORDER BY ts DESC LIMIT ?", (room, n))]
        return []

    def approval_streak(self, kind: str) -> int:
        """Consecutive 'approved' answers to decisions of this kind, newest first (decisions the user
        did not answer are skipped; any other answer ends the streak)."""
        streak = 0
        for r in self.rows("SELECT user_response FROM decisions WHERE kind = ? AND user_response IS NOT NULL "
                           "ORDER BY responded_at DESC, ts DESC LIMIT 20", (kind,)):
            if r["user_response"] != "approved":
                break
            streak += 1
        return streak

    def clear(self) -> int:
        return self.execute("DELETE FROM decisions")


def map_status(pt_status: str) -> Status:
    """Status of the timeline Decision (PT) -> decisions.status."""
    return {"executada": "executed", "dry-run": "dry_run", "rejeitada": "rejected", "erro": "rejected",
            "aprovada": "executed"}.get(pt_status, "rejected")


def describe(d: dict[str, Any]) -> str:
    return f"{time.strftime('%d/%m %H:%M', time.localtime(d['ts']))} {d.get('explanation') or ''}".strip()
