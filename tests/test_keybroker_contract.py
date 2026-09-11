"""One set of behaviours, run against both backends.

SQLite always. Postgres only when SUPABASE_TEST_DB_URL is set — deliberately
NOT SUPABASE_DB_URL, because these tests create friends, write spend rows and
truncate between cases. Reusing the production setting would mean configuring
the broker for real use silently arms this suite against the live ledger.
"""

import os
from urllib.parse import urlparse

import pytest

import config
from keybroker import db, dialect


def db_identity(url: str) -> tuple:
    """A normalised identity for a Postgres URL.

    Supabase spells ONE database several ways: the session pooler on :5432, the
    transaction pooler on :6543, the direct db.<ref>.supabase.co host, any of
    them with ?sslmode=require appended. String equality therefore does not
    answer "is this the same database" -- and that is the only question the
    truncate guard below actually needs answered.

    The project ref is the identity when we can find it: it lives in the pooler
    username (postgres.<ref>) and in the direct host (db.<ref>.supabase.co).
    Port, host spelling and query string are noise. For anything that is not
    recognisably Supabase we fall back to (host, db, user), which is still
    strictly stronger than comparing raw strings.
    """
    p = urlparse(url)
    host = (p.hostname or "").lower()
    user = (p.username or "").lower()
    dbname = (p.path or "").lstrip("/").lower()
    if "." in user:
        return ("supabase", user.split(".", 1)[1], dbname)
    if host.startswith("db.") and host.endswith(".supabase.co"):
        return ("supabase", host[len("db."):-len(".supabase.co")], dbname)
    return ("raw", host, dbname, user)


def _postgres_url() -> str:
    url = os.getenv("SUPABASE_TEST_DB_URL", "")
    prod = os.getenv("SUPABASE_DB_URL", "")
    if url and prod and db_identity(url) == db_identity(prod):
        pytest.fail(
            "SUPABASE_TEST_DB_URL names the same database as SUPABASE_DB_URL — "
            "these tests truncate the tables they touch. Note this compares the "
            "database's identity, not the URL text: the two pooler ports and the "
            "direct host are three spellings of one project."
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

    # conftest.py has an autouse fixture that blanks config.SUPABASE_DB_URL so
    # the suite can never reach production Postgres. This fixture deliberately
    # re-points it at the TEST database above. Autouse fixtures run first, so
    # the order is right -- but assert it rather than trust it: if the guard
    # ever ran last, these tests would run on SQLite and PASS WHILE TESTING
    # NOTHING, which is worse than failing.
    expected = "postgres" if request.param == "postgres" else "sqlite"
    assert dialect.active().name == expected, (
        f"expected the {expected} backend, got {dialect.active().name} -- "
        "fixture ordering changed and this test is no longer testing what it claims"
    )

    # try/finally, not a bare post-yield statement: if init_db() or the
    # TRUNCATE raises, a yield-fixture never reaches its teardown, and the
    # PostgresDialect this fixture just cached would leak into every following
    # test in the session.
    try:
        db.init_db()
        if request.param == "postgres":
            with db.get_conn() as conn:
                conn.execute("TRUNCATE spend, friends RESTART IDENTITY CASCADE")
                conn.commit()
        yield request.param
    finally:
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


# --- the truncate guard itself -------------------------------------------
# These need no database: they test the identity function the guard relies on.

_POOLER_5432 = "postgresql://postgres.abcdefghijklm:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"
_POOLER_6543 = "postgresql://postgres.abcdefghijklm:pw@aws-0-us-east-1.pooler.supabase.com:6543/postgres"
_DIRECT = "postgresql://postgres:pw@db.abcdefghijklm.supabase.co:5432/postgres"
_OTHER_PROJECT = "postgresql://postgres.zzzzzzzzzzzzz:pw@aws-0-us-east-1.pooler.supabase.com:5432/postgres"


def test_the_two_pooler_ports_are_one_database():
    """The exact failure the old string-equality guard allowed: point
    SUPABASE_TEST_DB_URL at production's transaction pooler and the strings
    differ, so the guard passes and the fixture truncates the live ledger."""
    assert db_identity(_POOLER_5432) == db_identity(_POOLER_6543)


def test_pooler_and_direct_host_are_one_database():
    assert db_identity(_POOLER_5432) == db_identity(_DIRECT)


def test_a_trailing_sslmode_does_not_change_identity():
    assert db_identity(_POOLER_5432) == db_identity(_POOLER_5432 + "?sslmode=require")


def test_different_projects_are_different_databases():
    assert db_identity(_POOLER_5432) != db_identity(_OTHER_PROJECT)


def test_non_supabase_urls_still_compare_on_host_and_db():
    a = "postgresql://me:pw@localhost:5432/broker"
    assert db_identity(a) == db_identity("postgresql://me:pw@localhost:6543/broker")
    assert db_identity(a) != db_identity("postgresql://me:pw@localhost:5432/other")


# --- the UTC pin, against a real server --------------------------------------

def test_month_filter_is_utc_under_a_hostile_session_timezone(backend):
    """Spec 4.1, tested rather than grepped.

    The unit test can only assert the SQL text. This runs the real clause on a
    real server whose session timezone is deliberately NOT UTC -- which is the
    only condition under which the bug is visible. Supabase defaults to UTC, so
    an uncast clause passes everywhere else, including the smoke script.
    """
    if backend != "postgres":
        pytest.skip("session timezone is a Postgres concept")

    clause = dialect.active().month_filter("t")
    bounds = ("2026-09-01 00:00:00", "2026-10-01 00:00:00")

    def matches(instant: str) -> bool:
        sql = f"SELECT 1 FROM (SELECT timestamptz '{instant}' AS t) s WHERE {clause}"
        with db.get_conn() as conn:
            # Any pooler default, ALTER ROLE ... SET timezone, or PGTZ can do
            # this to us in production; make it explicit here.
            conn.execute("SET TIME ZONE 'America/New_York'")
            return conn.execute(sql, bounds).fetchone() is not None

    # Midnight UTC on the 1st is inside September, and 23:59:59Z on Aug 31 is
    # not. With the uncast clause the boundary moves by the session offset and
    # both of these flip.
    assert matches("2026-09-01 00:00:00+00") is True, (
        "the first instant of the month was excluded — the month boundary is "
        "resolving in the session timezone, not UTC"
    )
    assert matches("2026-08-31 23:59:59+00") is False, (
        "an August instant was counted in September — the month boundary is "
        "resolving in the session timezone, not UTC"
    )
    assert matches("2026-09-30 23:59:59+00") is True
    assert matches("2026-10-01 00:00:00+00") is False
