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
