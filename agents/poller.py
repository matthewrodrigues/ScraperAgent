"""Background poller for seller replies.

PRD §"Next sessions" Session 3: replace the manual "Check for new replies"
button with an asyncio loop that polls every `config.SELLER_POLL_INTERVAL_SECONDS`
across all active negotiations. Per-negotiation cost is one
`GetMyMessages` Trading API call (eBay charges nothing per call; the daily
5000/keyset cap accommodates ~17 concurrent active negotiations at 5-min
cadence) plus, on the rare hit, one counter-draft (Claude Sonnet, paid).

Architecture:
- `process_seller_replies_for_listing()` is the unit of work — extracted from
  the original check-replies route handler so both the route (manual button)
  and the poller (automatic) share the dedup + persist + counter-fire path.
  Side-effect signature: returns `True` when a counter draft should fire
  (the caller decides *how* to fire — FastAPI BackgroundTasks vs synchronous
  in the poll thread).
- `run_one_poll_iteration()` walks all active negotiations and processes each.
  Per-negotiation errors are isolated so one failing listing doesn't kill the
  loop.
- `poll_loop()` is the asyncio entry point — sleeps `SELLER_POLL_INTERVAL_SECONDS`
  between iterations, runs the (sync) iteration in a thread pool so the event
  loop stays responsive. Started by the FastAPI lifespan; cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import logging

import config
from agents.graph import MAX_ROUNDS, build_counter_graph
from db import repo
from integrations import ebay_trading
from integrations.ebay_trading import EbayTradingError


log = logging.getLogger(__name__)


def process_seller_replies_for_listing(
    search_id: int,
    listing_id: int,
    negotiation_id: int,
) -> bool:
    """Fetch any new seller messages for one listing, persist deduped, and
    flip negotiation state if a counter draft should fire.

    Returns True when the caller should kick off the counter graph for this
    negotiation. The actual graph invocation is left to the caller so the
    route can use FastAPI BackgroundTasks while the poller calls it inline.

    Raises EbayTradingError on eBay-side failures so the caller can decide
    whether to surface (route → 502) or swallow (poller → log + continue).
    """
    listing = repo.get_listing(listing_id)
    if listing is None:
        log.warning("poller: listing %s vanished mid-poll", listing_id)
        return False
    negotiation = repo.get_negotiation(negotiation_id)
    if negotiation is None or negotiation["status"] in ("deal", "walked_away", "timed_out"):
        # Race: negotiation was closed between list_active_negotiations and now.
        return False

    incoming = ebay_trading.get_messages_for_item(listing["ebay_item_id"])

    # Dedup by exact body match — same contract as the original route helper.
    # eBay messages are user-typed; identical bodies in the same thread are
    # vanishingly rare. This trades a microscopic false-negative risk for a
    # zero-state-machine dedup story.
    existing_bodies = {m["body"] for m in repo.get_messages_by_negotiation(negotiation_id)}
    new_count = 0
    for m in incoming:
        body = (m.get("body") or "").strip()
        if body and body not in existing_bodies:
            repo.add_message(negotiation_id, role="seller", body=body, offer_amount=None, status="received")
            new_count += 1
            existing_bodies.add(body)  # avoid re-counting within the same fetch

    if new_count == 0:
        return False
    if negotiation["rounds"] >= MAX_ROUNDS:
        # Surface the seller message but DON'T fire a counter — round cap
        # forces a manual close (deal or walk-away).
        log.info(
            "poller: %d new seller msg(s) for negotiation %s — round cap hit, no counter",
            new_count, negotiation_id,
        )
        return False

    # Counter is expected. Reset to 'open' so the next round can draft.
    repo.update_negotiation_status(negotiation_id, "open")
    log.info(
        "poller: %d new seller msg(s) for negotiation %s — firing counter draft",
        new_count, negotiation_id,
    )
    return True


def _run_counter_graph(search_id: int, listing_id: int, negotiation_id: int) -> None:
    """Sync wrapper around build_counter_graph().invoke for the poller.
    Mirrors the equivalent helper in `api/routes/searches.py` so the same
    invocation contract is honored from both call sites."""
    build_counter_graph().invoke({
        "search_id": search_id,
        "listing_id": listing_id,
        "negotiation_id": negotiation_id,
    })


def run_one_poll_iteration() -> dict[str, int]:
    """Sync entry point — walk every active negotiation once.

    Returns a stats dict `{"checked", "errors", "counters_fired"}` for
    logging / future telemetry. Per-negotiation errors are caught here so
    one failing listing doesn't abort the iteration.
    """
    stats = {"checked": 0, "errors": 0, "counters_fired": 0}
    for active in repo.list_active_negotiations():
        stats["checked"] += 1
        try:
            should_counter = process_seller_replies_for_listing(
                active["search_id"], active["listing_id"], active["negotiation_id"],
            )
        except EbayTradingError as exc:
            log.warning(
                "poller: eBay error processing negotiation %s (item %s): %s",
                active["negotiation_id"], active.get("ebay_item_id"), exc,
            )
            stats["errors"] += 1
            continue
        except Exception as exc:  # noqa: BLE001 — isolate per-negotiation faults
            log.exception(
                "poller: unexpected error processing negotiation %s: %s",
                active["negotiation_id"], exc,
            )
            stats["errors"] += 1
            continue

        if should_counter:
            try:
                _run_counter_graph(
                    active["search_id"], active["listing_id"], active["negotiation_id"],
                )
                stats["counters_fired"] += 1
            except Exception as exc:  # noqa: BLE001 — graph errors must not kill the loop
                log.exception(
                    "poller: counter graph failed for negotiation %s: %s",
                    active["negotiation_id"], exc,
                )
                stats["errors"] += 1
    return stats


async def poll_loop() -> None:
    """Long-running asyncio task: poll → sleep → repeat. Started by FastAPI's
    lifespan and cancelled on shutdown.

    Defends against:
      * Per-iteration crashes: caught + logged so the loop survives.
      * Disabled-via-config: returns immediately, lifespan keeps the task ref
        but it self-terminates.
      * Shutdown: asyncio.CancelledError propagates cleanly.
    """
    if not config.SELLER_POLL_ENABLED:
        log.info("poller: disabled via SELLER_POLL_ENABLED=false; not starting loop")
        return
    interval = config.SELLER_POLL_INTERVAL_SECONDS
    log.info("poller: starting loop, interval=%ds", interval)
    try:
        while True:
            try:
                stats = await asyncio.to_thread(run_one_poll_iteration)
                if stats["checked"] or stats["errors"] or stats["counters_fired"]:
                    log.info("poller: iteration stats=%s", stats)
            except Exception as exc:  # noqa: BLE001 — extremely defensive: even a poll-loop bug shouldn't crash the app
                log.exception("poller: iteration crashed: %s", exc)
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        log.info("poller: cancelled (app shutdown)")
        raise
