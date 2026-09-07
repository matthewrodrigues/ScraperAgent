"""Tests for the broker operator scripts."""

import pytest

import config
from keybroker import auth, db
from scripts import add_friend, revoke_friend, spend_report


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()


def test_add_friend_creates_a_friend_and_prints_the_token(broker_db, capsys):
    assert add_friend.main(["alice", "--budget", "7.5"]) == 0
    printed = capsys.readouterr().out
    friend = db.get_friend_by_name("alice")
    assert friend["monthly_budget_usd"] == 7.5

    token = next(w for w in printed.split() if w.startswith(auth.TOKEN_PREFIX))
    assert auth.friend_for_token(token)["id"] == friend["id"]


def test_the_raw_token_is_not_stored(broker_db, capsys):
    add_friend.main(["alice"])
    printed = capsys.readouterr().out
    token = next(w for w in printed.split() if w.startswith(auth.TOKEN_PREFIX))
    assert db.get_friend_by_name("alice")["token_sha256"] != token


def test_adding_a_duplicate_name_fails_without_a_traceback(broker_db, capsys):
    add_friend.main(["alice"])
    assert add_friend.main(["alice"]) == 1


def test_revoke_friend_revokes(broker_db, capsys):
    add_friend.main(["alice"])
    assert revoke_friend.main(["alice"]) == 0
    assert db.get_friend_by_name("alice")["revoked_at"] is not None


def test_revoking_an_unknown_friend_exits_nonzero(broker_db):
    assert revoke_friend.main(["nobody"]) == 1


def test_spend_report_lists_each_friend_with_totals(broker_db, capsys):
    add_friend.main(["alice", "--budget", "5"])
    add_friend.main(["bob", "--budget", "5"])
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 1.25, upstream_ref="m1")
    capsys.readouterr()

    assert spend_report.main([]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "bob" in out
    assert "1.25" in out


def test_spend_report_honours_an_explicit_month(broker_db, capsys):
    add_friend.main(["alice"])
    friend = db.get_friend_by_name("alice")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO spend (friend_id, vendor, cost_usd, created_at) "
            "VALUES (?, 'anthropic', 3.00, '2026-08-10 09:00:00')",
            (friend["id"],),
        )
    capsys.readouterr()
    spend_report.main(["--month", "2026-08"])
    assert "3.00" in capsys.readouterr().out
