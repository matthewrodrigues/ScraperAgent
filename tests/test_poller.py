"""Tests for the background poller.

The poller's value is correctness of the shared helper + correct iteration
discipline (one failing negotiation doesn't break the loop). The async loop
itself is mostly a sleep+call wrapper; we test it with a zero-interval
fixture to keep the run fast.
"""

import asyncio
from unittest.mock import patch

import pytest

from agents import poller
from db import repo
from integrations.ebay_trading import EbayTradingError


# ---- Helpers ----------------------------------------------------------

def _seeded_negotiation(tmp_db, rounds: int = 0) -> tuple[int, int, int]:
    """Create a search + listing + active negotiation; return (search_id,
    listing_id, negotiation_id). Rounds defaults to 0; tests that need the
    cap can bump it."""
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=100.0,
    )
    ids = repo.add_listings(search_id, [{
        "ebay_item_id": "v1|abc|0", "title": "T", "price": 50.0,
        "url": "https://x", "seller_id": "seller_x",
    }])
    listing_id = ids[0]
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    if rounds:
        with repo.get_conn() as conn:
            conn.execute("UPDATE negotiations SET rounds = ? WHERE id = ?", (rounds, nid))
    return search_id, listing_id, nid


def _seller_msg(body: str, item_id: str = "abc") -> dict:
    return {"message_id": "m1", "sender": "seller_x", "body": body,
            "received_at": "2026-06-19T10:00:00Z", "item_id": item_id}


# ---- process_seller_replies_for_listing ------------------------------

