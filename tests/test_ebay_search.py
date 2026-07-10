"""Tests for browser.ebay.search_ebay against the eBay Browse API.

Strategy: mock the module-level requests.Session (`_session.post` for the token
endpoint and `_session.get` for the search endpoint)
so we can pin the exact request shapes eBay sees. A future refactor that drops
a header, swaps the sort, or breaks the condition-id mapping will fail here
before it ever hits production.
"""

import time
from unittest.mock import MagicMock, patch

import pytest

import config
from agents.criteria_parser import ParsedCriteria
from browser import ebay


@pytest.fixture(autouse=True)
def _reset_token_cache():
    """Each test starts with an empty token cache so caching tests are deterministic."""
    ebay._token = None
    ebay._token_expires_at = None
    yield
    ebay._token = None
    ebay._token_expires_at = None


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "EBAY_APP_ID", "test-app-id")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "test-cert-id")


def _mock_token_response(token: str = "fake-bearer-token", expires_in: int = 7200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"access_token": token, "expires_in": expires_in, "token_type": "Application Access Token"}
    return resp


def _mock_search_response(items: list[dict] | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"itemSummaries": items or [], "total": len(items or []), "limit": 25, "offset": 0}
    return resp


def _sample_item(**overrides) -> dict:
    """One realistic itemSummary from the Browse API, with overrides for variants."""
    base = {
        "itemId": "v1|1234567890|0",
        "title": "Sony WH-1000XM5 Wireless Noise Canceling Headphones",
        "price": {"value": "278.99", "currency": "USD"},
        "shippingOptions": [{"shippingCost": {"value": "0.00", "currency": "USD"}}],
        "condition": "Used",
        "seller": {"username": "audiogear_pro", "feedbackPercentage": "99.5", "feedbackScore": 4821},
        "itemWebUrl": "https://www.ebay.com/itm/1234567890",
        "image": {"imageUrl": "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"},
        "itemCreationDate": "2026-06-10T14:22:00.000Z",
    }
    base.update(overrides)
    return base


# ---------- Token caching ----------

def test_token_is_fetched_with_basic_auth_and_client_credentials(configured):
    with patch("browser.ebay._session.post", return_value=_mock_token_response()) as mock_post:
        token = ebay._get_oauth_token()

    assert token == "fake-bearer-token"
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.ebay.com/identity/v1/oauth2/token"
    assert kwargs["auth"] == ("test-app-id", "test-cert-id")
    assert kwargs["data"] == {"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"}


def test_token_is_cached_within_ttl(configured):
    with patch("browser.ebay._session.post", return_value=_mock_token_response()) as mock_post:
        ebay._get_oauth_token()
        ebay._get_oauth_token()
        ebay._get_oauth_token()
    assert mock_post.call_count == 1


def test_token_refreshes_after_expiry(configured):
    with patch("browser.ebay._session.post", return_value=_mock_token_response(expires_in=1)) as mock_post:
        ebay._get_oauth_token()
        # Force expiry by rewinding the cache timestamp past the 60s safety margin
        ebay._token_expires_at = time.time() - 1
        ebay._get_oauth_token()
    assert mock_post.call_count == 2


# ---------- Query/filter construction ----------

def _capture_search_call(criteria: ParsedCriteria, **search_kwargs):
    """Helper: run search_ebay with mocked HTTP and return the kwargs requests.get saw."""
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response()) as mock_get:
        ebay.search_ebay(criteria, **search_kwargs)
    return mock_get.call_args


def test_query_includes_negative_keywords_as_dash_tokens(configured):
    criteria = ParsedCriteria(title_keywords="sony headphones", must_not_keywords=["broken", "for parts"])
    args, kwargs = _capture_search_call(criteria)

    params = kwargs["params"]
    assert params["q"] == "sony headphones -broken -for parts"


def test_filter_includes_price_ceiling_when_set(configured):
    criteria = ParsedCriteria(title_keywords="x", max_price=300.0)
    _, kwargs = _capture_search_call(criteria)
    assert "price:[..300],priceCurrency:USD" in kwargs["params"]["filter"]


def test_filter_omits_price_clause_when_max_price_none(configured):
    criteria = ParsedCriteria(title_keywords="x", max_price=None)
    _, kwargs = _capture_search_call(criteria)
    assert "price:" not in kwargs["params"].get("filter", "")


@pytest.mark.parametrize("floor,expected_ids", [
    ("new", "1000"),
    ("refurbished", "1000|2000|2010|2020|2030"),
    ("used", "1000|2000|2010|2020|2030|3000|4000|5000|6000"),
])
def test_condition_floor_maps_to_inclusive_condition_ids(configured, floor, expected_ids):
    criteria = ParsedCriteria(title_keywords="x", condition_floor=floor)
    _, kwargs = _capture_search_call(criteria)
    assert f"conditionIds:{{{expected_ids}}}" in kwargs["params"]["filter"]


def test_condition_floor_any_or_none_omits_condition_filter(configured):
    for floor in ("any", None):
        criteria = ParsedCriteria(title_keywords="x", condition_floor=floor)
        _, kwargs = _capture_search_call(criteria)
        assert "conditionIds" not in kwargs["params"].get("filter", "")


