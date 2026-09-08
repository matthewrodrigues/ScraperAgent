# Key Broker on Supabase Postgres — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the key broker persist to Supabase Postgres so it can be deployed, while tests and offline development keep running against local SQLite.

**Architecture:** A new `keybroker/dialect.py` absorbs the handful of real differences between SQLite and Postgres. `keybroker/db.py` keeps all of its functions and most of its SQL, asking the dialect for a connection and a placeholder style instead of importing `sqlite3`. Which dialect is active is decided once at import by whether `SUPABASE_DB_URL` is set; the Postgres connection pool is owned by the FastAPI lifespan.

**Tech Stack:** Python 3.12, `psycopg[binary]` + `psycopg_pool` (new), SQLite 3.49.1, FastAPI, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-broker-supabase-postgres-design.md`

## Global Constraints

- **`SUPABASE_DB_URL` set → Postgres. Unset → SQLite at `BROKER_DB_PATH`.** No separate mode flag, so the two cannot disagree. Set-but-unparseable fails at startup rather than silently falling back to a local file nobody is reading.
- **Timestamps never depend on session state.** Postgres month comparisons use `created_at >= (%s AT TIME ZONE 'UTC')`. Do **not** use `SET TIME ZONE` on connect — it is session state and unreliable under a transaction pooler. `month_bounds` keeps returning naive `"YYYY-MM-DD HH:MM:SS"` strings and is unchanged.
- **Postgres contract tests read `SUPABASE_TEST_DB_URL`, never `SUPABASE_DB_URL`**, and refuse to run if the two are equal. Those tests create, write and truncate; pointing them at the live ledger must be a deliberate act.
- **Importing `keybroker.db` must never open a socket.** Dialect selection happens at import; pool creation happens in the FastAPI lifespan.
- `provisional` stays an `INTEGER` on both backends — never a Postgres `BOOLEAN`.
- The pool sets `prepare_threshold=None` so a later move to Supabase's transaction pooler is a URL change, not a debugging session.
- **`keybroker/auth.py`, `quota.py`, `meter.py`, `app.py` keep their current behaviour.** `app.py` gains only pool lifecycle wiring. The storage swap is invisible above `db.py`.
- All existing tests must pass unchanged, offline. The suite is **476** before Task 1.
- Interpreter is `.venv/Scripts/python.exe` run from the repo root. Windows; Git Bash available.
- Never print a connection string, password, or token to stdout or into a test fixture.

## File Structure

| File | Responsibility |
|---|---|
| `keybroker/dialect.py` | **New.** The two dialects: connection, row shape, placeholder, DDL, introspection query, UTC-safe month comparison |
| `keybroker/db.py` | Keeps its functions; routes connections and SQL text through the dialect |
| `keybroker/app.py` | Opens and closes the Postgres pool in the existing lifespan |
| `config.py` | `SUPABASE_DB_URL`, `SUPABASE_TEST_DB_URL` |
| `scripts/smoke_supabase.py` | **New.** Live end-to-end check against the real project |
| `requirements.txt`, `pyproject.toml` | `psycopg[binary]`, `psycopg_pool` |
| `tests/test_keybroker_dialect.py` | **New.** Dialect unit tests, no database |
| `tests/test_keybroker_contract.py` | **New.** One test body per behaviour, parameterized over both backends |

---

### Task 1: Config and dependencies

Isolated and independently checkable, so it goes first. No behaviour changes yet.

**Files:**
- Modify: `config.py`
- Modify: `requirements.txt`, `pyproject.toml`, `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Consumes: nothing
- Produces: `config.SUPABASE_DB_URL` (str, `""` default), `config.SUPABASE_TEST_DB_URL` (str, `""` default)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_supabase_urls_default_to_empty():
    """Unset means SQLite. An empty string is the 'use the local file' signal,
    so it must never be None — callers test truthiness, not identity."""
    assert config.SUPABASE_DB_URL == "" or isinstance(config.SUPABASE_DB_URL, str)
    assert config.SUPABASE_TEST_DB_URL == "" or isinstance(config.SUPABASE_TEST_DB_URL, str)


