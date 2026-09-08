"""LangGraph wiring for the search → negotiate pipeline.

v1 is a single-node graph: START -> discover -> END. The discover node reads
a persisted `search` row, hits the eBay Browse API, writes the resulting
listings, and flips the search status so the dashboard knows the human can
now pick one to negotiate on.

Future nodes (present, select, negotiate, done) will be added to this same
file as they're built. Keeping them here while the graph is small avoids the
file-per-node ceremony that pays off only once nodes have real logic.
"""

import logging
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from agents.criteria_parser import ParsedCriteria
from agents.negotiate import NegotiationDraftError, draft_message
from integrations.ebay_search import EbaySearchError, search_ebay
from db import repo
from pricing import aggregator, cost_guard, google_shopping
from pricing.google_shopping import PricingSourceError
from strategies import pick_strategy


log = logging.getLogger(__name__)


class SearchState(TypedDict, total=False):
    """LangGraph state. Grows as nodes are added; for now only discover writes here.

    All persistent state lives in SQLite — this dict is for in-flight values that
    nodes hand off to each other. `search_id` is the only field every node needs.
    """

    search_id: int
    error: str | None


def discover(state: SearchState) -> SearchState:
    search_id = state["search_id"]
    repo.update_search_status(search_id, "discovering")

    row = repo.get_search(search_id)
    structured = row["criteria_structured"]
    criteria = ParsedCriteria(
        title_keywords=structured.get("title_keywords", ""),
        must_not_keywords=structured.get("must_not_keywords") or [],
        condition_floor=structured.get("condition_floor"),
        # max_price is authoritative on the search row, not in the structured blob —
        # the form requires it at submit time even when Haiku didn't parse one.
        max_price=row["max_price"],
        min_seller_rating=structured.get("min_seller_rating"),
    )

    try:
        result = search_ebay(criteria, limit=25)
    except EbaySearchError as exc:
        log.warning("eBay search failed for search_id=%s: %s", search_id, exc)
        repo.set_search_error(search_id, str(exc))
        repo.update_search_status(search_id, "failed")
        return {"error": str(exc)}

    # The run was paid for regardless of what it matched, so record the cost
    # before any early return.
    repo.set_search_cost(search_id, result.cost_usd)

    if result.warning:
        repo.set_search_warning(search_id, result.warning)

    if not result.listings:
        # Best-Offer-only filtering makes empty results common. Reaching
        # awaiting_selection with nothing to select reads as a broken app, so
        # end the search with something actionable instead. "failed" is the
        # mechanism; the message carries the meaning.
        msg = (
            "No Best Offer listings matched. This search only returns listings "
            "where the seller accepts offers — try a wider price range or "
            "allowing used condition."
        )
        log.info("no BO listings for search_id=%s", search_id)
        repo.set_search_error(search_id, msg)
        repo.update_search_status(search_id, "failed")
        return {"error": msg}

    repo.add_listings(search_id, [l.model_dump() for l in result.listings])
    # Status stays at 'discovering' — reference_prices node flips it to
    # 'awaiting_selection' when both nodes have completed.
    return {}


# Rough pre-flight cost estimate. Actual spend is read back from the run and
# recorded; this is just the guardrail check before launching the actor.
# Real-world: burbn/google-shopping-scraper costs ~$0.49 per run for 25 results,
# which sits right at the $0.50 APIFY_BUDGET_USD cap. Plenty of headroom *per
# search* but means adding more sources will require either expanding the budget
# or switching to a cheaper actor.
_REF_PRICES_PLANNED_COST_USD = 0.50


def reference_prices(state: SearchState) -> SearchState:
    """Gather Google Shopping price points and persist aggregates per condition.

    Non-fatal: any failure (budget exceeded, actor error, missing token) is
    logged and the graph proceeds to END. Downstream code that wants ref-prices
    must tolerate their absence — discover-only data is still useful.
    """
    search_id = state["search_id"]

    if not cost_guard.under_budget(search_id, _REF_PRICES_PLANNED_COST_USD):
        log.warning("Apify budget cap reached for search_id=%s; skipping ref-prices", search_id)
        repo.update_search_status(search_id, "awaiting_selection")
        return {}

    row = repo.get_search(search_id)
    structured = row["criteria_structured"]
    criteria = ParsedCriteria(
        title_keywords=structured.get("title_keywords", ""),
        must_not_keywords=structured.get("must_not_keywords") or [],
        condition_floor=structured.get("condition_floor"),
        max_price=row["max_price"],
        min_seller_rating=structured.get("min_seller_rating"),
    )

    try:
        price_points, cost_usd = google_shopping.fetch(criteria)
    except PricingSourceError as exc:
        log.warning("google_shopping failed for search_id=%s: %s", search_id, exc)
        repo.update_search_status(search_id, "awaiting_selection")
        return {}

    buckets = aggregator.by_condition(price_points)
    # Spread the run cost across the per-condition rows we'll write. Trivial
    # accounting today (usually one bucket), but ensures sum_apify_cost stays
    # accurate as we add sources that split data more.
    cost_per_bucket = cost_usd / len(buckets) if buckets else 0.0
    for condition, points in buckets.items():
        stats = aggregator.percentiles(points)
        repo.add_reference_price(
            search_id=search_id,
            source="google_shopping",
            condition=condition,
            median=stats["median"],
            p25=stats["p25"],
            p75=stats["p75"],
            raw_data=points,
            cost_usd=cost_per_bucket,
        )

    repo.update_search_status(search_id, "awaiting_selection")
    return {}


