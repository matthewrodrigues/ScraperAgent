"""Tests for keybroker.app — the proxy end to end, with upstreams stubbed."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from keybroker import app as app_module
from keybroker import auth, clamps, db


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


def _body(**extra):
    """A minimal valid Anthropic body. The model must be one the broker prices,
    or the request is refused before it is forwarded."""
    return {"model": config.NEGOTIATOR_MODEL, "max_tokens": 1024, **extra}


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
    assert client.post("/anthropic/v1/messages", json=_body()).status_code == 401


def test_unknown_token_is_401(broker):
    client, _ = broker
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": "sa_nope"})
    assert r.status_code == 401


def test_revoked_token_is_401(broker, monkeypatch):
    client, token = broker
    db.revoke_friend("alice")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 401


def test_valid_request_is_forwarded_with_the_owner_key(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json=_body(),
                    headers={"x-api-key": token, "anthropic-version": "2023-06-01"})
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-owner-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


def test_friend_token_never_reaches_upstream(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
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


@pytest.mark.parametrize("method", ["DELETE", "PUT", "PATCH"])
def test_apify_management_methods_are_refused(broker, monkeypatch, method):
    client, token = broker
    fake = _install(monkeypatch, httpx.Response(200, json={}))
    r = client.request(method, "/apify/v2/acts/some-actor",
                        headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 405
    assert fake.calls == []


def test_successful_call_is_metered(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response(input_tokens=1000, output_tokens=500))
    client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    friend = db.get_friend_by_name("alice")
    expected = config.price_usage(config.NEGOTIATOR_MODEL, 1000, 500)
    assert db.friend_month_spend(friend["id"]) == pytest.approx(expected)


def test_upstream_error_is_forwarded_and_not_metered(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, httpx.Response(400, json={"error": "bad"}))
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 400
    friend = db.get_friend_by_name("alice")
    assert db.friend_month_spend(friend["id"]) == pytest.approx(0.0)


def test_upstream_connection_failure_is_502(broker, monkeypatch):
    client, token = broker

    class _Broken:
        async def request(self, *a, **k):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(app_module, "_client", _Broken())
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 502


def test_exhausted_budget_returns_402_not_429(broker, monkeypatch):
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.90, upstream_ref="m1")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 402


def test_streaming_request_is_400(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json=_body(stream=True), headers={"x-api-key": token})
    assert r.status_code == 400


def test_oversized_body_is_413(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json=_body(pad="x" * 300_000), headers={"x-api-key": token})
    assert r.status_code == 413


def test_max_tokens_is_clamped_before_forwarding(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    client.post("/anthropic/v1/messages",
                json=_body(max_tokens=64000),
                headers={"x-api-key": token})
    sent = json.loads(fake.calls[0]["content"])
    assert sent["max_tokens"] == 4096


def test_apify_run_charge_is_clamped_on_the_query_string(broker, monkeypatch):
    """Apify reads the ceiling from the query string; the body is actor input."""
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.50, upstream_ref="m1")
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"id": "r1"}}))
    client.post("/apify/v2/acts/actor~x/runs?maxTotalChargeUsd=5.0",
                json={"queries": "laptop"},
                headers={"authorization": f"Bearer {token}"})
    call = fake.calls[0]
    assert float(call["params"]["maxTotalChargeUsd"]) == pytest.approx(0.50)
    # The actor's input record must survive untouched.
    assert json.loads(call["content"]) == {"queries": "laptop"}


def test_apify_run_without_a_charge_param_gets_the_remaining_budget(broker, monkeypatch):
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.50, upstream_ref="m1")
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"id": "r1"}}))
    client.post("/apify/v2/acts/actor~x/run-sync-get-dataset-items",
                json={"queries": "laptop"},
                headers={"authorization": f"Bearer {token}"})
    assert float(fake.calls[0]["params"]["maxTotalChargeUsd"]) == pytest.approx(0.50)


def test_non_run_apify_call_keeps_its_query_string(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"items": []}}))
    client.get("/apify/v2/datasets/ds1/items?limit=5",
               headers={"authorization": f"Bearer {token}"})
    params = fake.calls[0]["params"]
    assert params == {"limit": "5"}


def test_unpriced_model_is_400_and_forwards_nothing(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"model": "claude-made-up-9", "max_tokens": 100},
                    headers={"x-api-key": token})
    assert r.status_code == 400
    assert fake.calls == []


def test_priced_model_still_passes(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json=_body(),
                    headers={"x-api-key": token})
    assert r.status_code == 200
    assert len(fake.calls) == 1


def test_count_tokens_path_is_allowed(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, httpx.Response(200, json={"input_tokens": 12}))
    r = client.post("/anthropic/v1/messages/count_tokens", json=_body(),
                    headers={"x-api-key": token})
    assert r.status_code == 200
    assert fake.calls[0]["url"] == "https://api.anthropic.com/v1/messages/count_tokens"


def test_batches_path_is_refused_and_forwards_nothing(broker, monkeypatch):
    """Batch bodies nest max_tokens past the clamp and return no usage."""
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post(
        "/anthropic/v1/messages/batches",
        json={"requests": [{"params": _body(max_tokens=64000)}]},
        headers={"x-api-key": token},
    )
    assert r.status_code == 404
    assert fake.calls == []


def test_oversized_content_length_is_413_before_the_body_is_read(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post(
        "/anthropic/v1/messages",
        content=b"{}",
        headers={"x-api-key": token,
                 "content-type": "application/json",
                 "content-length": str(10 * clamps.MAX_BODY_BYTES)},
    )
    assert r.status_code == 413
    assert fake.calls == []


def test_unset_owner_key_fails_closed_with_503(broker, monkeypatch):
    client, token = broker
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    r = client.post("/anthropic/v1/messages", json=_body(), headers={"x-api-key": token})
    assert r.status_code == 503


def test_apify_run_creation_records_a_provisional_row_at_the_ceiling(broker, monkeypatch):
    """The friend is debited the worst case up front, not Apify's first reading."""
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.50, upstream_ref="m1")
    _install(monkeypatch, httpx.Response(200, json={
        "data": {"id": "r1", "actId": "actor~x", "status": "READY"}}))
    client.post("/apify/v2/acts/actor~x/runs",
                json={"queries": "laptop"},
                headers={"authorization": f"Bearer {token}"})
    row = db.get_spend_by_ref("apify", "r1")
    assert row["provisional"] == 1
    assert row["cost_usd"] == pytest.approx(0.50)


def test_apify_poll_does_not_lower_the_provisional_debit(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, httpx.Response(200, json={
        "data": {"id": "r1", "actId": "actor~x", "status": "READY"}}))
    client.post("/apify/v2/acts/actor~x/runs",
                json={"queries": "laptop"},
                headers={"authorization": f"Bearer {token}"})
    ceiling = db.get_spend_by_ref("apify", "r1")["cost_usd"]

    _install(monkeypatch, httpx.Response(200, json={
        "data": {"id": "r1", "actId": "actor~x", "status": "SUCCEEDED",
                 "usageTotalUsd": 0.032}}))
    client.get("/apify/v2/actor-runs/r1",
               headers={"authorization": f"Bearer {token}"})
    row = db.get_spend_by_ref("apify", "r1")
    assert row["cost_usd"] == pytest.approx(ceiling)
    assert row["provisional"] == 1
