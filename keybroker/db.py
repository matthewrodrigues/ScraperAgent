"""Broker persistence: friends and spend.

Deliberately tiny. The broker stores no domain data — no searches, listings,
negotiations, or eBay credentials. Two tables is the whole model.

Conventions mirror db/schema.sql: integer PKs, ISO-8601 UTC text timestamps
written by SQLite's datetime('now'), cost_usd REAL, cascading FKs.

Backend differences (SQLite vs Supabase Postgres) live in keybroker.dialect;
this module keeps the SQL and asks the active dialect for a connection, a
placeholder style, and the few clauses the two backends spell differently.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from keybroker import dialect


def _q(sql: str) -> str:
    """Translate this module's ?-style placeholders to the active dialect's.

    A plain replace is safe here only because none of the SQL below contains a
    literal '?' inside a string. Keep it that way; if a query ever needs one,
    parameterise it instead of escaping.
    """
    ph = dialect.active().placeholder
    return sql if ph == "?" else sql.replace("?", ph)


def _commit(conn) -> None:
    """SQLite runs in autocommit (isolation_level=None); psycopg does not."""
    if dialect.active().name == "postgres":
        conn.commit()


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


@contextmanager
def get_conn() -> Iterator[Any]:
    """A connection from the active dialect, with dict-like rows."""
    with dialect.active().connect() as conn:
        yield conn


def _apply_column_migrations(conn) -> None:
    """Add any column in _COLUMN_MIGRATIONS that the table does not yet have.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
    a schema addition needs this to reach a database created before it.
    """
    d = dialect.active()
    for table, col, coltype in _COLUMN_MIGRATIONS:
        sql, params = d.introspect_columns_sql(table)
        rows = conn.execute(sql, params if d.name == "postgres" else ()).fetchall()
        # SQLite's PRAGMA returns (cid, name, type, ...); Postgres returns
        # {"column_name": ...}. Normalise to a set of names.
        existing = {
            (r["column_name"] if d.name == "postgres" else r[1]) for r in rows
        }
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


def init_db() -> None:
    """Create both tables and apply any pending column migrations."""
    with get_conn() as conn:
        for statement in dialect.active().schema_sql():
            conn.execute(statement)
        _apply_column_migrations(conn)
        if dialect.active().name == "postgres":
            conn.commit()


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
        row = conn.execute(
            _q(
                "INSERT INTO friends (name, token_sha256, monthly_budget_usd) "
                "VALUES (?, ?, ?) RETURNING id"
            ),
            (name, token_sha256, monthly_budget_usd),
        ).fetchone()
        _commit(conn)
        return int(row["id"] if not isinstance(row, tuple) else row[0])


def get_friend_by_token_hash(token_sha256: str) -> dict[str, Any] | None:
    """Active friends only — a revoked token is indistinguishable from unknown."""
    with get_conn() as conn:
        row = conn.execute(
            _q("SELECT * FROM friends WHERE token_sha256 = ? AND revoked_at IS NULL"),
            (token_sha256,),
        ).fetchone()
        return dict(row) if row else None


def get_friend_by_name(name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            _q("SELECT * FROM friends WHERE name = ?"), (name,)
        ).fetchone()
        return dict(row) if row else None


def revoke_friend(name: str) -> bool:
    """Set revoked_at. Never deletes — spend history outlives the friendship."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            _q(
                "UPDATE friends SET revoked_at = ? "
                "WHERE name = ? AND revoked_at IS NULL"
            ),
            (now, name),
        )
        _commit(conn)
        return cur.rowcount > 0


def list_friends() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(_q("SELECT * FROM friends ORDER BY name")).fetchall()
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
            _q(
                """
                INSERT OR IGNORE INTO spend (
                    friend_id, vendor, model_or_actor, cost_usd,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens, upstream_ref,
                    provisional
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            row,
        )
        if cur.rowcount > 0:
            _commit(conn)
            return True
        # The insert lost to the partial unique index, so a row already exists:
        # overwrite it with this observation and report "not new".
        conn.execute(
            _q(
                """
                UPDATE spend SET
                    friend_id = ?, model_or_actor = ?, cost_usd = ?,
                    input_tokens = ?, output_tokens = ?,
                    cache_read_tokens = ?, cache_creation_tokens = ?,
                    provisional = ?
                WHERE vendor = ? AND upstream_ref = ?
                """
            ),
            (
                friend_id, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                1 if provisional else 0,
                vendor, upstream_ref,
            ),
        )
        _commit(conn)
        return False


def get_spend_by_ref(vendor: str, upstream_ref: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            _q("SELECT * FROM spend WHERE vendor = ? AND upstream_ref = ?"),
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
            _q(
                "SELECT * FROM spend WHERE vendor = ? AND provisional = 1 "
                "AND upstream_ref IS NOT NULL "
                "AND created_at <= datetime('now', ?) ORDER BY id"
            ),
            (vendor, f"-{int(min_age_seconds)} seconds"),
        ).fetchall()
        return [dict(r) for r in rows]


def settle_spend(spend_id: int, cost_usd: float) -> None:
    """Replace a provisional estimate with the vendor's settled figure."""
    with get_conn() as conn:
        conn.execute(
            _q("UPDATE spend SET cost_usd = ?, provisional = 0 WHERE id = ?"),
            (cost_usd, spend_id),
        )
        _commit(conn)


def friend_month_provisional(friend_id: int, month: str | None = None) -> float:
    """Portion of friend_month_spend that is still a worst-case estimate."""
    start, end = month_bounds(month)
    clause = dialect.active().month_filter("created_at")
    with get_conn() as conn:
        row = conn.execute(
            _q(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
                f"WHERE friend_id = ? AND provisional = 1 AND {clause}"
            ),
            (friend_id, start, end),
        ).fetchone()
        return float(row["total"])


def friend_month_spend(friend_id: int, month: str | None = None) -> float:
    start, end = month_bounds(month)
    clause = dialect.active().month_filter("created_at")
    with get_conn() as conn:
        row = conn.execute(
            _q(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
                f"WHERE friend_id = ? AND {clause}"
            ),
            (friend_id, start, end),
        ).fetchone()
        return float(row["total"])


def global_month_spend(month: str | None = None) -> float:
    start, end = month_bounds(month)
    clause = dialect.active().month_filter("created_at")
    with get_conn() as conn:
        row = conn.execute(
            _q(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
                f"WHERE {clause}"
            ),
            (start, end),
        ).fetchone()
        return float(row["total"])
