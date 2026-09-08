"""Tests for the LangGraph discover node.

We mock search_ebay so tests are hermetic — no network, no eBay creds needed.
The node's job is small but it's the only thing connecting the search criteria
the user typed to actual listings in the database, so it's worth pinning.
"""

from unittest.mock import patch

import pytest

from agents import graph
from integrations.ebay_search import EbaySearchError, Listing
from db import repo
from pricing.google_shopping import PricingSourceError


def _make_search(**criteria_overrides) -> int:
    structured = {
        "title_keywords": "sony wh-1000xm4",
        "must_not_keywords": ["cable", "parts"],
        "condition_floor": "used",
        "min_seller_rating": 95.0,
    }
    structured.update(criteria_overrides)
    return repo.create_search(
        criteria_nl="sony headphones under 200",
        criteria_structured=structured,
        max_price=200.0,
    )


def _listing(**overrides) -> Listing:
    base = dict(
        ebay_item_id="v1|111|0",
        title="Sony WH-1000XM4",
        price=150.0,
        shipping_cost=0.0,
        condition="Used",
        seller_id="seller1",
        seller_rating=99.5,
        seller_feedback_count=1000,
        url="https://www.ebay.com/itm/111",
        image_url=None,
        listed_at=None,
        raw_data={},
    )
    base.update(overrides)
    return Listing(**base)


def test_discover_happy_path_persists_listings(tmp_db):
    search_id = _make_search()
    fake_listings = [_listing(ebay_item_id=f"v1|{i}|0") for i in range(3)]

    with patch("agents.graph.search_ebay", return_value=_result(fake_listings)) as mock_search:
        result = graph.discover({"search_id": search_id})

    # search_ebay called with criteria built from the persisted search row
    mock_search.assert_called_once()
    criteria_arg = mock_search.call_args.args[0]
    assert criteria_arg.title_keywords == "sony wh-1000xm4"
    assert criteria_arg.max_price == 200.0
    assert criteria_arg.min_seller_rating == 95.0

    # Listings persisted; status stays 'discovering' because reference_prices
    # hasn't run yet. The full-graph test below verifies the awaiting_selection flip.
    assert len(repo.list_listings(search_id)) == 3
    assert repo.get_search(search_id)["status"] == "discovering"
    assert result.get("error") is None


def test_discover_ebay_error_sets_failed_status_and_records_message(tmp_db):
    search_id = _make_search()

    with patch("agents.graph.search_ebay", side_effect=EbaySearchError("eBay returned 503")):
        result = graph.discover({"search_id": search_id})

    row = repo.get_search(search_id)
    assert row["status"] == "failed"
    assert "503" in row["error_message"]
    assert "503" in result["error"]
    assert repo.list_listings(search_id) == []


def test_discover_passes_max_price_from_search_row_when_missing_in_structured(tmp_db):
    """max_price lives at the top level of the search row, not always in criteria_structured.
    The node must use the row's max_price as the source of truth."""
    structured = {"title_keywords": "x", "must_not_keywords": [], "condition_floor": None, "min_seller_rating": None}
    search_id = repo.create_search(criteria_nl="x", criteria_structured=structured, max_price=75.0)

    with patch("agents.graph.search_ebay", return_value=_result([])) as mock_search:
        graph.discover({"search_id": search_id})

    criteria_arg = mock_search.call_args.args[0]
    assert criteria_arg.max_price == 75.0


def test_discover_flips_status_to_discovering_before_calling_ebay(tmp_db):
    """If eBay takes a few seconds, the dashboard should show 'discovering' while
    the call is in flight. We verify by snapshotting status inside the mock."""
    search_id = _make_search()
    snapshot = {}

    def _snapshot_status(*args, **kwargs):
        snapshot["status"] = repo.get_search(search_id)["status"]
        return _result([])

    with patch("agents.graph.search_ebay", side_effect=_snapshot_status):
        graph.discover({"search_id": search_id})

    assert snapshot["status"] == "discovering"