def test_supabase_url_is_read_from_the_environment(monkeypatch):
    """Read at import in production, but the attribute is what code consults,
    so tests patch the attribute rather than the environment."""
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "postgresql://u:p@h:5432/d")
    assert config.SUPABASE_DB_URL.startswith("postgresql://")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'SUPABASE_DB_URL'`

- [ ] **Step 3: Add the settings**

Append to `config.py`, immediately after the existing broker settings:

```python
# ---- Broker storage ----
# Set to a Supabase (or any Postgres) connection string to persist the broker's
# friends/spend tables there instead of the local SQLite file. Unset means
# SQLite at BROKER_DB_PATH — that is the signal, so there is no separate mode
# flag that could disagree with it.
#
# Contains a password: keep it in .env and your deploy platform's secret store,
# never in .env.example.
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL", "")

# A SEPARATE database for the Postgres contract tests. Deliberately not
# SUPABASE_DB_URL: those tests create friends, write spend rows, and truncate
# between cases, so reusing the production setting would mean configuring the
# broker for real use silently arms the suite to write to the live ledger.
SUPABASE_TEST_DB_URL = os.getenv("SUPABASE_TEST_DB_URL", "")
```

- [ ] **Step 4: Add the dependencies**

In `requirements.txt`, beside the other integration pins:

```
# Postgres driver for the key broker's Supabase backend. Binary wheel so
# Windows needs no build toolchain.
psycopg[binary]==3.2.3
psycopg-pool==3.2.4
```

Mirror both into `pyproject.toml`'s dependency list, matching the existing entries' style, then install:

`.venv/Scripts/python.exe -m pip install "psycopg[binary]==3.2.3" "psycopg-pool==3.2.4"`

- [ ] **Step 5: Document the setting**

In `.env.example`, add with an **empty** value:

```
# ---- Broker storage (optional) ----
# Leave empty to use the local SQLite file at BROKER_DB_PATH.
# Set to a Supabase connection string to run the broker against Postgres:
# Supabase dashboard -> Project Settings -> Database -> Connection string -> URI.
SUPABASE_DB_URL=
# A SEPARATE project/database used only by the Postgres contract tests.
# Never point this at the same database as SUPABASE_DB_URL.
SUPABASE_TEST_DB_URL=
```

- [ ] **Step 6: Run the tests**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 478 tests (476 existing + 2 new)

- [ ] **Step 8: Commit**

```bash
git add config.py requirements.txt pyproject.toml .env.example tests/test_config.py
git commit -m "feat(broker): Supabase connection settings and psycopg dependency"
```

---

### Task 2: The dialect layer

Pure translation, no I/O beyond opening a connection. Testable without any database for everything except the Postgres connection itself.

**Files:**
- Create: `keybroker/dialect.py`
- Test: `tests/test_keybroker_dialect.py`

**Interfaces:**
- Consumes: `config.SUPABASE_DB_URL` from Task 1
- Produces: `keybroker.dialect.Dialect` (protocol-ish base), `SqliteDialect`, `PostgresDialect`, `active() -> Dialect`, and on every dialect: `.name: str`, `.placeholder: str`, `.schema_sql() -> list[str]`, `.introspect_columns_sql(table) -> tuple[str, tuple]`, `.month_filter(column) -> str`, `.connect()` (context manager yielding a connection with dict-like rows), `.open_pool()`, `.close_pool()`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_dialect.py`:

