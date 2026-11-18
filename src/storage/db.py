"""Small base for the SQLite stores that share var/home.db (stdlib sqlite3, WAL mode).
Every call opens its own short connection, so threads and processes can share the file."""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any


class SqliteStore:
    SCHEMA = ""

    def __init__(self, path: Path | str, readonly: bool = False):
        self.path = Path(path)
        self.readonly = readonly
        if not readonly:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with closing(self.connect()) as db, db:
                db.execute("PRAGMA journal_mode=WAL")
                db.executescript(self.SCHEMA)

    def connect(self) -> sqlite3.Connection:
        if self.readonly:
            db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
        else:
            db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def execute(self, sql: str, args: tuple | list = ()) -> int:
        with closing(self.connect()) as db, db:
            return db.execute(sql, args).rowcount

    def rows(self, sql: str, args: tuple | list = ()) -> list[dict]:
        with closing(self.connect()) as db:
            return [dict(r) for r in db.execute(sql, args)]

    def row(self, sql: str, args: tuple | list = ()) -> dict | None:
        found = self.rows(sql, args)
        return found[0] if found else None


def dumps(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, default=str)


def loads(text: str | None, default: Any = None) -> Any:
    if text is None or text == "":
        return default
    try:
        return json.loads(text)
    except ValueError:
        return default