def test_compiled_graph_runs_end_to_end(tmp_db):
    """Smoke test: compile the graph and invoke it — proves the wiring works."""
    search_id = _make_search()
    with patch("agents.graph.search_ebay", return_value=_result([_listing()])), \
         patch("agents.graph.google_shopping.fetch", return_value=([], 0.02)):
        compiled = graph.build_graph()
        compiled.invoke({"search_id": search_id})
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


def test_compiled_graph_zero_results_stays_failed_with_message(tmp_db):
    """H1 regression test. discover() marks a zero-Best-Offer-match search
    'failed' with an explanatory message; reference_prices must not overwrite
    that with 'awaiting_selection' when the whole graph runs end to end. This
    must exercise the COMPILED graph, not discover() in isolation — that
    isolation is exactly what let the original bug through six reviews.

    google_shopping.fetch is intentionally NOT mocked to succeed here: if
    reference_prices's early-return regresses, this test would otherwise
    pass anyway on network/credential failure inside fetch. Failing fetch
    loudly (not via PricingSourceError) makes a regression here impossible
    to miss."""
    search_id = _make_search()
    with patch("agents.graph.search_ebay", return_value=_result([])), \
         patch(
             "agents.graph.google_shopping.fetch",
             side_effect=AssertionError(
                 "reference_prices should have early-returned before calling "
                 "google_shopping.fetch on an already-failed search"
             ),
         ):
        compiled = graph.build_graph()
        compiled.invoke({"search_id": search_id})

    row = repo.get_search(search_id)
    assert row["status"] == "failed"
    assert "best offer" in row["error_message"].lower()
    # Reference pricing must not have spent anything on a failed search.
    assert repo.list_reference_prices(search_id) == []


def test_compiled_graph_ebay_search_error_stays_failed_with_message(tmp_db):
    """H1 regression test, EbaySearchError path (predates Phase 0a but is
    the same defect). Must run the compiled graph end to end."""
    search_id = _make_search()
    with patch("agents.graph.search_ebay", side_effect=EbaySearchError("eBay returned 503")), \
         patch(
             "agents.graph.google_shopping.fetch",
             side_effect=AssertionError(
                 "reference_prices should have early-returned before calling "
                 "google_shopping.fetch on an already-failed search"
             ),
         ):
        compiled = graph.build_graph()
        compiled.invoke({"search_id": search_id})

    row = repo.get_search(search_id)
    assert row["status"] == "failed"
    assert "503" in row["error_message"]
    assert repo.list_reference_prices(search_id) == []


# ---------- reference_prices node ----------

