"""Tests for keybroker.clamps — the three trusted-input rewrites."""

import json

import pytest

from keybroker import clamps


def test_body_within_cap_passes():
    clamps.ensure_size(b"x" * 100)


def test_oversized_body_is_rejected():
    with pytest.raises(clamps.BodyTooLarge):
        clamps.ensure_size(b"x" * (clamps.MAX_BODY_BYTES + 1))


def test_streaming_request_is_rejected():
    with pytest.raises(clamps.StreamingUnsupported):
        clamps.ensure_not_streaming(json.dumps({"stream": True}).encode())


def test_non_streaming_request_passes():
    clamps.ensure_not_streaming(json.dumps({"stream": False}).encode())
    clamps.ensure_not_streaming(json.dumps({}).encode())


def test_unparseable_body_is_not_treated_as_streaming():
    clamps.ensure_not_streaming(b"not json")


def test_max_tokens_above_ceiling_is_rewritten_down():
    body = json.dumps({"model": "m", "max_tokens": 64000}).encode()
    assert json.loads(clamps.clamp_anthropic(body))["max_tokens"] == clamps.MAX_TOKENS_CEILING


def test_max_tokens_below_ceiling_is_untouched():
    body = json.dumps({"model": "m", "max_tokens": 1024}).encode()
    assert clamps.clamp_anthropic(body) == body


def test_clamping_preserves_other_fields():
    body = json.dumps({"model": "m", "max_tokens": 99999, "tools": [{"name": "t"}]}).encode()
    out = json.loads(clamps.clamp_anthropic(body))
    assert out["tools"] == [{"name": "t"}]
    assert out["model"] == "m"


def test_unparseable_anthropic_body_passes_through_unchanged():
    assert clamps.clamp_anthropic(b"not json") == b"not json"


def test_apify_run_creation_is_detected():
    assert clamps.is_apify_run_creation("POST", "/v2/acts/abc~actor/runs") is True
    assert clamps.is_apify_run_creation("GET", "/v2/acts/abc~actor/runs") is False
    assert clamps.is_apify_run_creation("POST", "/v2/actor-runs/xyz") is False


def test_apify_charge_above_remaining_is_clamped_down():
    body = json.dumps({"maxTotalChargeUsd": 5.00}).encode()
    out = json.loads(clamps.clamp_apify_run(body, remaining_usd=0.40))
    assert out["maxTotalChargeUsd"] == pytest.approx(0.40)


def test_apify_charge_below_remaining_is_untouched():
    body = json.dumps({"maxTotalChargeUsd": 0.10}).encode()
    out = json.loads(clamps.clamp_apify_run(body, remaining_usd=5.00))
    assert out["maxTotalChargeUsd"] == pytest.approx(0.10)


def test_apify_body_without_a_charge_field_gets_one():
    out = json.loads(clamps.clamp_apify_run(json.dumps({}).encode(), remaining_usd=0.40))
    assert out["maxTotalChargeUsd"] == pytest.approx(0.40)
