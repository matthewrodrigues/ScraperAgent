"""Backend differences between SQLite and Supabase Postgres, in one place.

keybroker.db keeps its functions and most of its SQL; it asks a dialect for a
connection, a placeholder style, and the few clauses the two backends spell
differently.

Modern SQLite (3.24+ upsert, 3.8+ partial indexes, 3.35+ RETURNING) means the
interesting queries — the record_spend upsert, its conflict target, and
create_friend — are written once in db.py rather than twice here.

Selection is decided at import by whether config.SUPABASE_DB_URL is set.
Connecting is not: constructing a dialect must never open a socket, or
importing keybroker.db would dial Supabase from the test suite and from every
operator script.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

import config


_FRIENDS_COLUMNS = """
    name               TEXT NOT NULL UNIQUE,
    token_sha256       TEXT NOT NULL UNIQUE,
    monthly_budget_usd {money} NOT NULL DEFAULT 5.0,
    created_at         {ts} NOT NULL DEFAULT {now},
    revoked_at         {ts}
"""

_SPEND_COLUMNS = """
    friend_id             INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    vendor                TEXT NOT NULL,
    model_or_actor        TEXT,
    cost_usd              {money} NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    upstream_ref          TEXT,
    provisional           INTEGER NOT NULL DEFAULT 0,
    created_at            {ts} NOT NULL DEFAULT {now}
"""


class SqliteDialect:
    name = "sqlite"
    placeholder = "?"

    def schema_sql(self) -> list[str]:
        fmt = {"money": "REAL", "ts": "TEXT", "now": "(datetime('now'))"}
        return [
            f"CREATE TABLE IF NOT EXISTS friends (\n"
            f"    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            f"{_FRIENDS_COLUMNS.format(**fmt)})",
            f"CREATE TABLE IF NOT EXISTS spend (\n"
            f"    id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
            f"{_SPEND_COLUMNS.format(**fmt)})",
            "CREATE INDEX IF NOT EXISTS idx_spend_friend_time ON spend(friend_id, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_upstream "
            "ON spend(vendor, upstream_ref) WHERE upstream_ref IS NOT NULL",
        ]

    def introspect_columns_sql(self, table: str) -> tuple[str, tuple]:
        # PRAGMA takes no bind parameters, hence the interpolation. `table` is
        # never user input — it comes from _COLUMN_MIGRATIONS in db.py.
        return (f"PRAGMA table_info({table})", (table,))

    def month_filter(self, column: str) -> str:
        # SQLite's datetime('now') is already UTC, so a plain lexical
        # comparison of "YYYY-MM-DD HH:MM:SS" strings is correct.
        return f"{column} >= ? AND {column} < ?"

    @contextmanager
    def connect(self) -> Iterator[Any]:
        conn = sqlite3.connect(config.BROKER_DB_PATH, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            yield conn
        finally:
            conn.close()

    def open_pool(self) -> None:
        """No pool: SQLite connections are per-call and cost nothing."""

    def close_pool(self) -> None:
        """No pool."""


class PostgresDialect:
    name = "postgres"
    placeholder = "%s"

    def __init__(self, url: str | None = None) -> None:
        # Resolved lazily so constructing a dialect never opens a socket.
        self._url = url
        self._pool: Any = None

    def schema_sql(self) -> list[str]:
        fmt = {"money": "DOUBLE PRECISION", "ts": "TIMESTAMPTZ", "now": "now()"}
        return [
            f"CREATE TABLE IF NOT EXISTS friends (\n"
            f"    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n"
            f"{_FRIENDS_COLUMNS.format(**fmt)})",
            f"CREATE TABLE IF NOT EXISTS spend (\n"
            f"    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,\n"
            f"{_SPEND_COLUMNS.format(**fmt)})",
            "CREATE INDEX IF NOT EXISTS idx_spend_friend_time ON spend(friend_id, created_at)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_upstream "
            "ON spend(vendor, upstream_ref) WHERE upstream_ref IS NOT NULL",
        ]

    def introspect_columns_sql(self, table: str) -> tuple[str, tuple]:
        return (
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s AND table_schema = current_schema()",
            (table,),
        )

    def month_filter(self, column: str) -> str:
        # AT TIME ZONE 'UTC' pins the interpretation of the naive bound strings
        # month_bounds() produces. Without it, now() and the comparison resolve
        # in the SESSION's timezone: month boundaries would shift by hours,
        # silently, and only near the 1st. Deliberately NOT solved with
        # `SET TIME ZONE` on connect — that is session state and unreliable
        # under a transaction pooler. See spec section 4.1.
        return (
            f"{column} >= (%s AT TIME ZONE 'UTC') "
            f"AND {column} < (%s AT TIME ZONE 'UTC')"
        )

    def open_pool(self) -> None:
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        if self._pool is not None:
            return
        url = self._url or config.SUPABASE_DB_URL
        if not url:
            raise RuntimeError("SUPABASE_DB_URL is not set but the Postgres dialect is active.")
        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=5,
            open=True,
            # psycopg 3 server-prepares statements after a few executions,
            # which pgBouncer's transaction mode rejects. Disabling it means
            # moving to Supabase's pooler later is a URL change, not a debug
            # session.
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
        )

    def close_pool(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    @contextmanager
    def connect(self) -> Iterator[Any]:
        if self._pool is None:
            # Operator scripts run outside the FastAPI lifespan, so they open
            # the pool on first use and close it on exit.
            self.open_pool()
        with self._pool.connection() as conn:
            yield conn


_active: Any = None


def active():
    """The dialect for this process. Chosen once, by whether SUPABASE_DB_URL is
    set — never re-read, so a process cannot switch backends mid-flight."""
    global _active
    if _active is None:
        _active = PostgresDialect() if config.SUPABASE_DB_URL else SqliteDialect()
    return _active


def reset_for_tests() -> None:
    """Drop the cached dialect. Tests patch config and call this; production
    never does."""
    global _active
    if _active is not None:
        _active.close_pool()
    _active = None
