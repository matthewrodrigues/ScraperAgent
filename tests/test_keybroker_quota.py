"""Tests for keybroker.quota — per-friend and global caps with headroom."""

import pytest

import config
from keybroker import db, quota


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    monkeypatch.setattr(config, "BROKER_GLOBAL_MONTHLY_BUDGET_USD", 25.0)
    db.init_db()


def _friend(name="alice", budget=5.0):
    db.create_friend(name, f"hash-{name}", budget)
    return db.get_friend_by_name(name)


def test_fresh_friend_passes(broker_db):
    quota.check(_friend())  # does not raise


def test_friend_well_under_budget_passes(broker_db):
    friend = _friend()
    db.record_spend(friend["id"], "anthropic", 1.00, upstream_ref="m1")
    quota.check(friend)


def test_friend_within_one_call_of_budget_is_rejected(broker_db):
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 4.85, upstream_ref="m1")
    with pytest.raises(quota.QuotaExceeded) as excinfo:
        quota.check(friend)
    assert "alice" in excinfo.value.detail


def test_just_inside_the_headroom_passes(broker_db):
    """Spec section 8 permits the last call to land at or under the budget, so
    anything with more than MAX_SINGLE_CALL_USD of room must be allowed.
    Margins avoid asserting on exact float equality at the boundary."""
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 5.0 - quota.MAX_SINGLE_CALL_USD - 0.01,
                    upstream_ref="m1")
    quota.check(friend)


def test_just_past_the_headroom_is_refused(broker_db):
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 5.0 - quota.MAX_SINGLE_CALL_USD + 0.01,
                    upstream_ref="m1")
    with pytest.raises(quota.QuotaExceeded):
        quota.check(friend)


def test_global_cap_rejects_a_friend_under_their_own_budget(broker_db, monkeypatch):
    monkeypatch.setattr(config, "BROKER_GLOBAL_MONTHLY_BUDGET_USD", 2.0)
    alice = _friend("alice", budget=5.0)
    bob = _friend("bob", budget=5.0)
    db.record_spend(bob["id"], "anthropic", 1.90, upstream_ref="m1")
    with pytest.raises(quota.QuotaExceeded) as excinfo:
        quota.check(alice)
    assert "global" in excinfo.value.detail.lower()


def test_remaining_reflects_spend(broker_db):
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 1.25, upstream_ref="m1")
    assert quota.remaining_usd(friend) == pytest.approx(3.75)


def test_remaining_never_goes_negative(broker_db):
    friend = _friend(budget=1.0)
    db.record_spend(friend["id"], "anthropic", 2.50, upstream_ref="m1")
    assert quota.remaining_usd(friend) == 0.0
