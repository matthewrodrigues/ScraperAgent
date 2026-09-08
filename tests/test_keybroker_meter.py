"""Tests for keybroker.meter — response bodies to spend rows."""

import json

import pytest

import config
from keybroker import db, meter


@pytest.fixture
def friend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()
    return db.create_friend("alice", "hash-alice", 5.0)


def _anthropic_body(input_tokens=1000, output_tokens=500):
    return json.dumps({
        "id": "msg_01",
        "model": config.NEGOTIATOR_MODEL,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }).encode()


def _apify_body(status="SUCCEEDED", usage=0.04, run_id="run_1"):
    return json.dumps({
        "data": {"id": run_id, "actId": "actor~x", "status": status,
                 "usageTotalUsd": usage}
    }).encode()


def test_anthropic_spend_is_priced_through_config(friend):
    meter.record_anthropic(friend, _anthropic_body())
    expected = config.price_usage(config.NEGOTIATOR_MODEL, 1000, 500)
    assert db.friend_month_spend(friend) == pytest.approx(expected)


def test_anthropic_token_counts_are_recorded(friend):
    meter.record_anthropic(friend, _anthropic_body())
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM spend").fetchone()
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["cache_read_tokens"] == 0
    assert row["upstream_ref"] == "msg_01"
    assert row["vendor"] == "anthropic"


def test_anthropic_response_without_usage_is_ignored(friend):
    meter.record_anthropic(friend, json.dumps({"id": "msg_02"}).encode())
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_unparseable_body_does_not_raise(friend):
    meter.record_anthropic(friend, b"not json")
    meter.record_apify(friend, b"not json")
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_apify_terminal_run_is_recorded(friend):
    meter.record_apify(friend, _apify_body())
    assert db.friend_month_spend(friend) == pytest.approx(0.04)


def test_apify_running_status_is_not_recorded(friend):
    meter.record_apify(friend, _apify_body(status="RUNNING"))
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_repeated_apify_polls_record_once(friend):
    for _ in range(4):
        meter.record_apify(friend, _apify_body())
    assert db.friend_month_spend(friend) == pytest.approx(0.04)


def test_failed_runs_still_cost_money_and_are_recorded(friend):
    meter.record_apify(friend, _apify_body(status="FAILED", usage=0.01))
    assert db.friend_month_spend(friend) == pytest.approx(0.01)


def _creation_body(run_id="run_1", status="READY"):
    """What Apify returns from POST .../runs — a run object with no usage yet."""
    return json.dumps({
        "data": {"id": run_id, "actId": "actor~x", "status": status}
    }).encode()


def test_run_creation_records_a_provisional_row_at_the_ceiling(friend):
    assert meter.record_apify_provisional(friend, _creation_body(), 0.50) is True
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM spend").fetchone()
    assert row["cost_usd"] == pytest.approx(0.50)
    assert row["provisional"] == 1
    assert row["upstream_ref"] == "run_1"
    assert row["vendor"] == "apify"
    assert row["model_or_actor"] == "actor~x"


def test_run_creation_without_a_run_id_records_nothing(friend):
    body = json.dumps({"data": {"status": "READY"}}).encode()
    assert meter.record_apify_provisional(friend, body, 0.50) is False
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_poll_does_not_overwrite_a_provisional_row(friend):
    """The terminal reading is exactly the one that settles late and low."""
    meter.record_apify_provisional(friend, _creation_body(), 0.50)
    meter.record_apify(friend, _apify_body(usage=0.032, run_id="run_1"))
    assert db.friend_month_spend(friend) == pytest.approx(0.50)
    assert db.get_spend_by_ref("apify", "run_1")["provisional"] == 1


def test_terminal_run_with_no_provisional_row_is_still_recorded(friend):
    """Defensive path: a run the broker never saw created."""
    meter.record_apify(friend, _apify_body(usage=0.04, run_id="orphan"))
    assert db.friend_month_spend(friend) == pytest.approx(0.04)
    assert db.get_spend_by_ref("apify", "orphan")["provisional"] == 0
