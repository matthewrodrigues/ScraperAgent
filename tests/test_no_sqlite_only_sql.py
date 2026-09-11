"""keybroker/db.py must contain no SQLite-only SQL.

Two such constructs shipped once and passed the entire suite, because every
test runs on SQLite. A grep is cruder than a real Postgres run but it executes
on every commit, which the Postgres contract tests do not.
"""
import pathlib
import re

import pytest

SQLITE_ONLY_SQL = {
    r"INSERT\s+OR\s+IGNORE": "Postgres has no INSERT OR IGNORE; use ON CONFLICT",
    r"INSERT\s+OR\s+REPLACE": "Postgres has no INSERT OR REPLACE",
    r"\bdatetime\s*\(": "Postgres has no datetime(); compute the timestamp in Python",
    r"\bAUTOINCREMENT\b": "Postgres uses GENERATED ALWAYS AS IDENTITY",
    r"\bPRAGMA\b": "PRAGMA is SQLite-only; introspect via the dialect",
}

# Driver-specific exceptions and types that only the dialect should import.
# sqlite3.IntegrityError and sqlite3.Row are SQLite-only; callers should
# catch domain exceptions or use the dialect layer instead.
SQLITE_ONLY_DRIVER = {
    r"sqlite3\.IntegrityError": "Catch db.DuplicateFriendError instead, or let the dialect translate it",
    r"sqlite3\.Row": "Use the dialect layer; both backends return dict-like rows",
}


@pytest.mark.parametrize("pattern,why", list(SQLITE_ONLY_SQL.items()))
def test_db_module_has_no_sqlite_only_sql(pattern, why):
    src = pathlib.Path("keybroker/db.py").read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.strip().startswith("#")
    )
    assert not re.search(pattern, code, re.I), why


@pytest.mark.parametrize("pattern,why", list(SQLITE_ONLY_DRIVER.items()))
def test_sqlite_only_driver_constructs_forbidden(pattern, why):
    """Forbid SQLite-only driver exceptions in scripts, not in the dialect layer.

    keybroker/db.py is allowed to catch and translate backend-specific errors to
    domain exceptions; scripts must use the domain exceptions only.
    """
    modules = list(pathlib.Path("scripts").glob("*.py"))
    for module_path in modules:
        src = module_path.read_text(encoding="utf-8")
        code = "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("#")
        )
        assert not re.search(pattern, code), f"{module_path}: {why}"
