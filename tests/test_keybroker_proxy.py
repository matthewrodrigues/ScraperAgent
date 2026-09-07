"""Tests for keybroker.app — the proxy end to end, with upstreams stubbed."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from keybroker import app as app_module
from keybroker import auth, db, quota


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-owner-key")
    monkeypatch.setattr(config, "APIFY_TOKEN", "apify-owner-token")
    monkeypatch.setattr(config, "BROKER_GLOBAL_MONTHLY_BUDGET_USD", 25.0)
    with TestClient(app_module.app) as client:
        token = auth.generate_token()
        db.create_friend("alice", auth.hash_token(token), 5.0)
        yield client, token


class _FakeUpstream:
    """Stands in for the module's httpx.AsyncClient and records the call."""

    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def _install(monkeypatch, response):
    fake = _FakeUpstream(response)
    monkeypatch.setattr(app_module, "_client", fake)
    return fake


def _anthropic_response(input_tokens=100, output_tokens=50):
    return httpx.Response(200, json={
        "id": "msg_01", "model": config.NEGOTIATOR_MODEL,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def test_health_needs_no_token(broker):
    client, _ = broker
    assert client.get("/health").status_code == 200


def test_missing_token_is_401(broker):
    client, _ = broker
    assert client.post("/anthropic/v1/messages", json={}).status_code == 401


def test_unknown_token_is_401(broker):
    client, _ = broker
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": "sa_nope"})
    assert r.status_code == 401


def test_revoked_token_is_401(broker, monkeypatch):
    client, token = broker
    db.revoke_friend("alice")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 401


def test_valid_request_is_forwarded_with_the_owner_key(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"model": "m", "max_tokens": 1024},
                    headers={"x-api-key": token, "anthropic-version": "2023-06-01"})
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-owner-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


def test_friend_token_never_reaches_upstream(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert token not in json.dumps(dict(fake.calls[0]["headers"]))


def test_apify_uses_bearer_and_its_own_upstream(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"items": []}}))
    r = client.get("/apify/v2/datasets/ds1/items",
                   headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["url"] == "https://api.apify.com/v2/datasets/ds1/items"
    assert call["headers"]["authorization"] == "Bearer apify-owner-token"


def test_successful_call_is_metered(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response(input_tokens=1000, output_tokens=500))
    client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    friend = db.get_friend_by_name("alice")
    expected = config.price_usage(config.NEGOTIATOR_MODEL, 1000, 500)
    assert db.friend_month_spend(friend["id"]) == pytest.approx(expected)


def test_upstream_error_is_forwarded_and_not_metered(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, httpx.Response(400, json={"error": "bad"}))
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 400
    friend = db.get_friend_by_name("alice")
    assert db.friend_month_spend(friend["id"]) == pytest.approx(0.0)


def test_upstream_connection_failure_is_502(broker, monkeypatch):
    client, token = broker

    class _Broken:
        async def request(self, *a, **k):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(app_module, "_client", _Broken())
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 502


def test_exhausted_budget_returns_402_not_429(broker, monkeypatch):
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.90, upstream_ref="m1")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 402


def test_streaming_request_is_400(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"stream": True}, headers={"x-api-key": token})
    assert r.status_code == 400


def test_oversized_body_is_413(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"pad": "x" * 300_000}, headers={"x-api-key": token})
    assert r.status_code == 413


def test_max_tokens_is_clamped_before_forwarding(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    client.post("/anthropic/v1/messages",
                json={"model": "m", "max_tokens": 64000},
                headers={"x-api-key": token})
    sent = json.loads(fake.calls[0]["content"])
    assert sent["max_tokens"] == 4096


def test_apify_run_charge_is_clamped_to_remaining_budget(broker, monkeypatch):
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.60, upstream_ref="m1")
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"id": "r1"}}))
    client.post("/apify/v2/acts/actor~x/runs", json={"maxTotalChargeUsd": 5.0},
                headers={"authorization": f"Bearer {token}"})
    sent = json.loads(fake.calls[0]["content"])
    assert sent["maxTotalChargeUsd"] == pytest.approx(0.40)


def test_unset_owner_key_fails_closed_with_503(broker, monkeypatch):
    client, token = broker
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 503
