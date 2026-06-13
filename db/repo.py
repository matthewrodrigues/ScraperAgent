"""Thin SQLite wrapper.

Deliberately thin: no ORM, no migrations framework. The schema is small and
the access patterns are simple. When that stops being true, swap to SQLAlchemy.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import config


SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every boot."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Connection context with row factory + foreign keys on."""
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None)  # autocommit; we manage txns explicitly when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ---- Search CRUD ---------------------------------------------------------

def create_search(criteria_nl: str, criteria_structured: dict[str, Any], max_price: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO searches (criteria_nl, criteria_structured_json, max_price) VALUES (?, ?, ?)",
            (criteria_nl, json.dumps(criteria_structured), max_price),
        )
        return cur.lastrowid


def get_search(search_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["criteria_structured"] = json.loads(d.pop("criteria_structured_json"))
        return d


def update_search_status(search_id: int, status: str, thread_id: str | None = None) -> None:
    with get_conn() as conn:
        if thread_id is not None:
            conn.execute(
                "UPDATE searches SET status = ?, thread_id = ? WHERE id = ?",
                (status, thread_id, search_id),
            )
        else:
            conn.execute("UPDATE searches SET status = ? WHERE id = ?", (status, search_id))


def list_recent_searches(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, criteria_nl, max_price, status, created_at FROM searches "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
