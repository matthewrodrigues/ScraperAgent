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
    with pytest.raises(Exception):
        db.create_friend("alice", "hash-b", 5.0)


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