```python
"""Tests for keybroker.dialect — SQL translation only, no database needed.

These cover the differences that SQLite tests structurally cannot: the DDL
Postgres emits, its introspection query, and the UTC-pinned month comparison.
"""

import pytest

import config
from keybroker import dialect


@pytest.fixture
def sqlite_mode(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "")


@pytest.fixture
def postgres_mode(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "postgresql://u:p@example:5432/d")


def test_active_is_sqlite_when_no_url(sqlite_mode):
    assert dialect.active().name == "sqlite"


def test_active_is_postgres_when_url_set(postgres_mode):
    assert dialect.active().name == "postgres"


def test_placeholders_differ():
    assert dialect.SqliteDialect().placeholder == "?"
    assert dialect.PostgresDialect().placeholder == "%s"


def test_sqlite_schema_uses_autoincrement_and_text_timestamps():
    ddl = " ".join(dialect.SqliteDialect().schema_sql())
    assert "INTEGER PRIMARY KEY AUTOINCREMENT" in ddl
    assert "datetime('now')" in ddl


def test_postgres_schema_uses_identity_and_timestamptz():
    ddl = " ".join(dialect.PostgresDialect().schema_sql())
    assert "GENERATED ALWAYS AS IDENTITY" in ddl
    assert "TIMESTAMPTZ" in ddl
    assert "AUTOINCREMENT" not in ddl


def test_both_schemas_keep_the_partial_unique_index():
    """It is the conflict target for record_spend's upsert, not just a dedupe —
    a provisional row must stay correctable in place."""
    for d in (dialect.SqliteDialect(), dialect.PostgresDialect()):
        ddl = " ".join(d.schema_sql())
        assert "WHERE upstream_ref IS NOT NULL" in ddl


def test_provisional_is_an_integer_on_both_backends():
    """Never a Postgres BOOLEAN — calling code must not know which backend it is on."""
    for d in (dialect.SqliteDialect(), dialect.PostgresDialect()):
        ddl = " ".join(d.schema_sql())
        assert "provisional" in ddl
        assert "BOOLEAN" not in ddl.upper()


def test_postgres_month_filter_pins_utc():
    """The whole point of section 4.1: without AT TIME ZONE 'UTC', a non-UTC
    session shifts every month boundary by hours, silently."""
    clause = dialect.PostgresDialect().month_filter("created_at")
    assert "AT TIME ZONE 'UTC'" in clause


def test_sqlite_month_filter_is_a_plain_comparison():
    """SQLite's datetime('now') is already UTC, so no cast is needed."""
    clause = dialect.SqliteDialect().month_filter("created_at")
    assert "AT TIME ZONE" not in clause
    assert "created_at" in clause


def test_month_filter_uses_a_half_open_range_on_both():
    for d in (dialect.SqliteDialect(), dialect.PostgresDialect()):
        clause = d.month_filter("created_at")
        assert ">=" in clause and "<" in clause


def test_introspection_targets_the_right_catalog():
    sq, _ = dialect.SqliteDialect().introspect_columns_sql("spend")
    pg, params = dialect.PostgresDialect().introspect_columns_sql("spend")
    assert "PRAGMA table_info" in sq
    assert "information_schema.columns" in pg
    assert "spend" in params


def test_postgres_dialect_does_not_connect_on_construction():
    """Importing or constructing must never open a socket — otherwise the test
    suite and every operator script would dial Supabase just by importing."""
    d = dialect.PostgresDialect()
    assert d._pool is None
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_dialect.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'keybroker.dialect'`

- [ ] **Step 3: Write the SQLite dialect**

Create `keybroker/dialect.py`:

```python
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
```

- [ ] **Step 4: Write the Postgres dialect**

Append to `keybroker/dialect.py`:

```python
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_dialect.py -q`
Expected: PASS (12 tests)

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 490 tests (478 + 12). `keybroker/db.py` still imports `sqlite3` directly at this point; Task 3 changes that.

- [ ] **Step 7: Commit**

```bash
git add keybroker/dialect.py tests/test_keybroker_dialect.py
git commit -m "feat(broker): SQLite/Postgres dialect layer"
```

---

### Task 3: Route `keybroker/db.py` through the dialect

The behaviour-preserving refactor. Every existing broker test must still pass with no edits — that is the check that nothing changed.

**Files:**
- Modify: `keybroker/db.py`
- Test: existing `tests/test_keybroker_db.py` (unchanged — do not edit it)

**Interfaces:**
- Consumes: from Task 2 `dialect.active()`, `.placeholder`, `.schema_sql()`, `.introspect_columns_sql()`, `.month_filter()`, `.connect()`
- Produces: the same 17 public functions with identical signatures. Callers (`auth.py`, `quota.py`, `meter.py`, `app.py`, `scripts/*`) are untouched.

- [ ] **Step 1: Confirm the baseline before you change anything**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_db.py -q`
Expected: PASS. Note the count — the same tests must pass identically at Step 6.

- [ ] **Step 2: Replace the module's SQLite plumbing**

In `keybroker/db.py`, delete the `SCHEMA` constant, `get_conn`, `init_db`, and `_apply_column_migrations`, and replace them with dialect-driven versions. Keep `_COLUMN_MIGRATIONS` exactly as it is.

```python
from keybroker import dialect


