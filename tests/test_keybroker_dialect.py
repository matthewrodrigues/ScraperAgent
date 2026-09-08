"""Tests for keybroker.dialect — SQL translation only, no database needed.

These cover the differences that SQLite tests structurally cannot: the DDL
Postgres emits, its introspection query, and the UTC-pinned month comparison.
"""

import pytest

import config
from keybroker import dialect


@pytest.fixture(autouse=True)
def _reset_dialect_cache():
    # dialect.active() caches its choice at module level (by design — a
    # process must not switch backends mid-flight). That makes the two
    # active()-selection tests below order-dependent on each other unless
    # something resets the cache between tests, so this test module resets it
    # itself rather than relying on test order or on tests/conftest.py, which
    # Task 2 does not own.
    dialect.reset_for_tests()
    yield
    dialect.reset_for_tests()


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
