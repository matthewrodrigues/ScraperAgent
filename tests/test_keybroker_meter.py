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