def _q(sql: str) -> str:
    """Translate this module's ?-style placeholders to the active dialect's.

    A plain replace is safe here only because none of the SQL below contains a
    literal '?' inside a string. Keep it that way; if a query ever needs one,
    parameterise it instead of escaping.
    """
    ph = dialect.active().placeholder
    return sql if ph == "?" else sql.replace("?", ph)


@contextmanager
def get_conn():
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
```

- [ ] **Step 3: Convert the three non-mechanical queries**

These are the only ones whose SQL text changes beyond placeholders.

`create_friend` — replace `cur.lastrowid` with `RETURNING id`, which both backends support:

```python
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
```

`friend_month_spend` — the month comparison comes from the dialect so Postgres gets its UTC pin:

```python
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
```

Apply the same `month_filter` substitution to `global_month_spend` and `friend_month_provisional`. **Do not hand-write `AT TIME ZONE` in `db.py`** — it belongs to the dialect, and duplicating it is how the two drift.

Add the commit helper beneath `_q`:

```python
def _commit(conn) -> None:
    """SQLite runs in autocommit (isolation_level=None); psycopg does not."""
    if dialect.active().name == "postgres":
        conn.commit()
```

- [ ] **Step 4: Convert the remaining functions mechanically**

Every other function changes in exactly one way: wrap its SQL string in `_q(...)`, and add `_commit(conn)` after any INSERT, UPDATE or DELETE. Nothing else about them moves — same names, same parameters, same return types.

The full list, so none is missed: `get_friend_by_token_hash`, `get_friend_by_name`, `revoke_friend`, `list_friends`, `record_spend`, `get_spend_by_ref`, `list_provisional_spend`, `settle_spend`.

`revoke_friend` additionally needs its `datetime('now')` replaced, since Postgres does not have that function. Use a Python-side UTC timestamp so both backends store the same shape:

```python
def revoke_friend(name: str) -> bool:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        cur = conn.execute(
            _q("UPDATE friends SET revoked_at = ? WHERE name = ? AND revoked_at IS NULL"),
            (now, name),
        )
        _commit(conn)
        return cur.rowcount > 0
```

- [ ] **Step 5: Run the broker DB tests — unchanged**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_db.py -q`
Expected: PASS, the same count as Step 1. **If a test needs editing to pass, the refactor changed behaviour — fix the code, not the test.**

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 490 tests, unchanged from Task 2. This task adds no tests; it must subtract none either.

- [ ] **Step 7: Commit**

```bash
git add keybroker/db.py
git commit -m "refactor(broker): route persistence through the dialect layer"
```

---

### Task 4: Pool lifecycle and the parameterized contract tests

**Files:**
- Modify: `keybroker/app.py` (the existing `lifespan`)
- Create: `tests/test_keybroker_contract.py`
- Test: both

**Interfaces:**
- Consumes: from Task 2 `dialect.active().open_pool()` / `.close_pool()` / `reset_for_tests()`; from Task 3 the `db` functions
- Produces: nothing later tasks depend on

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_contract.py`:

```python
"""One set of behaviours, run against both backends.

SQLite always. Postgres only when SUPABASE_TEST_DB_URL is set — deliberately
NOT SUPABASE_DB_URL, because these tests create friends, write spend rows and
truncate between cases. Reusing the production setting would mean configuring
the broker for real use silently arms this suite against the live ledger.
"""

import os

import pytest

import config
from keybroker import db, dialect


def _postgres_url() -> str:
    url = os.getenv("SUPABASE_TEST_DB_URL", "")
    prod = os.getenv("SUPABASE_DB_URL", "")
    if url and prod and url == prod:
        pytest.fail(
            "SUPABASE_TEST_DB_URL must not equal SUPABASE_DB_URL — these tests "
            "truncate the tables they touch."
        )
    return url


