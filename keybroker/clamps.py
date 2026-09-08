"""Request rewrites for untrusted clients.

The broker forwards bodies verbatim except here. Each clamp exists because the
client runs on someone else's machine, so its values are advisory at best.
Together, the max_tokens ceiling and the body cap are what make
quota.MAX_SINGLE_CALL_USD a valid reservation — change them and recompute it.
"""

import json
import logging
from typing import Any

import config


log = logging.getLogger(__name__)

MAX_TOKENS_CEILING = 4096
MAX_BODY_BYTES = 256 * 1024

# Apify reads its pre-spend ceiling from the *query string*, not the body: the
# SDK folds max_total_charge_usd into request_params (see
# apify_client/_resource_clients/actor.py), while the POST body is the actor's
# own input record. Writing this key into the body caps nothing and pollutes
# input schemas that disallow additional properties.
APIFY_CHARGE_PARAM = "maxTotalChargeUsd"


class BodyTooLarge(Exception):
    """Request body exceeds MAX_BODY_BYTES."""


class StreamingUnsupported(Exception):
    """Streaming would under-meter silently, so it is refused outright."""


class UnpricedModel(Exception):
    """The requested model has no entry in config.MODEL_PRICING.

    config.price_usage() returns 0.0 for unknown models, so forwarding one would
    spend the owner's money and meter it at zero, quietly disabling both caps.
    """


def _load(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def ensure_size(body: bytes) -> None:
    if len(body) > MAX_BODY_BYTES:
        raise BodyTooLarge(f"{len(body)} bytes exceeds the {MAX_BODY_BYTES} byte cap")


def ensure_not_streaming(body: bytes) -> None:
    payload = _load(body)
    if payload is not None and payload.get("stream") is True:
        raise StreamingUnsupported("streaming responses are not supported by the broker")


def ensure_priced_model(body: bytes) -> None:
    """The broker serves a fixed menu of models; anything else is refused.

    The menu is config.MODEL_PRICING's keys, deliberately not a separate list:
    the invariant is "never serve a model you cannot price". A 0.0 from
    config.price_usage() is not a valid meter reading — it would let a friend
    spend without ever moving spend.cost_usd, leaving both caps inert — so the
    two must not be able to drift apart.

    Unlike the other clamps this rejects rather than rewrites: silently swapping
    a friend's model would be worse than telling them no. The message names the
    escape hatch, since a friend wanting a different model can simply bill it to
    themselves.
    """
    payload = _load(body)
    model = payload.get("model") if payload is not None else None
    if not isinstance(model, str) or model not in config.MODEL_PRICING:
        served = ", ".join(sorted(config.MODEL_PRICING))
        raise UnpricedModel(
            f"This broker serves only: {served}. To use {model!r}, set your own "
            f"ANTHROPIC_API_KEY and unset SCRAPERAGENT_BROKER_URL to bill it "
            f"yourself."
        )


def clamp_anthropic(body: bytes) -> bytes:
    """Rewrite max_tokens down to the ceiling. Unparseable bodies pass through —
    upstream is a better judge of malformed JSON than we are."""
    payload = _load(body)
    if payload is None:
        return body
    requested = payload.get("max_tokens", 0)
    # A non-numeric max_tokens (a JSON string, say) would raise TypeError on the
    # comparison and surface as a 500. Anything we cannot compare is untrusted,
    # so it gets the ceiling.
    over = (
        not isinstance(requested, (int, float))
        or isinstance(requested, bool)
        or requested > MAX_TOKENS_CEILING
    )
    if over:
        log.info("clamping max_tokens %r -> %s", requested, MAX_TOKENS_CEILING)
        payload["max_tokens"] = MAX_TOKENS_CEILING
        return json.dumps(payload).encode("utf-8")
    return body


# Every Apify endpoint that starts a run and therefore starts charging. The
# SDK reaches three of these shapes: acts/{id}/runs, the run-sync variants, and
# actor-tasks/{id}/runs (which also ends in /runs).
_APIFY_RUN_SUFFIXES = ("/runs", "/run-sync", "/run-sync-get-dataset-items")


def is_apify_run_creation(method: str, path: str) -> bool:
    """True for the Apify calls that start a run, the only ones that spend."""
    return method.upper() == "POST" and path.rstrip("/").endswith(_APIFY_RUN_SUFFIXES)


def clamp_apify_charge(params: dict[str, str], remaining_usd: float) -> dict[str, str]:
    """Cap the run's spend ceiling at the friend's remaining budget.

    Apify is the only vendor exposing a pre-spend limit, so this is the one
    place the broker prevents spend rather than measuring it afterwards. The
    ceiling travels as a query parameter (see APIFY_CHARGE_PARAM), so this
    rewrites the query string and never touches the actor's input body.

    Returns a new dict; any casing of the parameter the client supplied is
    dropped first so a duplicate cannot survive the override.
    """
    clamped = {
        k: v for k, v in params.items()
        if k.lower() != APIFY_CHARGE_PARAM.lower()
    }
    requested = params.get(APIFY_CHARGE_PARAM)
    ceiling = round(remaining_usd, 4)
    try:
        # A missing or non-numeric value means we have nothing to trust, so the
        # friend's remaining budget stands in — never an unbounded run.
        if requested is not None and float(requested) <= remaining_usd:
            ceiling = float(requested)
    except (TypeError, ValueError):
        log.info("non-numeric %s %r; using remaining budget", APIFY_CHARGE_PARAM, requested)
    clamped[APIFY_CHARGE_PARAM] = str(ceiling)
    return clamped
