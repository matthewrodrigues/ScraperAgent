"""Tests for scripts.reconcile_spend — settling provisional Apify rows."""

import pytest

import config
from keybroker import db
from scripts import reconcile_spend


@pytest.fixture
def friend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    monkeypatch.setattr(config, "APIFY_TOKEN", "apify-owner-token")
    db.init_db()
    return db.create_friend("alice", "hash-alice", 5.0)


def _add_provisional(friend_id, run_id="run_1", ceiling=0.50, age="-1 hour"):
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO spend (friend_id, vendor, model_or_actor, cost_usd, "
            "upstream_ref, provisional, created_at) "
            "VALUES (?, 'apify', 'actor~x', ?, ?, 1, datetime('now', ?))",
            (friend_id, ceiling, run_id, age),
        )


def _fetcher(runs):
    def fetch(run_id, token):
        assert token == "apify-owner-token"
        return runs.get(run_id)
    return fetch


def test_settles_a_provisional_row_to_the_true_value(friend, monkeypatch, capsys):
    _add_provisional(friend)
    monkeypatch.setattr(reconcile_spend, "fetch_run", _fetcher({
        "run_1": {"id": "run_1", "status": "SUCCEEDED", "usageTotalUsd": 0.492},
    }))
    assert reconcile_spend.main([]) == 0

    row = db.get_spend_by_ref("apify", "run_1")
    assert row["cost_usd"] == pytest.approx(0.492)
    assert row["provisional"] == 0
    out = capsys.readouterr().out
    assert "settled 1 provisional row(s)" in out
    assert "-0.0080" in out  # net change: 0.492 - 0.50


def test_a_row_younger_than_the_min_age_is_left_alone(friend, monkeypatch, capsys):
    _add_provisional(friend, age="-5 seconds")
    monkeypatch.setattr(reconcile_spend, "fetch_run", _fetcher({
        "run_1": {"id": "run_1", "status": "SUCCEEDED", "usageTotalUsd": 0.492},
    }))
    assert reconcile_spend.main(["--min-age-seconds", "120"]) == 0

    row = db.get_spend_by_ref("apify", "run_1")
    assert row["cost_usd"] == pytest.approx(0.50)
    assert row["provisional"] == 1
    assert "settled 0 provisional row(s)" in capsys.readouterr().out


def test_dry_run_writes_nothing(friend, monkeypatch, capsys):
    _add_provisional(friend)
    monkeypatch.setattr(reconcile_spend, "fetch_run", _fetcher({
        "run_1": {"id": "run_1", "status": "SUCCEEDED", "usageTotalUsd": 0.492},
    }))
    assert reconcile_spend.main(["--dry-run"]) == 0

    row = db.get_spend_by_ref("apify", "run_1")
    assert row["cost_usd"] == pytest.approx(0.50)
    assert row["provisional"] == 1
    out = capsys.readouterr().out
    assert "would settle" in out and "nothing was written" in out


def test_a_still_running_run_stays_provisional(friend, monkeypatch):
    _add_provisional(friend)
    monkeypatch.setattr(reconcile_spend, "fetch_run", _fetcher({
        "run_1": {"id": "run_1", "status": "RUNNING"},
    }))
    reconcile_spend.main([])
    assert db.get_spend_by_ref("apify", "run_1")["provisional"] == 1


def test_an_unfetchable_run_stays_provisional(friend, monkeypatch):
    _add_provisional(friend)
    monkeypatch.setattr(reconcile_spend, "fetch_run", _fetcher({}))
    reconcile_spend.main([])
    assert db.get_spend_by_ref("apify", "run_1")["provisional"] == 1


def test_settled_rows_are_not_reconciled_twice(friend, monkeypatch, capsys):
    _add_provisional(friend)
    fetch = _fetcher({
        "run_1": {"id": "run_1", "status": "SUCCEEDED", "usageTotalUsd": 0.492},
    })
    monkeypatch.setattr(reconcile_spend, "fetch_run", fetch)
    reconcile_spend.main([])
    capsys.readouterr()
    reconcile_spend.main([])
    assert "settled 0 provisional row(s)" in capsys.readouterr().out


def test_missing_owner_token_exits_nonzero(friend, monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    assert reconcile_spend.main([]) == 1
