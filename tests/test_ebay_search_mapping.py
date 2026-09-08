"""Tests for integrations.ebay_search payload parsing.

The Apify actor returns every field as a string, so mapping is parsing. These
are pure functions — no client, no network.
"""

import pytest

from integrations import ebay_search


def _item(**overrides) -> dict:
    """A realistically-shaped actor item; override individual fields per test."""
    base = {
        "item_id": "126543210987",
        "product_title": "Sony WH-1000XM5 Wireless Headphones",
        "price": "$248.00",
        "shipping_cost": "Free",
        "condition": "Pre-Owned",
        "seller_name": "audio_deals_99",
        "seller_feedback_percent": "99.2%",
        "seller_feedback_count": "1,234",
        "product_url": "https://www.ebay.com/itm/126543210987",
        "image_url": "https://i.ebayimg.com/images/g/abc/s-l500.jpg",
        "buying_format": "Best Offer",
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize("raw,expected", [
    ("$248.00", 248.00),
    ("$1,299.99", 1299.99),
    ("248.00", 248.00),
    ("GBP 99.50", 99.50),
    ("", None),
    (None, None),
    ("not a price", None),
])
def test_parse_price(raw, expected):
    assert ebay_search._parse_price(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Free", 0.0),
    ("free shipping", 0.0),
    ("+$5.99", 5.99),
    ("$12.34", 12.34),
    ("", None),
    (None, None),
    ("Varies", None),
])
def test_parse_shipping(raw, expected):
    assert ebay_search._parse_shipping(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("99.2%", 99.2),
    ("100%", 100.0),
    ("98", 98.0),
    ("", None),
    (None, None),
    ("no feedback yet", None),
])
def test_parse_percent(raw, expected):
    assert ebay_search._parse_percent(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("1,234", 1234),
    ("7", 7),
    ("(2,041)", 2041),
    ("", None),
    (None, None),
    ("none", None),
])
def test_parse_count(raw, expected):
    assert ebay_search._parse_count(raw) == expected


def test_map_item_full():
    listing = ebay_search._map_item(_item())
    assert listing.ebay_item_id == "126543210987"
    assert listing.title == "Sony WH-1000XM5 Wireless Headphones"
    assert listing.price == 248.00
    assert listing.shipping_cost == 0.0
    assert listing.condition == "Pre-Owned"
    assert listing.seller_id == "audio_deals_99"
    assert listing.seller_rating == 99.2
    assert listing.seller_feedback_count == 1234
    assert listing.url == "https://www.ebay.com/itm/126543210987"
    assert listing.image_url.endswith("s-l500.jpg")


def test_every_mapped_listing_is_best_offer():
    """Results are filtered to Best Offer server-side, so the flag is implied.
    api/routes/searches.py branches on this to enable the offer flow."""
    assert ebay_search._map_item(_item()).buying_options == ["BEST_OFFER"]


def test_listed_at_is_always_none():
    """The actor exposes no listing date. strategies._listing_age_days already
    tolerates None, costing only the >30-day staleness signal."""
    assert ebay_search._map_item(_item()).listed_at is None


def test_raw_data_round_trips():
    item = _item()
    assert ebay_search._map_item(item).raw_data == item


def test_missing_item_id_is_unmappable():
    assert ebay_search._map_item(_item(item_id="")) is None


def test_unparseable_price_is_unmappable():
    """price is non-optional on Listing, so a listing without one cannot be
    stored. Returning None lets the caller skip it and keep its siblings."""
    assert ebay_search._map_item(_item(price="Best offer only")) is None


def test_missing_optional_fields_still_map():
    listing = ebay_search._map_item(_item(
        shipping_cost="", condition=None, seller_feedback_percent="",
        seller_feedback_count="", image_url="",
    ))
    assert listing is not None
    assert listing.shipping_cost is None
    assert listing.seller_rating is None
    assert listing.seller_feedback_count is None
    assert listing.image_url is None
