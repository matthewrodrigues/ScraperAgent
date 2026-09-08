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
    # Cache token counts are stored but NOT priced: config.price_usage() takes
    # only input/output. Both are zero today because nothing enables prompt
    # caching; if that changes, cache-creation tokens (billed at 1.25x input)
    # will under-meter until price_usage() learns about them.
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


def record_apify_provisional(friend_id: int, body: bytes, ceiling_usd: float) -> bool:
    """Debit the worst case the moment a run is created.

    Apify's `usageTotalUsd` settles only *after* the run first reports a
    terminal status: two real runs read $0.032 and $0.000 at that moment and
    $0.492 each when re-fetched later. Metering on that first reading
    under-meters by an order of magnitude, and an under-meter is the one failure
    a spend cap cannot tolerate — it leaves headroom that authorises the next
    over-budget run.

    So the broker meters pessimistically: the run is debited at the ceiling it
    was clamped to (the `maxTotalChargeUsd` Apify was told to enforce), which is
    the most it can possibly cost. `scripts.reconcile_spend` refunds the
    difference once the real figure has settled.

    Returns True if a provisional row was written, False if the response carried
    no run id for the caller to fall back on.
    """
    payload = _load(body)
    data = payload.get("data") if isinstance(payload, dict) else None
    run_id = data.get("id") if isinstance(data, dict) else None
    if not run_id:
        return False

    try:
        db.record_spend(
            friend_id=friend_id,
            vendor="apify",
            model_or_actor=data.get("actId"),
            cost_usd=float(ceiling_usd),
            upstream_ref=run_id,
            provisional=True,
        )
    except Exception:
        log.exception("failed to record provisional apify spend for friend %s", friend_id)
    return True


def record_apify(friend_id: int, body: bytes) -> None:
    """Record a terminal run that has no provisional row of its own.

    Defensive only. Every run the broker starts is debited up front by
    record_apify_provisional, and this deliberately will not touch such a row:
    the terminal reading it would write is precisely the unreliable one (see
    record_apify_provisional), and lowering a provisional debit on the strength
    of it is how the cap sprang its leak. Only scripts.reconcile_spend settles a
    provisional row.

    What is left for this to catch is a terminal run the broker never saw
    created — a run id that reached a poll without passing through run creation.
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
        run_id = data.get("id")
        if run_id:
            existing = db.get_spend_by_ref("apify", run_id)
            if existing is not None and existing["provisional"]:
                return
        db.record_spend(
            friend_id=friend_id,
            vendor="apify",
            model_or_actor=data.get("actId"),
            cost_usd=float(usage),
            upstream_ref=run_id,
        )
    except Exception:
        log.exception("failed to record apify spend for friend %s", friend_id)