@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_process_persists_new_seller_messages_and_returns_counter_flag(mock_get, tmp_db):
    search_id, listing_id, nid = _seeded_negotiation(tmp_db)
    mock_get.return_value = [_seller_msg("I can do $90 firm.")]

    should_counter = poller.process_seller_replies_for_listing(search_id, listing_id, nid)

    assert should_counter is True
    msgs = [m for m in repo.get_messages_by_negotiation(nid) if m["role"] == "seller"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "I can do $90 firm."
    # Status flipped back to 'open' so the next round can draft.
    assert repo.get_negotiation(nid)["status"] == "open"


@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_process_dedups_already_known_messages(mock_get, tmp_db):
    """Same body that's already on the thread must not be re-persisted."""
    search_id, listing_id, nid = _seeded_negotiation(tmp_db)
    repo.add_message(nid, "seller", "Already saw this one.", None, status="received")
    mock_get.return_value = [_seller_msg("Already saw this one.")]

    should_counter = poller.process_seller_replies_for_listing(search_id, listing_id, nid)

    assert should_counter is False
    seller_msgs = [m for m in repo.get_messages_by_negotiation(nid) if m["role"] == "seller"]
    assert len(seller_msgs) == 1  # still just the original


@patch("agents.poller.ebay_trading.get_messages_for_item", return_value=[])
def test_process_no_new_messages_returns_false(_mock_get, tmp_db):
    search_id, listing_id, nid = _seeded_negotiation(tmp_db)
    assert poller.process_seller_replies_for_listing(search_id, listing_id, nid) is False


@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_process_skips_counter_at_round_cap_but_still_persists(mock_get, tmp_db):
    """Round cap reached: persist the seller reply (user wants to see it) but
    DO NOT fire a counter — user must manually close out."""
    from agents.graph import MAX_ROUNDS
    search_id, listing_id, nid = _seeded_negotiation(tmp_db, rounds=MAX_ROUNDS)
    mock_get.return_value = [_seller_msg("Final $85, take it or leave it.")]

    should_counter = poller.process_seller_replies_for_listing(search_id, listing_id, nid)

    assert should_counter is False
    seller_msgs = [m for m in repo.get_messages_by_negotiation(nid) if m["role"] == "seller"]
    assert len(seller_msgs) == 1
    assert "Final $85" in seller_msgs[0]["body"]


@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_process_skips_terminal_negotiation(mock_get, tmp_db):
    """Race: a negotiation closed (deal / walk-away) between list_active and
    process — no eBay call should fire."""
    search_id, listing_id, nid = _seeded_negotiation(tmp_db)
    repo.mark_negotiation_deal(nid, 75.0)

    should_counter = poller.process_seller_replies_for_listing(search_id, listing_id, nid)

    assert should_counter is False
    mock_get.assert_not_called()


# ---- run_one_poll_iteration ------------------------------------------

@patch("agents.poller._run_counter_graph")
@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_iteration_fires_counter_for_each_negotiation_with_new_reply(mock_get, mock_counter, tmp_db):
    """End-to-end of one poll iteration: every active negotiation gets one
    GetMyMessages call; each new reply queues the counter graph."""
    s_a, l_a, n_a = _seeded_negotiation(tmp_db)
    s_b, l_b, n_b = _seeded_negotiation(tmp_db)
    mock_get.return_value = [_seller_msg("I can do $90.")]

    stats = poller.run_one_poll_iteration()

    assert stats["checked"] == 2
    assert stats["counters_fired"] == 2
    assert stats["errors"] == 0
    # Both negotiations got their counter graph fired with their own IDs.
    assert {c.args[2] for c in mock_counter.call_args_list} == {n_a, n_b}


@patch("agents.poller._run_counter_graph")
@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_iteration_isolates_per_negotiation_ebay_errors(mock_get, mock_counter, tmp_db):
    """One eBay failure must not stop the loop — other negotiations still get
    processed. The error count is tracked for telemetry."""
    s_a, l_a, n_a = _seeded_negotiation(tmp_db)
    s_b, l_b, n_b = _seeded_negotiation(tmp_db)
    # First call raises; second returns a real reply.
    mock_get.side_effect = [
        EbayTradingError("eBay 503 transient"),
        [_seller_msg("Counter at $88.")],
    ]

    stats = poller.run_one_poll_iteration()

    assert stats["checked"] == 2
    assert stats["errors"] == 1
    assert stats["counters_fired"] == 1
    # The healthy negotiation still got its counter fired.
    assert mock_counter.call_args.args[2] == n_b


@patch("agents.poller._run_counter_graph", side_effect=RuntimeError("graph blew up"))
@patch("agents.poller.ebay_trading.get_messages_for_item")
def test_iteration_isolates_per_negotiation_counter_graph_errors(mock_get, _mock_counter, tmp_db):
    """If the counter graph itself throws (Claude outage, bad state), the
    loop must keep going for the rest of the negotiations."""
    _seeded_negotiation(tmp_db)
    _seeded_negotiation(tmp_db)
    mock_get.return_value = [_seller_msg("Counter offer.")]

    stats = poller.run_one_poll_iteration()

    assert stats["checked"] == 2
    assert stats["errors"] == 2  # both counter calls raised
    assert stats["counters_fired"] == 0


def test_iteration_with_no_active_negotiations_is_a_noop(tmp_db):
    """No-op fast path: no DB rows, no eBay calls, returns clean stats."""
    stats = poller.run_one_poll_iteration()
    assert stats == {"checked": 0, "errors": 0, "counters_fired": 0}


# ---- poll_loop (async, driven from sync tests via asyncio.run) -------

def test_poll_loop_disabled_returns_immediately(monkeypatch):
    """SELLER_POLL_ENABLED=false short-circuits the loop entry. The coroutine
    must return without scheduling any sleep — wait_for with a tight ceiling
    is the simplest way to assert "actually terminated"."""
    import config
    monkeypatch.setattr(config, "SELLER_POLL_ENABLED", False)

    async def _drive() -> None:
        await asyncio.wait_for(poller.poll_loop(), timeout=1.0)

    asyncio.run(_drive())


def test_poll_loop_runs_iteration_then_can_be_cancelled(monkeypatch, tmp_db):
    """Smoke: with a zero interval, the loop spins iterations and yields to
    the event loop between each. Cancel cleanly after a few ticks and
    confirm the iteration entry point was hit at least once."""
    import config
    monkeypatch.setattr(config, "SELLER_POLL_ENABLED", True)
    monkeypatch.setattr(config, "SELLER_POLL_INTERVAL_SECONDS", 0)

    calls = {"n": 0}

    def fake_iter():
        calls["n"] += 1
        return {"checked": 0, "errors": 0, "counters_fired": 0}

    monkeypatch.setattr(poller, "run_one_poll_iteration", fake_iter)

    async def _drive() -> None:
        task = asyncio.create_task(poller.poll_loop())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())
    assert calls["n"] >= 1