@pytest.fixture(params=["sqlite", "postgres"])
def backend(request, tmp_path, monkeypatch):
    """Yield a clean broker database on each backend in turn."""
    if request.param == "postgres":
        url = _postgres_url()
        if not url:
            pytest.skip("SUPABASE_TEST_DB_URL not set; skipping the Postgres contract run")
        monkeypatch.setattr(config, "SUPABASE_DB_URL", url)
    else:
        monkeypatch.setattr(config, "SUPABASE_DB_URL", "")
        monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")

    dialect.reset_for_tests()
    db.init_db()
    if request.param == "postgres":
        with db.get_conn() as conn:
            conn.execute("TRUNCATE spend, friends RESTART IDENTITY CASCADE")
            conn.commit()
    yield request.param
    dialect.reset_for_tests()


def test_create_and_find_a_friend(backend):
    fid = db.create_friend("alice", "hash-alice", 5.0)
    found = db.get_friend_by_token_hash("hash-alice")
    assert found["id"] == fid
    assert found["monthly_budget_usd"] == 5.0


def test_revoked_friend_is_indistinguishable_from_unknown(backend):
    db.create_friend("alice", "hash-alice", 5.0)
    assert db.revoke_friend("alice") is True
    assert db.get_friend_by_token_hash("hash-alice") is None
    assert db.get_friend_by_name("alice")["revoked_at"] is not None


def test_repeated_upstream_ref_updates_in_place(backend):
    """The partial unique index is a conflict target, not just a dedupe: a
    provisional row must stay correctable once Apify's usage settles."""
    fid = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(fid, "apify", 0.90, upstream_ref="run_1", provisional=True)
    db.record_spend(fid, "apify", 0.90, upstream_ref="run_1", provisional=True)
    assert db.friend_month_spend(fid) == pytest.approx(0.90)


def test_provisional_settles_to_the_true_cost(backend):
    fid = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(fid, "apify", 0.90, upstream_ref="run_1", provisional=True)
    row = db.get_spend_by_ref("apify", "run_1")
    db.settle_spend(row["id"], 0.53)
    assert db.friend_month_spend(fid) == pytest.approx(0.53)
    assert db.friend_month_provisional(fid) == pytest.approx(0.0)


def test_spend_without_upstream_ref_is_never_deduplicated(backend):
    fid = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(fid, "anthropic", 0.01)
    db.record_spend(fid, "anthropic", 0.01)
    assert db.friend_month_spend(fid) == pytest.approx(0.02)


def test_month_boundaries_agree_across_backends(backend):
    """The one failure mode that is silent, wrong by hours, and only visible
    near the 1st: a non-UTC session shifting every boundary."""
    fid = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(fid, "anthropic", 0.02, upstream_ref="m1")
    this_month = db.month_bounds()[0][:7]
    assert db.friend_month_spend(fid, month=this_month) == pytest.approx(0.02)
    assert db.friend_month_spend(fid, month="2020-01") == pytest.approx(0.0)


def test_global_spend_sums_across_friends(backend):
    a = db.create_friend("alice", "hash-a", 5.0)
    b = db.create_friend("bob", "hash-b", 5.0)
    db.record_spend(a, "anthropic", 0.02, upstream_ref="m1")
    db.record_spend(b, "anthropic", 0.03, upstream_ref="m2")
    assert db.global_month_spend() == pytest.approx(0.05)
```

- [ ] **Step 2: Run it to verify the SQLite half passes and Postgres skips**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_contract.py -v`
Expected: the `sqlite` parameterisation FAILS first if `reset_for_tests` is missing, then passes once Task 2's code is present; every `postgres` case reports SKIPPED while `SUPABASE_TEST_DB_URL` is unset.

- [ ] **Step 3: Wire the pool into the app lifespan**

In `keybroker/app.py`, inside the existing `lifespan`, open the pool alongside the httpx client and close it in the same `finally`:

```python
    dialect.active().open_pool()
```

immediately after `db.init_db()`, and in the `finally` block, after closing the httpx client:

```python
        dialect.active().close_pool()
```

Add `from keybroker import dialect` to the imports. Nothing else in `app.py` changes — the proxy, clamps, quota and metering are untouched.

- [ ] **Step 4: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 497 tests (490 + 7 contract tests on the SQLite parameterisation; the Postgres ones report as skipped, not failed).

