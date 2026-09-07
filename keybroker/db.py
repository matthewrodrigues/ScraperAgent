"""Broker persistence: friends and spend.

Deliberately tiny. The broker stores no domain data — no searches, listings,
negotiations, or eBay credentials. Two tables is the whole model.

Conventions mirror db/schema.sql: integer PKs, ISO-8601 UTC text timestamps
written by SQLite's datetime('now'), cost_usd REAL, cascading FKs.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import config


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS friends (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    token_sha256       TEXT NOT NULL UNIQUE,
    monthly_budget_usd REAL NOT NULL DEFAULT 5.0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at         TEXT
);

CREATE TABLE IF NOT EXISTS spend (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id             INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    vendor                TEXT NOT NULL,
    model_or_actor        TEXT,
    cost_usd              REAL NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    upstream_ref          TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spend_friend_time ON spend(friend_id, created_at);

-- Apify's .call() polls, so the same terminal run object arrives repeatedly.
-- A partial unique index makes INSERT OR IGNORE the dedupe mechanism, mirroring
-- the idx_messages_pending idiom in db/schema.sql.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_upstream
    ON spend(vendor, upstream_ref) WHERE upstream_ref IS NOT NULL;
"""


def init_db() -> None:
    with sqlite3.connect(config.BROKER_DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.BROKER_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def month_bounds(month: str | None = None) -> tuple[str, str]:
    """Half-open [start, end) for a YYYY-MM month, in SQLite's datetime('now')
    format. Defaults to the current UTC month."""
    if month is None:
        now = datetime.now(timezone.utc)
        year, mon = now.year, now.month
    else:
        year, mon = (int(part) for part in month.split("-"))
    next_year, next_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return (
        f"{year:04d}-{mon:02d}-01 00:00:00",
        f"{next_year:04d}-{next_mon:02d}-01 00:00:00",
    )


def create_friend(name: str, token_sha256: str, monthly_budget_usd: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO friends (name, token_sha256, monthly_budget_usd) VALUES (?, ?, ?)",
            (name, token_sha256, monthly_budget_usd),
        )
        return int(cur.lastrowid)


def get_friend_by_token_hash(token_sha256: str) -> dict[str, Any] | None:
    """Active friends only — a revoked token is indistinguishable from unknown."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM friends WHERE token_sha256 = ? AND revoked_at IS NULL",
            (token_sha256,),
        ).fetchone()
        return dict(row) if row else None


def get_friend_by_name(name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM friends WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def revoke_friend(name: str) -> bool:
    """Set revoked_at. Never deletes — spend history outlives the friendship."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE friends SET revoked_at = datetime('now') "
            "WHERE name = ? AND revoked_at IS NULL",
            (name,),
        )
        return cur.rowcount > 0


def list_friends() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM friends ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def record_spend(
    friend_id: int,
    vendor: str,
    cost_usd: float,
    model_or_actor: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    upstream_ref: str | None = None,
) -> bool:
    """Record one charge. Returns False when a row for this (vendor,
    upstream_ref) already existed, which is the normal case for Apify polls."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO spend (
                friend_id, vendor, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, upstream_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                friend_id, vendor, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, upstream_ref,
            ),
        )
        return cur.rowcount > 0


def friend_month_spend(friend_id: int, month: str | None = None) -> float:
    start, end = month_bounds(month)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
            "WHERE friend_id = ? AND created_at >= ? AND created_at < ?",
            (friend_id, start, end),
        ).fetchone()
        return float(row["total"])


def global_month_spend(month: str | None = None) -> float:
    start, end = month_bounds(month)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
            "WHERE created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchone()
        return float(row["total"])
