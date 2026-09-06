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


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    repo.init_db()
    return db_path
