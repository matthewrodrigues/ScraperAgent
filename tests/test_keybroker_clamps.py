"""Tests for keybroker.clamps — the trusted-input rewrites and refusals."""

import json

import pytest

import config
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


def test_apify_run_sync_variants_are_detected():
    """These start runs too, and were previously missed by the clamp."""
    assert clamps.is_apify_run_creation("POST", "/v2/acts/abc~actor/run-sync") is True
    assert clamps.is_apify_run_creation(
        "POST", "/v2/acts/abc~actor/run-sync-get-dataset-items") is True
    assert clamps.is_apify_run_creation("POST", "/v2/actor-tasks/t1/runs") is True


def test_apify_charge_above_remaining_is_clamped_down():
    out = clamps.clamp_apify_charge({"maxTotalChargeUsd": "5.00"}, remaining_usd=0.40)
    assert float(out["maxTotalChargeUsd"]) == pytest.approx(0.40)


def test_apify_charge_below_remaining_is_untouched():
    out = clamps.clamp_apify_charge({"maxTotalChargeUsd": "0.10"}, remaining_usd=5.00)
    assert float(out["maxTotalChargeUsd"]) == pytest.approx(0.10)


def test_apify_params_without_a_charge_get_one():
    out = clamps.clamp_apify_charge({}, remaining_usd=0.40)
    assert float(out["maxTotalChargeUsd"]) == pytest.approx(0.40)


def test_apify_charge_clamp_preserves_other_params():
    out = clamps.clamp_apify_charge({"memory": "2048", "build": "latest"},
                                    remaining_usd=1.0)
    assert out["memory"] == "2048"
    assert out["build"] == "latest"


def test_apify_charge_in_any_casing_cannot_survive_the_override():
    """A differently-cased duplicate must not reach Apify alongside the clamp."""
    out = clamps.clamp_apify_charge(
        {"maxtotalchargeusd": "99", "MaxTotalChargeUsd": "99"}, remaining_usd=0.25)
    assert [k for k in out] == ["maxTotalChargeUsd"]
    assert float(out["maxTotalChargeUsd"]) == pytest.approx(0.25)


def test_non_numeric_apify_charge_is_replaced_rather_than_crashing():
    out = clamps.clamp_apify_charge({"maxTotalChargeUsd": "lots"}, remaining_usd=0.40)
    assert float(out["maxTotalChargeUsd"]) == pytest.approx(0.40)


def test_non_numeric_max_tokens_is_clamped_rather_than_crashing():
    body = json.dumps({"model": "m", "max_tokens": "many"}).encode()
    assert json.loads(clamps.clamp_anthropic(body))["max_tokens"] == clamps.MAX_TOKENS_CEILING


def test_priced_model_passes():
    clamps.ensure_priced_model(json.dumps({"model": config.NEGOTIATOR_MODEL}).encode())


def test_unpriced_model_is_rejected():
    with pytest.raises(clamps.UnpricedModel):
        clamps.ensure_priced_model(json.dumps({"model": "claude-made-up-9"}).encode())


def test_rejection_names_the_menu_and_the_escape_hatch():
    """The 400 is read by a friend, so it must be actionable, not diagnostic."""
    with pytest.raises(clamps.UnpricedModel) as exc:
        clamps.ensure_priced_model(json.dumps({"model": "claude-made-up-9"}).encode())
    message = str(exc.value)
    for served in config.MODEL_PRICING:
        assert served in message
    assert "claude-made-up-9" in message
    assert "ANTHROPIC_API_KEY" in message
    assert "SCRAPERAGENT_BROKER_URL" in message


def test_missing_or_unparseable_model_is_rejected():
    """Fail closed: no model means no price, and no price means no cap."""
    with pytest.raises(clamps.UnpricedModel):
        clamps.ensure_priced_model(json.dumps({}).encode())
    with pytest.raises(clamps.UnpricedModel):
        clamps.ensure_priced_model(b"not json")
