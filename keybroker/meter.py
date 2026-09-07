"""Response-driven spend metering.

The broker is the authoritative ledger — it sees SDK retries the client never
attributes to a search. Metering never raises: the money is already spent
upstream, so a bookkeeping failure must not also cost the friend their result.
"""

import json
import logging
from typing import Any

import config
from keybroker import db


log = logging.getLogger(__name__)

# A run that reached any of these has finished charging.
TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def _load(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def record_anthropic(friend_id: int, body: bytes) -> None:
    payload = _load(body)
    if payload is None:
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return  # error responses carry no usage

    model = payload.get("model", "")
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    try:
        db.record_spend(
            friend_id=friend_id,
            vendor="anthropic",
            model_or_actor=model,
            cost_usd=config.price_usage(model, input_tokens, output_tokens),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
            upstream_ref=payload.get("id"),
        )
    except Exception:
        log.exception("failed to record anthropic spend for friend %s", friend_id)


def record_apify(friend_id: int, body: bytes) -> None:
    """Record when a response carries a terminal run with a usage figure.

    Matching on shape rather than route means this covers both the run-creation
    response and every poll, without the broker having to parse URLs.
    """
    payload = _load(body)
    if payload is None:
        return
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    if data.get("status") not in TERMINAL_RUN_STATUSES:
        return
    usage = data.get("usageTotalUsd")
    if usage is None:
        return

    try:
        db.record_spend(
            friend_id=friend_id,
            vendor="apify",
            model_or_actor=data.get("actId"),
            cost_usd=float(usage),
            upstream_ref=data.get("id"),
        )
    except Exception:
        log.exception("failed to record apify spend for friend %s", friend_id)
