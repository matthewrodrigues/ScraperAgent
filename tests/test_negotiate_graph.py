"""Tests for the negotiate subgraph (strategy_chooser + draft_message nodes).

We mock the Claude call (draft_message) so tests are hermetic. The strategy
chooser is exercised through its real implementation — it's already tested in
test_strategy_chooser.py at the unit level; here we test the wiring.
"""

from unittest.mock import patch

import pytest

from agents import graph
from agents.negotiate import DraftedMessage, NegotiationDraftError
from db import repo


# Realistic placeholder usage dict — draft_message now returns
# `(DraftedMessage, usage_dict)`. Tests that mock draft_message must mimic
# that shape; the helper keeps the test bodies tidy.
_FAKE_USAGE = {"input_tokens": 1500, "output_tokens": 80, "cost_usd": 0.0057}


def _drafted_with_usage(drafted: DraftedMessage):
    return (drafted, _FAKE_USAGE)


def _seed_search_with_one_listing(price: float = 100.0, max_price: float = 100.0) -> tuple[int, int]:
    search_id = repo.create_search(
        criteria_nl="x",
        criteria_structured={"title_keywords": "x"},
        max_price=max_price,
    )
    repo.update_search_status(search_id, "negotiating")
    ids = repo.add_listings(search_id, [{
        "ebay_item_id": "v1|1|0",
        "title": "Test item",
        "price": price,
        "url": "https://x/1",
        "seller_rating": 99.5,
    }])
    return search_id, ids[0]


def test_strategy_chooser_creates_negotiation_row(tmp_db):
    search_id, listing_id = _seed_search_with_one_listing(price=120.0, max_price=200.0)
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.05)

    out = graph.strategy_chooser({"search_id": search_id, "listing_id": listing_id})

    nid = out["negotiation_id"]
    n = repo.get_negotiation(nid)
    assert n["listing_id"] == listing_id
    assert n["strategy"] == "anchor_low"  # 20% over $100 median
    assert n["current_offer"] > 0
    assert n["current_offer"] <= 200.0  # under max_price


def test_strategy_chooser_is_idempotent(tmp_db):
    """Re-running strategy_chooser for the same listing reuses the existing
    negotiation row instead of creating a duplicate."""
    search_id, listing_id = _seed_search_with_one_listing()
    nid1 = graph.strategy_chooser({"search_id": search_id, "listing_id": listing_id})["negotiation_id"]
    nid2 = graph.strategy_chooser({"search_id": search_id, "listing_id": listing_id})["negotiation_id"]
    assert nid1 == nid2


def test_draft_message_node_persists_pending_message(tmp_db):
    search_id, listing_id = _seed_search_with_one_listing()
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    fake_drafted = DraftedMessage(body="Hi, would you accept $85?" * 2, offer_amount=85.0)

    with patch("agents.graph.draft_message", return_value=_drafted_with_usage(fake_drafted)):
        graph.draft_message_node({"search_id": search_id, "listing_id": listing_id, "negotiation_id": nid})

    pending = repo.get_pending_message_for_search(search_id)
    assert pending is not None
    assert "would you accept" in pending["body"].lower()
    assert pending["offer_amount"] == 85.0


def test_draft_message_node_handles_llm_failure_by_walking_away(tmp_db):
    search_id, listing_id = _seed_search_with_one_listing()
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)

    with patch("agents.graph.draft_message", side_effect=NegotiationDraftError("API down")):
        out = graph.draft_message_node({"search_id": search_id, "listing_id": listing_id, "negotiation_id": nid})

    assert "API down" in out["error"]
    n = repo.get_negotiation(nid)
    assert n["status"] == "walked_away"
    assert n["walked_away_at"] is not None
    assert repo.get_pending_message_for_search(search_id) is None


def test_compiled_negotiate_graph_runs_end_to_end(tmp_db):
    """Strategy chooser feeds negotiation_id into draft_message via state."""
    search_id, listing_id = _seed_search_with_one_listing(price=120.0, max_price=200.0)
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.05)

    fake_drafted = DraftedMessage(body="Hello there, $96 sound good?", offer_amount=96.0)
    with patch("agents.graph.draft_message", return_value=_drafted_with_usage(fake_drafted)):
        compiled = graph.build_negotiate_graph()
        compiled.invoke({"search_id": search_id, "listing_id": listing_id})

    pending = repo.get_pending_message_for_search(search_id)
    assert pending is not None
    assert pending["offer_amount"] == 96.0


# ---------- Counter graph ----------

def test_counter_draft_includes_history_in_llm_call(tmp_db):
    search_id, listing_id = _seed_search_with_one_listing()
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    # Prior round: agent sent + seller replied
    repo.add_message(nid, "agent", "Would you take $80?", 80.0, status="sent")
    repo.add_message(nid, "seller", "I can do $90.", None, status="received")

    fake_drafted = DraftedMessage(body="Could we meet at $85 to close this today?", offer_amount=85.0)
    with patch("agents.graph.draft_message", return_value=_drafted_with_usage(fake_drafted)) as mock_draft:
        graph.draft_counter_node({"search_id": search_id, "listing_id": listing_id, "negotiation_id": nid})

    # Pulled history was passed to the drafter
    history = mock_draft.call_args.kwargs.get("history") or mock_draft.call_args.args[4]
    assert len(history) == 2
    assert history[0]["role"] == "agent"
    assert history[1]["role"] == "seller"

    # New pending message persisted
    pending = repo.get_pending_message_for_search(search_id)
    assert pending is not None
    assert pending["offer_amount"] == 85.0


def test_counter_draft_refuses_after_round_cap(tmp_db):
    """After MAX_ROUNDS agent-side messages, counter-drafting is rejected
    without consulting Claude."""
    search_id, listing_id = _seed_search_with_one_listing()
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    with repo.get_conn() as conn:
        conn.execute("UPDATE negotiations SET rounds = ? WHERE id = ?", (graph.MAX_ROUNDS, nid))

    with patch("agents.graph.draft_message") as mock_draft:
        out = graph.draft_counter_node({"search_id": search_id, "listing_id": listing_id, "negotiation_id": nid})

    mock_draft.assert_not_called()
    assert "cap" in (out.get("error") or "").lower()
    assert repo.get_pending_message_for_search(search_id) is None


def test_counter_graph_runs_end_to_end(tmp_db):
    search_id, listing_id = _seed_search_with_one_listing()
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    repo.add_message(nid, "agent", "Would you take $80?", 80.0, status="sent")
    repo.add_message(nid, "seller", "Can do $90.", None, status="received")

    fake = DraftedMessage(body="Let's meet at $85; I'd close right away.", offer_amount=85.0)
    with patch("agents.graph.draft_message", return_value=_drafted_with_usage(fake)):
        graph.build_counter_graph().invoke(
            {"search_id": search_id, "listing_id": listing_id, "negotiation_id": nid}
        )

    pending = repo.get_pending_message_for_search(search_id)
    assert pending is not None and pending["offer_amount"] == 85.0
