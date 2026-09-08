"""Tests for integrations.ebay_search.search_ebay.

Mocks the Apify client at module level, matching tests/test_google_shopping.py.
No test touches the network.
"""

from unittest.mock import MagicMock, patch

import pytest

import config
from agents.criteria_parser import ParsedCriteria
from integrations import ebay_search


@pytest.fixture(autouse=True)
def _reset_client():
    ebay_search._client = None
    yield
    ebay_search._client = None


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", "test-apify-token")
    monkeypatch.setattr(config, "BROKER_URL", "")
    monkeypatch.setattr(config, "EBAY_SEARCH_BUDGET_USD", 0.15)


def _criteria(**overrides) -> ParsedCriteria:
    base = {
        "title_keywords": "sony wh-1000xm5",
        "must_not_keywords": [],
        "condition_floor": None,
        "max_price": 250.0,
        "min_seller_rating": None,
    }
    base.update(overrides)
    return ParsedCriteria(**base)


def _item(item_id="1", price="$200.00", feedback="99.2%", **overrides) -> dict:
    base = {
        "item_id": item_id,
        "product_title": f"Listing {item_id}",
        "price": price,
        "shipping_cost": "Free",
        "condition": "Pre-Owned",
        "seller_name": f"seller_{item_id}",
        "seller_feedback_percent": feedback,
        "seller_feedback_count": "1,234",
        "product_url": f"https://www.ebay.com/itm/{item_id}",
        "image_url": "https://i.ebayimg.com/x.jpg",
        "buying_format": "Best Offer",
    }
    base.update(overrides)
    return base


def _mock_client(items: list[dict], usage: float = 0.05, status: str = "SUCCEEDED") -> MagicMock:
    client = MagicMock()
    client.actor.return_value.call.return_value = {
        "status": status,
        "defaultDatasetId": "ds-1",
        "usageTotalUsd": usage,
    }
    client.dataset.return_value.iterate_items.return_value = iter(items)
    return client


def _run(criteria, client, limit=25):
    with patch("integrations.ebay_search.ApifyClient", return_value=client):
        return ebay_search.search_ebay(criteria, limit=limit)


# --- actor input -----------------------------------------------------------

def test_input_always_filters_to_best_offer(configured):
    actor_input = ebay_search._build_actor_input(_criteria())
    assert actor_input["buyingFormat"] == "LH_BO"


def test_new_condition_floor_sends_new_only(configured):
    assert ebay_search._build_actor_input(_criteria(condition_floor="new"))["condition"] == "1000"


@pytest.mark.parametrize("floor", ["used", None])
def test_used_or_absent_floor_sends_no_condition_filter(configured, floor):
    """condition_floor is a FLOOR. "used" means used-or-better and must still
    admit new listings, so no filter is sent. Sending "3000" here would
    silently exclude every new listing from used-floor searches."""
    assert "condition" not in ebay_search._build_actor_input(_criteria(condition_floor=floor))


def test_max_price_and_exclusions_are_passed(configured):
    actor_input = ebay_search._build_actor_input(
        _criteria(max_price=250.0, must_not_keywords=["broken", "parts"])
    )
    assert actor_input["maxPrice"] == 250
    assert "broken" in actor_input["excludeKeywords"]
    assert "parts" in actor_input["excludeKeywords"]


# --- happy path ------------------------------------------------------------

def test_returns_mapped_listings_and_cost(configured):
    result = _run(_criteria(), _mock_client([_item("1"), _item("2")], usage=0.05))
    assert [l.ebay_item_id for l in result.listings] == ["1", "2"]
    assert result.cost_usd == pytest.approx(0.05)
    assert result.warning is None


def test_limit_is_applied_client_side(configured):
    items = [_item(str(i)) for i in range(10)]
    assert len(_run(_criteria(), _mock_client(items), limit=3).listings) == 3


def test_declared_ceiling_is_the_discovery_budget(configured):
    """Not APIFY_BUDGET_USD: the key broker debits a friend the declared
    ceiling provisionally, so over-declaring is a real charge against them."""
    client = _mock_client([_item("1")])
    _run(_criteria(), client)
    kwargs = client.actor.return_value.call.call_args.kwargs
    assert float(kwargs["max_total_charge_usd"]) == pytest.approx(0.15)


def test_unmappable_listing_is_skipped_not_fatal(configured):
    result = _run(_criteria(), _mock_client([_item("1"), _item("2", price="Best offer only")]))
    assert [l.ebay_item_id for l in result.listings] == ["1"]


# --- seller rating filter --------------------------------------------------

def test_min_seller_rating_filters_client_side(configured):
    result = _run(
        _criteria(min_seller_rating=99.0),
        _mock_client([_item("1", feedback="99.5%"), _item("2", feedback="95.0%")]),
    )
    assert [l.ebay_item_id for l in result.listings] == ["1"]


def test_partial_feedback_loss_behaves_normally(configured):
    """Some listings lacking feedback is ordinary — a new seller. Only a run
    where EVERY listing lacks it indicates a broken actor contract."""
    result = _run(
        _criteria(min_seller_rating=99.0),
        _mock_client([_item("1", feedback="99.5%"), _item("2", feedback="")]),
    )
    assert [l.ebay_item_id for l in result.listings] == ["1"]
    assert result.warning is None


def test_total_feedback_loss_keeps_listings_and_warns(configured):
    """The filter fails closed, so applying it to all-None data would drop
    every listing and the user would be told their criteria matched nothing.
    Skip the filter, keep the listings, and say so."""
    result = _run(
        _criteria(min_seller_rating=99.0),
        _mock_client([_item("1", feedback=""), _item("2", feedback="")]),
    )
    assert [l.ebay_item_id for l in result.listings] == ["1", "2"]
    assert result.warning == ebay_search.WARNING_NO_SELLER_FEEDBACK


def test_no_warning_when_no_rating_filter_requested(configured):
    """Nothing degraded if the user never asked to filter on rating."""
    result = _run(_criteria(min_seller_rating=None),
                  _mock_client([_item("1", feedback=""), _item("2", feedback="")]))
    assert result.warning is None


# --- errors ----------------------------------------------------------------

def test_non_succeeded_run_raises(configured):
    with pytest.raises(ebay_search.EbaySearchError):
        _run(_criteria(), _mock_client([], status="FAILED"))


def test_budget_exhausted_names_the_budget(configured):
    client = MagicMock()
    client.actor.return_value.call.side_effect = _api_error(402)
    with pytest.raises(ebay_search.EbaySearchError) as excinfo:
        _run(_criteria(), client)
    assert "budget" in str(excinfo.value).lower()


def test_broker_unavailable_mentions_own_keys(configured):
    client = MagicMock()
    client.actor.return_value.call.side_effect = _api_error(503)
    with pytest.raises(ebay_search.EbaySearchError) as excinfo:
        _run(_criteria(), client)
    assert "APIFY_TOKEN" in str(excinfo.value)


def _api_error(status_code: int) -> Exception:
    exc = RuntimeError(f"HTTP {status_code}")
    exc.status_code = status_code
    return exc


def test_zero_results_is_not_an_error(configured):
    """Discovery returns empty; agents.graph.discover decides what to tell the
    user (see Task 4). Raising here would conflate 'no matches' with 'broken'."""
    result = _run(_criteria(), _mock_client([]))
    assert result.listings == []
    assert result.cost_usd == pytest.approx(0.05)