def test_reference_prices_persists_aggregates_and_flips_status(tmp_db):
    search_id = _make_search()
    fake_points = [
        {"price": 250.0, "condition": "new", "url": "u1", "title": "t1", "currency": "USD"},
        {"price": 270.0, "condition": "new", "url": "u2", "title": "t2", "currency": "USD"},
        {"price": 290.0, "condition": "new", "url": "u3", "title": "t3", "currency": "USD"},
    ]
    with patch("agents.graph.google_shopping.fetch", return_value=(fake_points, 0.05)):
        graph.reference_prices({"search_id": search_id})

    rows = repo.list_reference_prices(search_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "google_shopping"
    assert rows[0]["condition"] == "new"
    assert rows[0]["median"] == 270.0
    assert rows[0]["cost_usd"] == pytest.approx(0.05)
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


def test_reference_prices_groups_new_and_used_into_separate_rows(tmp_db):
    search_id = _make_search()
    points = [
        {"price": 200.0, "condition": "new", "url": "u1", "title": "t1", "currency": "USD"},
        {"price": 150.0, "condition": "used", "url": "u2", "title": "t2", "currency": "USD"},
    ]
    with patch("agents.graph.google_shopping.fetch", return_value=(points, 0.04)):
        graph.reference_prices({"search_id": search_id})

    rows = repo.list_reference_prices(search_id)
    conditions = sorted(r["condition"] for r in rows)
    assert conditions == ["new", "used"]
    # Cost split across the two condition buckets (0.04 / 2 = 0.02 each)
    assert all(r["cost_usd"] == pytest.approx(0.02) for r in rows)


def test_reference_prices_source_failure_is_non_fatal(tmp_db):
    search_id = _make_search()
    repo.update_search_status(search_id, "discovering")
    with patch("agents.graph.google_shopping.fetch", side_effect=PricingSourceError("apify down")):
        graph.reference_prices({"search_id": search_id})

    assert repo.list_reference_prices(search_id) == []
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


def test_reference_prices_skips_when_budget_exhausted(tmp_db, monkeypatch):
    import config as _config
    monkeypatch.setattr(_config, "APIFY_BUDGET_USD", 0.05)
    search_id = _make_search()
    # Pre-existing spend equal to the budget cap
    repo.add_reference_price(search_id, "google_shopping", "new", 1.0, 1.0, 1.0, [], cost_usd=0.05)

    with patch("agents.graph.google_shopping.fetch") as mock_fetch:
        graph.reference_prices({"search_id": search_id})

    mock_fetch.assert_not_called()
    # Only the pre-existing row remains; no new persistence
    rows = repo.list_reference_prices(search_id)
    assert len(rows) == 1
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


def test_reference_prices_empty_results_still_flips_status(tmp_db):
    """Actor returns zero items (e.g. obscure query) — node still finishes cleanly."""
    search_id = _make_search()
    with patch("agents.graph.google_shopping.fetch", return_value=([], 0.01)):
        graph.reference_prices({"search_id": search_id})

    assert repo.list_reference_prices(search_id) == []
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


from integrations import ebay_search as _ebay_search_mod


def _result(listings, cost_usd=0.05, warning=None):
    return _ebay_search_mod.SearchResult(
        listings=listings, cost_usd=cost_usd, warning=warning
    )


def _bo_listing(item_id="1", price=200.0, rating=99.5):
    return _ebay_search_mod.Listing(
        ebay_item_id=item_id, title=f"Listing {item_id}", price=price,
        url=f"https://www.ebay.com/itm/{item_id}", seller_rating=rating,
        buying_options=["BEST_OFFER"], raw_data={},
    )


def test_discover_records_the_run_cost(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    with patch("agents.graph.search_ebay", return_value=_result([_bo_listing()], cost_usd=0.052)):
        graph.discover({"search_id": search_id})
    assert repo.get_search(search_id)["search_cost_usd"] == pytest.approx(0.052)


def test_discover_stores_listings(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    with patch("agents.graph.search_ebay", return_value=_result([_bo_listing("1"), _bo_listing("2")])):
        graph.discover({"search_id": search_id})
    assert len(repo.list_listings(search_id)) == 2


def test_discover_surfaces_the_degraded_feedback_warning(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    warning = _ebay_search_mod.WARNING_NO_SELLER_FEEDBACK
    with patch("agents.graph.search_ebay", return_value=_result([_bo_listing()], warning=warning)):
        graph.discover({"search_id": search_id})
    row = repo.get_search(search_id)
    assert row["warning_message"] == warning
    # A warning is not a failure — the listings are real and still usable.
    assert row["status"] != "failed"
    assert row["error_message"] is None


def test_zero_results_explains_best_offer_filtering(tmp_db):
    """Filtering to Best-Offer-only makes empty results common. Landing in
    awaiting_selection with nothing to select reads as a broken app."""
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    with patch("agents.graph.search_ebay", return_value=_result([])):
        graph.discover({"search_id": search_id})
    row = repo.get_search(search_id)
    assert row["status"] == "failed"
    assert "best offer" in row["error_message"].lower()


def test_zero_results_still_records_the_cost(tmp_db):
    """The run was paid for whether or not it matched anything."""
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    with patch("agents.graph.search_ebay", return_value=_result([], cost_usd=0.048)):
        graph.discover({"search_id": search_id})
    assert repo.get_search(search_id)["search_cost_usd"] == pytest.approx(0.048)


def test_search_error_marks_the_search_failed(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    err = _ebay_search_mod.EbaySearchError("Your monthly search budget is used up.")
    with patch("agents.graph.search_ebay", side_effect=err):
        graph.discover({"search_id": search_id})
    row = repo.get_search(search_id)
    assert row["status"] == "failed"
    assert "budget" in row["error_message"].lower()
