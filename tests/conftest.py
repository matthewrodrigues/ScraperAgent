"""Shared pytest fixtures.

Every test gets a fresh SQLite file in a tmp dir so tests can't pollute
./scraperagent.db or each other.
"""

import pytest

import config
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
