"""keybroker/db.py must contain no SQLite-only SQL.

Two such constructs shipped once and passed the entire suite, because every
test runs on SQLite. A grep is cruder than a real Postgres run but it executes
on every commit, which the Postgres contract tests do not.
"""
import pathlib
import re

import pytest

SQLITE_ONLY = {
    r"INSERT\s+OR\s+IGNORE": "Postgres has no INSERT OR IGNORE; use ON CONFLICT",
    r"INSERT\s+OR\s+REPLACE": "Postgres has no INSERT OR REPLACE",
    r"\bdatetime\s*\(": "Postgres has no datetime(); compute the timestamp in Python",
    r"\bAUTOINCREMENT\b": "Postgres uses GENERATED ALWAYS AS IDENTITY",
    r"\bPRAGMA\b": "PRAGMA is SQLite-only; introspect via the dialect",
}


@pytest.mark.parametrize("pattern,why", list(SQLITE_ONLY.items()))
def test_db_module_has_no_sqlite_only_sql(pattern, why):
    src = pathlib.Path("keybroker/db.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    assert not re.search(pattern, code, re.I), why