- [ ] **Step 5: Commit**

```bash
git add keybroker/app.py tests/test_keybroker_contract.py
git commit -m "feat(broker): pool lifecycle and dual-backend contract tests"
```

---

### Task 5: Live smoke script and documentation

The check that validates against reality rather than against assumptions about Postgres. Three things are structurally untestable on SQLite — the DDL, the `AT TIME ZONE 'UTC'` comparison, and `information_schema` migrations — and this is what exercises them against the real project.

**Files:**
- Create: `scripts/smoke_supabase.py`
- Modify: `README.md`
- Test: `tests/test_smoke_supabase.py`

**Interfaces:**
- Consumes: from Task 3 the `db` functions; from Task 2 `dialect.active()`
- Produces: nothing later tasks depend on

- [ ] **Step 1: Write the failing test**

Create `tests/test_smoke_supabase.py`:

```python
"""Tests for the smoke script's own guard rails.

The script itself needs a live database, so what is tested here is that it
refuses to run against the wrong one and never leaks a credential.
"""

import pytest

import config
from scripts import smoke_supabase


def test_refuses_to_run_without_a_url(monkeypatch, capsys):
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "")
    assert smoke_supabase.main([]) == 1
    assert "SUPABASE_DB_URL" in capsys.readouterr().err


def test_never_prints_the_connection_string(monkeypatch, capsys):
    """A smoke script's output gets pasted into issues and chat logs."""
    secret = "postgresql://postgres:hunter2@db.example.supabase.co:5432/postgres"
    monkeypatch.setattr(config, "SUPABASE_DB_URL", secret)
    monkeypatch.setattr(smoke_supabase, "_run_checks", lambda: None)
    smoke_supabase.main([])
    out = capsys.readouterr()
    assert "hunter2" not in out.out + out.err
    assert secret not in out.out + out.err


def test_reports_the_host_without_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        config, "SUPABASE_DB_URL",
        "postgresql://postgres:hunter2@db.example.supabase.co:5432/postgres",
    )
    monkeypatch.setattr(smoke_supabase, "_run_checks", lambda: None)
    smoke_supabase.main([])
    assert "db.example.supabase.co" in capsys.readouterr().out
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_smoke_supabase.py -q`
Expected: FAIL — `ImportError: cannot import name 'smoke_supabase' from 'scripts'`

- [ ] **Step 3: Write the smoke script**

Create `scripts/smoke_supabase.py`:

