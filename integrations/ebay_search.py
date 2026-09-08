"""eBay listing discovery via an Apify actor.

Replaces the eBay Browse API, whose EBAY_APP_ID/EBAY_CERT_ID are
developer-portal credentials an invited friend cannot obtain by signing into
eBay. Discovery is the first node in the search graph, so that credential was
the single thing making a friend's search fail at step one.

Results are filtered to Best-Offer-eligible listings server-side
(`buyingFormat: LH_BO`): every result is negotiable, and the search is cheaper
than fetching listings that can never be acted on.

The actor returns every value as a string, so `_map_item` is doing real parsing.
A listing that cannot be parsed is skipped rather than failing the whole search.
"""

import logging
import re
from typing import Any

from pydantic import BaseModel


log = logging.getLogger(__name__)

ACTOR_ID = "delicious_zebu/ebay-product-listing-scraper"


class EbaySearchError(RuntimeError):
    """Discovery failed. `agents.graph.discover` catches this and marks the
    search failed, so the message reaches the user verbatim — write it for a
    friend, not for a maintainer."""


class Listing(BaseModel):
    """A single eBay listing, shaped to match the columns the DB `listings`
    table expects from a search (search_id / selected_at / outcome are caller
    concerns)."""

    ebay_item_id: str
    title: str
    price: float
    shipping_cost: float | None = None
    condition: str | None = None
    seller_id: str | None = None
    seller_rating: float | None = None
    seller_feedback_count: int | None = None
    url: str
    image_url: str | None = None
    listed_at: str | None = None
    # Kept for interface compatibility with the Browse API this replaced. Every
    # result is Best-Offer filtered server-side, so this is always
    # ["BEST_OFFER"]; api/routes/searches.py branches on it.
    buying_options: list[str] = []
    raw_data: dict[str, Any]


_NUMBER = re.compile(r"[-+]?\d[\d,]*\.?\d*")


def _first_number(raw: Any) -> float | None:
    """Pull the first number out of a noisy string. Returns None when there
    isn't one, which is how every parser below signals 'absent'."""
    if raw is None:
        return None
    match = _NUMBER.search(str(raw))
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_price(raw: Any) -> float | None:
    return _first_number(raw)


def _parse_shipping(raw: Any) -> float | None:
    """"Free" is a price of zero, not a missing value — the distinction matters
    because shipping feeds the effective-price comparison."""
    if raw is None:
        return None
    if "free" in str(raw).lower():
        return 0.0
    return _first_number(raw)


def _parse_percent(raw: Any) -> float | None:
    return _first_number(raw)


def _parse_count(raw: Any) -> int | None:
    value = _first_number(raw)
    return None if value is None else int(value)


def _map_item(item: dict[str, Any]) -> Listing | None:
    """Map one actor item to a Listing, or None when it cannot be stored.

    Only `item_id`, `product_title`, `price` and `product_url` are load-bearing
    — everything else degrades to None. Returning None rather than raising lets
    one malformed listing be skipped without losing its siblings.
    """
    item_id = str(item.get("item_id") or "").strip()
    title = str(item.get("product_title") or "").strip()
    url = str(item.get("product_url") or "").strip()
    price = _parse_price(item.get("price"))

    if not item_id or not title or not url or price is None:
        return None

    return Listing(
        ebay_item_id=item_id,
        title=title,
        price=price,
        shipping_cost=_parse_shipping(item.get("shipping_cost")),
        condition=(str(item["condition"]).strip() or None) if item.get("condition") else None,
        seller_id=(str(item["seller_name"]).strip() or None) if item.get("seller_name") else None,
        seller_rating=_parse_percent(item.get("seller_feedback_percent")),
        seller_feedback_count=_parse_count(item.get("seller_feedback_count")),
        url=url,
        image_url=(str(item["image_url"]).strip() or None) if item.get("image_url") else None,
        listed_at=None,
        buying_options=["BEST_OFFER"],
        raw_data=item,
    )