def test_search_sends_bearer_token_and_marketplace_header(configured):
    criteria = ParsedCriteria(title_keywords="x")
    _, kwargs = _capture_search_call(criteria)
    assert kwargs["headers"]["Authorization"] == "Bearer fake-bearer-token"
    assert kwargs["headers"]["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"


def test_search_omits_sort_param_to_use_ebay_best_match_default(configured):
    """We rely on eBay's default `bestMatch` ranking; sending sort=price surfaces
    accessories ahead of the actual product. Explicit assertion so a future
    refactor that 're-adds sort for predictability' fails here."""
    criteria = ParsedCriteria(title_keywords="x")
    _, kwargs = _capture_search_call(criteria, limit=25)
    assert "sort" not in kwargs["params"]
    assert kwargs["params"]["limit"] == 25


# ---------- Response mapping ----------

def test_response_maps_to_listing_objects(configured):
    item = _sample_item()
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response([item])):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x"))

    assert len(listings) == 1
    listing = listings[0]
    assert listing.ebay_item_id == "v1|1234567890|0"
    assert listing.title == "Sony WH-1000XM5 Wireless Noise Canceling Headphones"
    assert listing.price == 278.99
    assert listing.shipping_cost == 0.0
    assert listing.condition == "Used"
    assert listing.seller_id == "audiogear_pro"
    assert listing.seller_rating == 99.5
    assert listing.seller_feedback_count == 4821
    assert listing.url == "https://www.ebay.com/itm/1234567890"
    assert listing.image_url == "https://i.ebayimg.com/images/g/abc/s-l1600.jpg"
    assert listing.listed_at == "2026-06-10T14:22:00.000Z"


def test_missing_optional_fields_become_none(configured):
    """Some itemSummaries omit shipping, image, or itemCreationDate — must not crash."""
    item = _sample_item()
    del item["shippingOptions"]
    del item["image"]
    del item["itemCreationDate"]
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response([item])):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x"))

    assert listings[0].shipping_cost is None
    assert listings[0].image_url is None
    assert listings[0].listed_at is None


# ---------- Client-side min_seller_rating filter ----------

def test_min_seller_rating_drops_sub_threshold_sellers(configured):
    items = [
        _sample_item(itemId="hi", seller={"username": "good", "feedbackPercentage": "99.5", "feedbackScore": 1000}),
        _sample_item(itemId="lo", seller={"username": "bad", "feedbackPercentage": "82.0", "feedbackScore": 1000}),
    ]
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response(items)):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x", min_seller_rating=95.0))

    assert [l.seller_id for l in listings] == ["good"]


# ---------- Client-side price-floor filter (accessory suppression) ----------

def test_price_floor_drops_listings_below_10_percent_of_max_price(configured):
    """A $5 charger for a $300 product is almost certainly noise — drop it."""
    items = [
        _sample_item(itemId="cheap", price={"value": "9.99", "currency": "USD"}),
        _sample_item(itemId="real", price={"value": "250.00", "currency": "USD"}),
    ]
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response(items)):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x", max_price=300.0))

    assert [l.ebay_item_id for l in listings] == ["real"]


def test_price_floor_inactive_when_max_price_unset(configured):
    items = [_sample_item(itemId="cheap", price={"value": "1.00", "currency": "USD"})]
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response(items)):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x"))
    assert len(listings) == 1


def test_no_min_seller_rating_keeps_all(configured):
    items = [
        _sample_item(itemId="a", seller={"username": "x", "feedbackPercentage": "70.0", "feedbackScore": 5}),
        _sample_item(itemId="b", seller={"username": "y", "feedbackPercentage": "99.9", "feedbackScore": 5}),
    ]
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response(items)):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x"))

    assert len(listings) == 2


# ---------- Error handling ----------

def test_non_200_search_raises_ebay_search_error(configured):
    bad = MagicMock()
    bad.status_code = 500
    bad.text = "internal server error"
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=bad):
        with pytest.raises(ebay.EbaySearchError):
            ebay.search_ebay(ParsedCriteria(title_keywords="x"))


# ---------- buyingOptions plumbing (for Best Offer detection) ----------

def test_buying_options_threaded_into_listing(configured):
    """Browse API exposes which listings accept Best Offer via the buyingOptions
    array. The send-route needs that value at the column level so it can pick
    PlaceOffer vs AAQ without re-parsing raw_data on every render."""
    item = _sample_item(buyingOptions=["FIXED_PRICE", "BEST_OFFER"])
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response([item])):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x"))
    assert listings[0].buying_options == ["FIXED_PRICE", "BEST_OFFER"]


def test_missing_buying_options_becomes_empty_list(configured):
    item = _sample_item()  # no buyingOptions key at all
    item.pop("buyingOptions", None)
    with patch("browser.ebay._session.post", return_value=_mock_token_response()), \
         patch("browser.ebay._session.get", return_value=_mock_search_response([item])):
        listings = ebay.search_ebay(ParsedCriteria(title_keywords="x"))
    assert listings[0].buying_options == []


def test_non_200_token_raises_ebay_search_error(configured):
    bad = MagicMock()
    bad.status_code = 401
    bad.text = "invalid credentials"
    with patch("browser.ebay._session.post", return_value=bad):
        with pytest.raises(ebay.EbaySearchError):
            ebay._get_oauth_token()