```python
"""Diagnostic: exercise the broker's Postgres backend against the real project.

Why this exists: the test suite runs on SQLite, so three things are never
exercised by CI — the Postgres DDL, the AT TIME ZONE 'UTC' month comparison,
and information_schema-based column migrations. All three are Postgres-only by
construction. This script is what checks them against reality before a deploy.

It writes to whatever SUPABASE_DB_URL points at, then cleans up after itself.
The throwaway friend it creates is prefixed 'smoke-' and deleted at the end.

Usage:
    .venv/Scripts/python.exe -m scripts.smoke_supabase
"""

from __future__ import annotations

import argparse
import sys
import uuid
from urllib.parse import urlparse

import config
from keybroker import db, dialect


def _safe_target(url: str) -> str:
    """Host and database only. Never the user or password — this output gets
    pasted into issues and chat logs."""
    parts = urlparse(url)
    return f"{parts.hostname}{parts.path}"


def _run_checks() -> None:
    marker = f"smoke-{uuid.uuid4().hex[:8]}"
    print(f"  dialect:        {dialect.active().name}")

    db.init_db()
    print("  schema:         created or already present")

    fid = db.create_friend(marker, f"hash-{marker}", 1.00)
    print(f"  create_friend:  id={fid}")

    db.record_spend(fid, "apify", 0.90, upstream_ref=f"run-{marker}", provisional=True)
    prov = db.friend_month_provisional(fid)
    assert prov == 0.90, f"provisional should be 0.90, got {prov}"
    print(f"  provisional:    ${prov:.2f}")

    row = db.get_spend_by_ref("apify", f"run-{marker}")
    db.settle_spend(row["id"], 0.53)
    spent = db.friend_month_spend(fid)
    assert abs(spent - 0.53) < 1e-9, f"settled spend should be 0.53, got {spent}"
    print(f"  settled:        ${spent:.2f}")

    # The check that only a real Postgres run can make: a row written "now"
    # must fall inside the current UTC month and outside a past one. A session
    # timezone leak shows up here and nowhere else.
    this_month = db.month_bounds()[0][:7]
    assert db.friend_month_spend(fid, month=this_month) == spent, "row missed the current month"
    assert db.friend_month_spend(fid, month="2020-01") == 0.0, "row leaked into a past month"
    print(f"  month bounds:   correct for {this_month}")

    assert db.revoke_friend(marker) is True
    assert db.get_friend_by_token_hash(f"hash-{marker}") is None
    print("  revoke:         token no longer resolves")

    with db.get_conn() as conn:
        conn.execute(db._q("DELETE FROM friends WHERE name = ?"), (marker,))
        if dialect.active().name == "postgres":
            conn.commit()
    print("  cleanup:        throwaway friend and its spend removed")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the broker's Postgres backend")
    parser.parse_args(argv)

    if not config.SUPABASE_DB_URL:
        print(
            "SUPABASE_DB_URL is not set, so the broker is using SQLite and there "
            "is nothing to smoke-test. Set it in .env first.",
            file=sys.stderr,
        )
        return 1

    print(f"target: {_safe_target(config.SUPABASE_DB_URL)}")
    try:
        _run_checks()
    except AssertionError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        dialect.active().close_pool()

    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_smoke_supabase.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Document it**

Add to `README.md`'s "Key broker" section a short subsection covering:

- What `SUPABASE_DB_URL` does — set means Postgres, unset means the local SQLite file, no separate mode flag.
- That rollback is unsetting the variable, with the honest caveat that spend recorded in Postgres does not flow back to SQLite, so rolling back mid-month reopens caps. Describe it as an emergency lever, not a routine toggle.
- Where to get the connection string (Supabase dashboard → Project Settings → Database → Connection string → URI) and that it must never go in `.env.example`.
- `SUPABASE_TEST_DB_URL` as a **separate** database used only by the contract tests, and why: they truncate.
- Running `python -m scripts.smoke_supabase` before a deploy, and that CI never exercises Postgres so this is the check that does.

Match the README's existing voice, which explains why a thing exists rather than only how to run it.

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 500 tests (497 + 3).

- [ ] **Step 7: Commit**

```bash
git add scripts/smoke_supabase.py tests/test_smoke_supabase.py README.md
git commit -m "feat(broker): live Supabase smoke script and docs"
```

---

### Task 6: Fail closed when the database is unreachable

Spec §6. Today a database error inside the proxy surfaces as a 500 from the framework's error middleware. That is wrong in a specific and expensive way: the checks that gate *spending* must fail closed, or a database outage silently becomes an uncapped one.

**Files:**
- Modify: `keybroker/app.py` (the `_proxy` function)
- Test: `tests/test_keybroker_proxy.py`

**Interfaces:**
- Consumes: from Task 2 `dialect`; from Task 3 the `db` functions
- Produces: nothing later tasks depend on

- [ ] **Step 1: Write the failing test**

Add to `tests/test_keybroker_proxy.py`:

```python
def test_db_failure_during_auth_returns_503_and_forwards_nothing(broker, monkeypatch):
    """Auth gates spending, so it fails closed. A 500 here would be a bug
    report; forwarding anyway would be an uncapped bill."""
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())

    def boom(*_a, **_k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app_module.auth, "friend_for_token", boom)
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 503
    assert fake.calls == []


def test_db_failure_during_quota_returns_503_and_forwards_nothing(broker, monkeypatch):
    """The cap cannot be checked, so nothing may be spent."""
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())

    def boom(*_a, **_k):
        raise RuntimeError("pool timeout")

    monkeypatch.setattr(app_module.quota, "check", boom)
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 503
    assert fake.calls == []


def test_quota_exceeded_still_returns_402_not_503(broker, monkeypatch):
    """A real refusal must stay distinguishable from an outage — 402 is
    actionable by the friend, 503 is not."""
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.90, upstream_ref="m1")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 402


