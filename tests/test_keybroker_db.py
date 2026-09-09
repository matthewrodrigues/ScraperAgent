"""Tests for keybroker.db — friends and spend persistence."""

import pytest

import config
from keybroker import db


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()
    return tmp_path / "broker.db"


def test_create_and_lookup_friend_by_token_hash(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    found = db.get_friend_by_token_hash("hash-alice")
    assert found["id"] == friend_id
    assert found["name"] == "alice"
    assert found["monthly_budget_usd"] == 5.0


def test_unknown_token_hash_returns_none(broker_db):
    db.create_friend("alice", "hash-alice", 5.0)
    assert db.get_friend_by_token_hash("hash-nobody") is None


def test_revoked_friend_is_not_returned_by_token_lookup(broker_db):
    db.create_friend("alice", "hash-alice", 5.0)
    assert db.revoke_friend("alice") is True
    assert db.get_friend_by_token_hash("hash-alice") is None


def test_revoke_preserves_the_row_and_its_spend(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.02, upstream_ref="msg_1")
    db.revoke_friend("alice")
    assert db.get_friend_by_name("alice")["revoked_at"] is not None
    assert db.friend_month_spend(friend_id) == pytest.approx(0.02)


def test_revoking_an_unknown_friend_returns_false(broker_db):
    assert db.revoke_friend("nobody") is False


def test_duplicate_name_is_rejected(broker_db):
    db.create_friend("alice", "hash-a", 5.0)
    with pytest.raises(db.DuplicateFriendError):
        db.create_friend("alice", "hash-b", 5.0)


def test_duplicate_token_hash_is_rejected(broker_db):
    db.create_friend("alice", "hash-shared", 5.0)
    with pytest.raises(db.DuplicateFriendError):
        db.create_friend("bob", "hash-shared", 5.0)


def test_spend_accumulates_per_friend(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.02, upstream_ref="msg_1")
    db.record_spend(friend_id, "anthropic", 0.03, upstream_ref="msg_2")
    assert db.friend_month_spend(friend_id) == pytest.approx(0.05)


def test_repeated_upstream_ref_is_recorded_once(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    assert db.record_spend(friend_id, "apify", 0.04, upstream_ref="run_1") is True
    assert db.record_spend(friend_id, "apify", 0.04, upstream_ref="run_1") is False
    assert db.friend_month_spend(friend_id) == pytest.approx(0.04)


def test_same_ref_across_vendors_is_not_deduplicated(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.01, upstream_ref="shared")
    db.record_spend(friend_id, "apify", 0.02, upstream_ref="shared")
    assert db.friend_month_spend(friend_id) == pytest.approx(0.03)


def test_spend_without_upstream_ref_is_never_deduplicated(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.01)
    db.record_spend(friend_id, "anthropic", 0.01)
    assert db.friend_month_spend(friend_id) == pytest.approx(0.02)


def test_global_spend_sums_across_friends(broker_db):
    a = db.create_friend("alice", "hash-a", 5.0)
    b = db.create_friend("bob", "hash-b", 5.0)
    db.record_spend(a, "anthropic", 0.02, upstream_ref="m1")
    db.record_spend(b, "anthropic", 0.03, upstream_ref="m2")
    assert db.global_month_spend() == pytest.approx(0.05)


def test_prior_month_spend_is_excluded(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO spend (friend_id, vendor, cost_usd, created_at) "
            "VALUES (?, 'anthropic', 1.50, '2026-08-15 12:00:00')",
            (friend_id,),
        )
    assert db.friend_month_spend(friend_id, month="2026-09") == pytest.approx(0.0)
    assert db.friend_month_spend(friend_id, month="2026-08") == pytest.approx(1.50)


def test_month_bounds_wraps_december():
    assert db.month_bounds("2026-12") == ("2026-12-01 00:00:00", "2027-01-01 00:00:00")


def test_list_friends_includes_revoked(broker_db):
    db.create_friend("alice", "hash-a", 5.0)
    db.create_friend("bob", "hash-b", 5.0)
    db.revoke_friend("bob")
    assert {f["name"] for f in db.list_friends()} == {"alice", "bob"}


def test_repeated_upstream_ref_updates_rather_than_being_ignored(broker_db):
    """The first Apify reading is often wrong, so a correction must land."""
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    assert db.record_spend(friend_id, "apify", 0.03, upstream_ref="run_1") is True
    assert db.record_spend(friend_id, "apify", 0.49, upstream_ref="run_1") is False
    assert db.friend_month_spend(friend_id) == pytest.approx(0.49)


def test_provisional_flag_defaults_off_and_round_trips(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.02, upstream_ref="m1")
    db.record_spend(friend_id, "apify", 0.50, upstream_ref="run_1", provisional=True)
    assert db.get_spend_by_ref("anthropic", "m1")["provisional"] == 0
    assert db.get_spend_by_ref("apify", "run_1")["provisional"] == 1
    assert db.friend_month_provisional(friend_id) == pytest.approx(0.50)


def test_settle_spend_clears_the_flag_and_rewrites_the_cost(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "apify", 0.50, upstream_ref="run_1", provisional=True)
    row = db.get_spend_by_ref("apify", "run_1")
    db.settle_spend(row["id"], 0.492)
    settled = db.get_spend_by_ref("apify", "run_1")
    assert settled["provisional"] == 0
    assert settled["cost_usd"] == pytest.approx(0.492)
    assert db.friend_month_provisional(friend_id) == pytest.approx(0.0)


def test_list_provisional_spend_honours_the_minimum_age(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "apify", 0.50, upstream_ref="fresh", provisional=True)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO spend (friend_id, vendor, cost_usd, upstream_ref, "
            "provisional, created_at) VALUES (?, 'apify', 0.50, 'old', 1, "
            "datetime('now', '-1 hour'))",
            (friend_id,),
        )
    refs = [r["upstream_ref"] for r in db.list_provisional_spend("apify", 120)]
    assert refs == ["old"]
    assert {r["upstream_ref"] for r in db.list_provisional_spend("apify", 0)} == {
        "fresh", "old"
    }


def test_column_migration_adds_provisional_to_an_existing_db(broker_db, monkeypatch):
    """CREATE TABLE IF NOT EXISTS won't add a column, so init_db must migrate."""
    with db.get_conn() as conn:
        conn.execute("DROP TABLE spend")
        conn.execute(
            "CREATE TABLE spend ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, friend_id INTEGER NOT NULL, "
            "vendor TEXT NOT NULL, model_or_actor TEXT, cost_usd REAL NOT NULL, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_creation_tokens INTEGER, upstream_ref TEXT, "
            "created_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
    db.init_db()
    with db.get_conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(spend)").fetchall()}
    assert "provisional" in cols
