"""Shared pytest fixtures.

Every test gets a fresh SQLite file in a tmp dir so tests can't pollute
./scraperagent.db or each other.
"""

import pytest

import config
from db import repo


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    repo.init_db()
    return db_path