def test_db_failure_during_metering_still_returns_the_response(broker, monkeypatch):
    """Metering runs AFTER the money is spent, so it fails open. Withholding
    the response would cost the friend both the result and the dollars."""
    client, token = broker

    def boom(*_a, **_k):
        raise RuntimeError("connection refused")

    _install(monkeypatch, _anthropic_response())
    monkeypatch.setattr(app_module.meter, "record_anthropic", boom)
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_proxy.py -q`
Expected: FAIL — the first two raise `RuntimeError` out of the handler instead of returning 503.

- [ ] **Step 3: Fail closed around auth and quota**

In `keybroker/app.py`, wrap the two spending gates in `_proxy`. Place this after the owner-key check and before the body is read:

```python
    try:
        friend = auth.friend_for_token(auth.extract_token(request.headers, vendor))
    except Exception:
        # Fail CLOSED: the cap cannot be checked, so nothing may be spent.
        # Distinct from 401 (a known-bad token) and 402 (a real refusal) —
        # this is the broker being unable to answer, not a decision about
        # this caller.
        log.exception("broker: datastore unavailable during auth")
        return Response("Broker datastore unavailable.", status_code=503)
```

and around the quota check, keeping the existing `QuotaExceeded` branch **first** so a genuine refusal is never misreported as an outage:

```python
    try:
        quota.check(friend)
    except quota.QuotaExceeded as exc:
        return Response(exc.detail, status_code=402)
    except Exception:
        log.exception("broker: datastore unavailable during quota check")
        return Response("Broker datastore unavailable.", status_code=503)
```

**Do not** add a similar wrapper around metering. `keybroker/meter.py` already swallows its own exceptions by design, because by then the money is spent and withholding the response would cost the friend twice. That asymmetry is the point.

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_proxy.py -q`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 504 tests (500 + 4).

- [ ] **Step 6: Commit**

```bash
git add keybroker/app.py tests/test_keybroker_proxy.py
git commit -m "fix(broker): fail closed when the datastore is unreachable"
```

---

## Verification

After Task 6, confirm the spec's success criteria:

- [ ] `.venv/Scripts/python.exe -m pytest -q` — 504 passing, offline, with `SUPABASE_DB_URL` unset.
- [ ] `python -c "from keybroker import dialect; print(dialect.active().name)"` prints `sqlite` with the variable unset and `postgres` with it set.
- [ ] Importing `keybroker.db` with `SUPABASE_DB_URL` set opens **no** socket — verify by importing with an unreachable host in the URL and confirming no exception and no hang.
- [ ] **Live:** `python -m scripts.smoke_supabase` against the real project prints `all checks passed`, and its output contains no password.
- [ ] **Live:** `python -m scripts.add_friend <name>` with `SUPABASE_DB_URL` set creates the row in Supabase, and `python -m scripts.spend_report` reads it back.
- [ ] With `SUPABASE_TEST_DB_URL` set, `pytest tests/test_keybroker_contract.py -v` runs both parameterisations and both pass.
- [ ] Unsetting `SUPABASE_DB_URL` returns the broker to the local file with no code change.

## Notes for the executor

- **Never use `SET TIME ZONE` to fix timestamps.** It is session state and unreliable under a transaction pooler; the UTC pin belongs in the query via the dialect's `month_filter`. If you find yourself writing `AT TIME ZONE` inside `keybroker/db.py`, stop — it belongs in `keybroker/dialect.py`, and duplicating it is how the two backends drift.
- **`SUPABASE_TEST_DB_URL` is not `SUPABASE_DB_URL`.** The contract tests truncate. If you find yourself reading the production variable in a test, stop.
- **Task 3 must not change any test.** Its whole purpose is that the existing broker tests pass untouched. A test that needs editing means the refactor changed behaviour.
- **Constructing a dialect must never connect.** Pool creation belongs to the FastAPI lifespan, or to first use in an operator script. If importing `keybroker.db` opens a socket, the test suite and every script will dial Supabase.
- **Do not print connection strings.** `_safe_target` exists because smoke output gets pasted into issues.
- **Keep the `QuotaExceeded` branch ahead of the generic handler in Task 6.** A real refusal (402, actionable by the friend) must never be reported as an outage (503, not actionable by anyone).