def build_graph():
    """Compile the search graph. No checkpointer yet — added when a node
    needs resumption across HTTP requests."""
    g = StateGraph(SearchState)
    g.add_node("discover", discover)
    g.add_node("reference_prices", reference_prices)
    g.add_edge(START, "discover")
    g.add_edge("discover", "reference_prices")
    g.add_edge("reference_prices", END)
    return g.compile()


# ---- Negotiate subgraph --------------------------------------------------
# Runs on listing-selection, separately from the search graph above. State is
# small (just IDs); everything substantive is read/written via the DB so the
# template can poll while this runs in the background.

class NegotiateState(TypedDict, total=False):
    search_id: int
    listing_id: int
    negotiation_id: int  # populated by strategy_chooser; consumed by draft_message
    error: str | None


def strategy_chooser(state: NegotiateState) -> NegotiateState:
    """Pure-rules step: pick a strategy + opening offer, persist the
    negotiation row. No LLM call. Idempotent at the row level — if a
    negotiation already exists for this listing we return its id rather than
    creating a duplicate."""
    listing_id = state["listing_id"]
    existing = repo.get_active_negotiation_for_listing(listing_id)
    if existing is not None:
        return {"negotiation_id": existing["id"]}

    listing = repo.get_listing(listing_id)
    search = repo.get_search(state["search_id"])
    refs = repo.list_reference_prices(state["search_id"])
    strategy = pick_strategy(listing, refs, max_price=search["max_price"])

    nid = repo.create_negotiation(
        listing_id=listing_id,
        strategy=strategy.name,
        strategy_inputs=strategy.inputs,
        current_offer=strategy.offer_amount,
    )
    log.info(
        "strategy=%s offer=$%.2f chosen for listing %d (search %d)",
        strategy.name, strategy.offer_amount, listing_id, state["search_id"],
    )
    return {"negotiation_id": nid}


def draft_message_node(state: NegotiateState) -> NegotiateState:
    """Claude draft step. On failure, mark the negotiation walked_away so the
    user isn't stuck staring at a 'drafting...' spinner forever — they can
    deselect and try a different listing."""
    negotiation = repo.get_negotiation(state["negotiation_id"])
    listing = repo.get_listing(negotiation["listing_id"])
    search = repo.get_search(state["search_id"])
    refs = repo.list_reference_prices(state["search_id"])

    # Rebuild the Strategy object so we can hand the right system prompt to Claude.
    # We don't persist the system prompt — pick_strategy is the source of truth.
    strategy = pick_strategy(listing, refs, max_price=search["max_price"])

    try:
        drafted, usage = draft_message(strategy, listing, search, refs)
    except NegotiationDraftError as exc:
        log.warning("draft failed for negotiation %s: %s", state["negotiation_id"], exc)
        repo.update_negotiation_status(state["negotiation_id"], "walked_away")
        return {"error": str(exc)}

    repo.add_message(
        negotiation_id=state["negotiation_id"],
        role="agent",
        body=drafted.body,
        offer_amount=drafted.offer_amount,
        status="pending",
        **usage,
    )
    return {}


def build_negotiate_graph():
    g = StateGraph(NegotiateState)
    g.add_node("strategy_chooser", strategy_chooser)
    g.add_node("draft_message", draft_message_node)
    g.add_edge(START, "strategy_chooser")
    g.add_edge("strategy_chooser", "draft_message")
    g.add_edge("draft_message", END)
    return g.compile()


# ---- Counter draft graph -------------------------------------------------
# Re-enters the negotiation when the seller has responded. Skips the strategy
# chooser (same strategy applies — PRD says no mid-conversation adaptation)
# and just drafts the next agent turn given the full conversation history.

# Hard cap from PRD §123. After 3 agent-side messages we stop offering counters
# entirely; the user must mark as deal or walk away.
MAX_ROUNDS = 3


def draft_counter_node(state: NegotiateState) -> NegotiateState:
    """Draft the next agent turn given the existing negotiation's history.
    Enforces the round cap before consulting Claude (cheap rejection)."""
    nid = state["negotiation_id"]
    negotiation = repo.get_negotiation(nid)
    if negotiation is None:
        return {"error": f"negotiation {nid} not found"}

    if negotiation["rounds"] >= MAX_ROUNDS:
        log.info("negotiation %s hit %d-round cap; skipping counter draft", nid, MAX_ROUNDS)
        return {"error": f"round cap ({MAX_ROUNDS}) reached"}

    listing = repo.get_listing(negotiation["listing_id"])
    search = repo.get_search(state["search_id"])
    refs = repo.list_reference_prices(state["search_id"])
    history = repo.get_messages_by_negotiation(nid)

    strategy = pick_strategy(listing, refs, max_price=search["max_price"])

    try:
        drafted, usage = draft_message(strategy, listing, search, refs, history=history)
    except NegotiationDraftError as exc:
        log.warning("counter draft failed for negotiation %s: %s", nid, exc)
        return {"error": str(exc)}

    repo.add_message(
        negotiation_id=nid,
        role="agent",
        body=drafted.body,
        offer_amount=drafted.offer_amount,
        status="pending",
        **usage,
    )
    # Keep negotiation status='open' until the user approves+sends.
    return {}


def build_counter_graph():
    """Counter-only graph: skips strategy_chooser since the strategy is fixed
    for the lifetime of a negotiation. Takes an existing negotiation_id."""
    g = StateGraph(NegotiateState)
    g.add_node("draft_counter", draft_counter_node)
    g.add_edge(START, "draft_counter")
    g.add_edge("draft_counter", END)
    return g.compile()
