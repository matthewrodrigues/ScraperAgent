"""Shared pytest fixtures.

Every test gets a fresh SQLite file in a tmp dir so tests can't pollute
./scraperagent.db or each other.
"""

import pytest

import config
from keybroker import dialect
from db import repo


# The dashboard is closed by default and fails closed when DASHBOARD_PASSWORD is
# unset, so without this every route test would get a 503 instead of exercising
# the route. Patching config (rather than adding a test-only bypass flag) keeps
# the production auth path — middleware, session cookie, login form — under test
# on every request the suite makes. A bypass flag would be the hole this whole
# module exists to close.
TEST_DASHBOARD_PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(autouse=True)
def dashboard_password(monkeypatch):
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", TEST_DASHBOARD_PASSWORD)


# Autouse so isolation cannot be opted out of by omission: a test that forgets
# to request tmp_db would otherwise fall through to config.DB_PATH as it stood
# at import time — the developer's real scraperagent.db. That both corrupts a
# developer's working data (negotiation history, seller replies, offer/cost
# records, none of it regenerable) and makes the test's outcome depend on
# whatever that database happens to contain, which has nothing to do with the
# code under test. pytest caches a fixture per test, so a test that also
# requests tmp_db explicitly (by name or via a fixture that depends on it)
# gets the same object back, not a second database — this is harmless to the
# existing tests that already ask for it. A test that needs a different
# database (e.g. to exercise a pre-migration schema) can still monkeypatch
# config.DB_PATH itself afterwards; its assignment simply wins because it runs
# after this fixture.
@pytest.fixture(autouse=True)
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    repo.init_db()
    return db_path

# The broker's storage layer routes through keybroker.dialect, which chooses
# Postgres whenever config.SUPABASE_DB_URL is set. A developer with a real
# Supabase URL in .env would therefore have the ENTIRE test suite -- not just
# the opt-in Postgres contract tests -- connect to the live project and write
# to it. That happened once during development: a bare `pytest` created the
# schema and inserted a friend row in production.
#
# So the suite pins itself to SQLite. The Postgres contract tests opt back in
# deliberately, and only via SUPABASE_TEST_DB_URL, which must name a different
# database.
#
# Blanking the setting is NOT sufficient on its own: dialect.active() consults
# config only when nothing is cached, and dialect._active is a module global
# that outlives any single test. A contract-test fixture that dies before its
# teardown (an init_db() failure, say) leaves a live PostgresDialect -- pool and
# all -- in that global, and every later test then runs against Postgres while
# this guard sits there blanking a setting nobody reads. So reset the cache too,
# on both sides of the test.
@pytest.fixture(autouse=True)
def _never_touch_production_postgres(monkeypatch):
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "")
    dialect.reset_for_tests()
    yield
    dialect.reset_for_tests()
