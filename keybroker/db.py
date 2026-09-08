"""Broker persistence: friends and spend.

Deliberately tiny. The broker stores no domain data — no searches, listings,
negotiations, or eBay credentials. Two tables is the whole model.

Conventions mirror db/schema.sql: integer PKs, ISO-8601 UTC text timestamps
written by SQLite's datetime('now'), cost_usd REAL, cascading FKs.
"""

import sqlite3
from contextlib import closing, contextmanager
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
    provisional           INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spend_friend_time ON spend(friend_id, created_at);

-- Apify's .call() polls, so the same terminal run object arrives repeatedly.
-- A partial unique index keys the upsert in record_spend, mirroring the
-- idx_messages_pending idiom in db/schema.sql. It is a conflict target, not
-- just a dedupe: a provisional row must be correctable in place by
-- scripts.reconcile_spend once Apify's usage figure settles.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_upstream
    ON spend(vendor, upstream_ref) WHERE upstream_ref IS NOT NULL;
"""


# Lightweight "add a column if it doesn't already exist" pattern, mirroring
# db/repo.py's _COLUMN_MIGRATIONS. CREATE TABLE IF NOT EXISTS does nothing to a
# table that already exists, so a schema addition needs this to reach an
# existing broker.db. Keep entries ordered oldest-first.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    # Apify's usageTotalUsd settles only some time *after* the run first reports
    # a terminal status, so the broker debits the clamped ceiling up front and
    # flags the row provisional. scripts.reconcile_spend settles it later.
    ("spend", "provisional", "INTEGER NOT NULL DEFAULT 0"),
]


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, col, coltype in _COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def init_db() -> None:
    # sqlite3's own context manager commits, it does not close — hence closing().
    with closing(sqlite3.connect(config.BROKER_DB_PATH)) as conn:
        with conn:
            conn.executescript(SCHEMA)
            _apply_column_migrations(conn)


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
    provisional: bool = False,
) -> bool:
    """Record one charge, upserting on (vendor, upstream_ref).

    Returns False when a row for this (vendor, upstream_ref) already existed,
    which is the normal case for Apify polls. It is an upsert rather than an
    INSERT OR IGNORE because the first observation of an Apify run is not
    necessarily the right one: usageTotalUsd settles after the run first reports
    a terminal status, so the ledger has to stay correctable. Ignoring the
    second write made the first (low) reading permanent, which under-metered
    real runs by ~15x.

    The caller decides whether a correction is trustworthy — meter.record_apify
    deliberately refuses to touch a provisional row, leaving that to
    scripts.reconcile_spend.
    """
    row = (
        friend_id, vendor, model_or_actor, cost_usd,
        input_tokens, output_tokens,
        cache_read_tokens, cache_creation_tokens, upstream_ref,
        1 if provisional else 0,
    )
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO spend (
                friend_id, vendor, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, upstream_ref,
                provisional
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        if cur.rowcount > 0:
            return True
        # The insert lost to the partial unique index, so a row already exists:
        # overwrite it with this observation and report "not new".
        conn.execute(
            """
            UPDATE spend SET
                friend_id = ?, model_or_actor = ?, cost_usd = ?,
                input_tokens = ?, output_tokens = ?,
                cache_read_tokens = ?, cache_creation_tokens = ?,
                provisional = ?
            WHERE vendor = ? AND upstream_ref = ?
            """,
            (
                friend_id, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                1 if provisional else 0,
                vendor, upstream_ref,
            ),
        )
        return False


def get_spend_by_ref(vendor: str, upstream_ref: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM spend WHERE vendor = ? AND upstream_ref = ?",
            (vendor, upstream_ref),
        ).fetchone()
        return dict(row) if row else None


def list_provisional_spend(
    vendor: str = "apify", min_age_seconds: int = 0
) -> list[dict[str, Any]]:
    """Provisional rows old enough that the vendor's usage figure has settled.

    Age is measured in SQLite's own clock so it matches the created_at default.
    """
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM spend WHERE vendor = ? AND provisional = 1 "
            "AND upstream_ref IS NOT NULL "
            "AND created_at <= datetime('now', ?) ORDER BY id",
            (vendor, f"-{int(min_age_seconds)} seconds"),
        ).fetchall()
        return [dict(r) for r in rows]


def settle_spend(spend_id: int, cost_usd: float) -> None:
    """Replace a provisional estimate with the vendor's settled figure."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE spend SET cost_usd = ?, provisional = 0 WHERE id = ?",
            (cost_usd, spend_id),
        )


def friend_month_provisional(friend_id: int, month: str | None = None) -> float:
    """Portion of friend_month_spend that is still a worst-case estimate."""
    start, end = month_bounds(month)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
            "WHERE friend_id = ? AND provisional = 1 "
            "AND created_at >= ? AND created_at < ?",
            (friend_id, start, end),
        ).fetchone()
        return float(row["total"])


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
