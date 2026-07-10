"""eBay Browse API search.

Fetches live item listings matching a ParsedCriteria. The Browse API only
exposes server-side filters for price, condition, and seller account type;
seller-rating thresholds (the buyer-supplied trust floor) are enforced
client-side after the response comes back.

Authentication uses a client-credentials OAuth token (App ID + Cert ID),
which is distinct from the user OAuth token used for buyer-scoped endpoints
like Negotiation. Tokens last ~2h; we cache them in-process with a 60s
safety margin and refresh on next call after expiry.
"""

import time
from typing import Any

import requests
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config
from agents.criteria_parser import ParsedCriteria


_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_SCOPE = "https://api.ebay.com/oauth/api_scope"

# Retries cover transient RemoteDisconnected from stale keep-alive sockets and
# brief 5xx blips from eBay's edge. urllib3 treats connection-reset as a
# connect-error, so `connect=` (not `status=`) is what catches it.
_session = requests.Session()
_session.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=3,
            connect=3,
            read=2,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
    ),
)
_REQUEST_TIMEOUT = 15

# Fraction of max_price below which a listing is almost certainly an accessory
# or replacement part, not the actual item. A $5 charger for a $300 product is
# noise; this drops them client-side after the search returns.
_PRICE_FLOOR_FRACTION = 0.10

# eBay condition IDs, ordered loosest-to-strictest. A "floor" includes itself
# and everything stricter (i.e. better-condition) — picking "used" admits new
# and refurbished too, not just used.
# Reference: https://developer.ebay.com/devzone/finding/callref/enums/conditionIdList.html
_CONDITION_TIERS = [
    ("new", ["1000"]),
    ("refurbished", ["2000", "2010", "2020", "2030"]),
    ("used", ["3000", "4000", "5000", "6000"]),
]

_token: str | None = None
_token_expires_at: float | None = None


class EbaySearchError(RuntimeError):
    """Raised when eBay returns a non-200 for a token fetch or search request."""


class Listing(BaseModel):
    """A single eBay listing, shaped to match the columns the DB `listings` table
    expects from a search (search_id / selected_at / outcome are caller concerns)."""

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
    # eBay Browse exposes "FIXED_PRICE" / "AUCTION" / "BEST_OFFER" tokens per
    # listing. Threaded through so the send-route can branch on BO availability
    # without re-parsing raw_data on every render.
    buying_options: list[str] = []
    raw_data: dict[str, Any]


def _get_oauth_token() -> str:
    global _token, _token_expires_at
    now = time.time()
    if _token and _token_expires_at and now < _token_expires_at:
        return _token

    resp = _session.post(
        _TOKEN_URL,
        auth=(config.EBAY_APP_ID, config.EBAY_CERT_ID),
        data={"grant_type": "client_credentials", "scope": _SCOPE},
        timeout=_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise EbaySearchError(f"OAuth token fetch failed: {resp.status_code} {resp.text}")

    body = resp.json()
    _token = body["access_token"]
    # 60s safety margin so we refresh just before eBay would reject the token.
    _token_expires_at = now + body["expires_in"] - 60
    return _token


def _condition_ids_for_floor(floor: str | None) -> list[str] | None:
    if floor in (None, "any"):
        return None
    allowed: list[str] = []
    for tier_name, ids in _CONDITION_TIERS:
        allowed.extend(ids)
        if tier_name == floor:
            return allowed
    return None


def _build_query(criteria: ParsedCriteria) -> str:
    parts = [criteria.title_keywords] if criteria.title_keywords else []
    parts.extend(f"-{kw}" for kw in criteria.must_not_keywords)
    return " ".join(parts)


def _build_filter(criteria: ParsedCriteria) -> str | None:
    clauses: list[str] = []
    if criteria.max_price is not None:
        # Format max as int when it has no fractional part, to match what eBay
        # accepts ergonomically; both forms work, but integers are cleaner in logs.
        max_str = str(int(criteria.max_price)) if criteria.max_price == int(criteria.max_price) else str(criteria.max_price)
        clauses.append(f"price:[..{max_str}],priceCurrency:USD")
    cond_ids = _condition_ids_for_floor(criteria.condition_floor)
    if cond_ids:
        clauses.append("conditionIds:{" + "|".join(cond_ids) + "}")
    return ",".join(clauses) if clauses else None


def _map_item(item: dict) -> Listing:
    price = float(item["price"]["value"])

    shipping_cost = None
    shipping_options = item.get("shippingOptions") or []
    if shipping_options:
        cost = shipping_options[0].get("shippingCost", {}).get("value")
        if cost is not None:
            shipping_cost = float(cost)

    seller = item.get("seller") or {}
    seller_rating = float(seller["feedbackPercentage"]) if "feedbackPercentage" in seller else None
    seller_feedback_count = int(seller["feedbackScore"]) if "feedbackScore" in seller else None

    return Listing(
        ebay_item_id=item["itemId"],
        title=item["title"],
        price=price,
        shipping_cost=shipping_cost,
        condition=item.get("condition"),
        seller_id=seller.get("username"),
        seller_rating=seller_rating,
        seller_feedback_count=seller_feedback_count,
        url=item["itemWebUrl"],
        image_url=(item.get("image") or {}).get("imageUrl"),
        listed_at=item.get("itemCreationDate"),
        buying_options=list(item.get("buyingOptions") or []),
        raw_data=item,
    )


def search_ebay(criteria: ParsedCriteria, limit: int = 25) -> list[Listing]:
    token = _get_oauth_token()

    # Omit `sort` to get eBay's default `bestMatch` ranking — much better than
    # price-asc for surfacing the actual product instead of cheap accessories
    # that happen to mention the product name.
    params: dict[str, Any] = {
        "q": _build_query(criteria),
        "limit": limit,
    }
    filter_str = _build_filter(criteria)
    if filter_str:
        params["filter"] = filter_str

    resp = _session.get(
        _SEARCH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params=params,
        timeout=_REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        raise EbaySearchError(f"Browse search failed: {resp.status_code} {resp.text}")

    items = resp.json().get("itemSummaries") or []
    listings = [_map_item(item) for item in items]

    if criteria.min_seller_rating is not None:
        threshold = criteria.min_seller_rating
        listings = [l for l in listings if l.seller_rating is not None and l.seller_rating >= threshold]

    if criteria.max_price is not None:
        floor = criteria.max_price * _PRICE_FLOOR_FRACTION
        listings = [l for l in listings if l.price >= floor]

    return listings
