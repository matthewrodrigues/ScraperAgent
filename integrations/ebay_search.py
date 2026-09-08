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
from datetime import timedelta
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient
from pydantic import BaseModel

import config
from agents.criteria_parser import ParsedCriteria
from integrations import clients


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


_DEFAULT_RUN_TIMEOUT_SECS = 120

WARNING_NO_SELLER_FEEDBACK = (
    "Seller feedback could not be retrieved for these listings, so your minimum "
    "seller rating filter was not applied. Check each seller's feedback on eBay "
    "before making an offer."
)

_client: ApifyClient | None = None


def _get_client() -> ApifyClient:
    global _client
    if _client is None:
        if not clients.apify_configured():
            raise EbaySearchError(
                "No Apify credentials. Set APIFY_TOKEN, or SCRAPERAGENT_BROKER_URL "
                "and SCRAPERAGENT_BROKER_TOKEN to use a shared broker."
            )
        _client = ApifyClient(**clients.apify_kwargs())
    return _client


class SearchResult(BaseModel):
    """What discovery produced: the listings, what the run cost, and any
    non-fatal degradation the user should be told about."""

    listings: list[Listing]
    cost_usd: float
    warning: str | None = None


def _build_actor_input(criteria: ParsedCriteria) -> dict[str, Any]:
    actor_input: dict[str, Any] = {
        "keywords": [criteria.title_keywords or ""],
        "ebaySite": "www.ebay.com",
        # Best Offer only. Every result is negotiable, and we don't pay for
        # listings the product can never act on.
        "buyingFormat": "LH_BO",
        "sortBy": "12",       # Best Match, matching the Browse API default
        "maxPages": 1,        # a page is up to 240 items, well above any limit
    }
    if criteria.max_price is not None:
        actor_input["maxPrice"] = int(criteria.max_price)
    if criteria.must_not_keywords:
        actor_input["excludeKeywords"] = " ".join(criteria.must_not_keywords)
    # condition_floor is a FLOOR: "used" means used-or-better and must still
    # admit new listings, so only "new" narrows the search.
    if criteria.condition_floor == "new":
        actor_input["condition"] = "1000"
    return actor_input


def search_ebay(criteria: ParsedCriteria, limit: int = 25) -> SearchResult:
    """Discover Best-Offer-eligible listings matching `criteria`.

    Raises EbaySearchError on failure; the message reaches the user verbatim.
    An empty result set is NOT an error — the caller decides how to present it.
    """
    client = _get_client()

    try:
        run = client.actor(ACTOR_ID).call(
            run_input=_build_actor_input(criteria),
            wait_duration=timedelta(seconds=_DEFAULT_RUN_TIMEOUT_SECS),
            max_total_charge_usd=Decimal(str(config.EBAY_SEARCH_BUDGET_USD)),
            # maxPages=1 in the actor input still admits up to 240 items —
            # well above `limit` and above what EBAY_SEARCH_BUDGET_USD covers
            # at $0.002/result. max_items stops the vendor at `limit` results;
            # the client-side [:limit] slice below stays as a backstop since
            # the two enforce at different layers.
            max_items=limit,
        )

        if run is None:
            raise EbaySearchError("eBay search returned no run.")
        run_d = run.model_dump() if hasattr(run, "model_dump") else run
        if (run_d.get("status") or "").upper() != "SUCCEEDED":
            raise EbaySearchError(f"eBay search did not complete (status={run_d.get('status')}).")

        # usage_total_usd is the field name on apify-client's Pydantic Run
        # model (3.x); usageTotalUsd is the raw camelCase key some older
        # mocks/dicts use. Keep this fallback order in sync with the
        # equivalent lookup in pricing/google_shopping.py.
        cost_usd = float(
            run_d.get("usage_total_usd") or run_d.get("usageTotalUsd") or 0.0
        )
        dataset_id = run_d.get("default_dataset_id") or run_d.get("defaultDatasetId")
        # Fetched inside this try: the run has already been charged by this
        # point, so a network failure here must still become an
        # EbaySearchError (via _explain below) rather than a raw client
        # exception escaping uncaught into the background task.
        raw_items = list(client.dataset(dataset_id).iterate_items()) if dataset_id else []
    except EbaySearchError:
        raise
    except Exception as exc:
        raise EbaySearchError(_explain(exc)) from exc

    listings: list[Listing] = []
    for item in raw_items:
        mapped = _map_item(item)
        if mapped is None:
            log.warning("ebay_search: skipping unmappable item %r", item.get("item_id"))
            continue
        listings.append(mapped)

    listings, warning = _apply_seller_filter(listings, criteria)
    return SearchResult(listings=listings[:limit], cost_usd=cost_usd, warning=warning)


def _apply_seller_filter(
    listings: list[Listing], criteria: ParsedCriteria
) -> tuple[list[Listing], str | None]:
    """Apply the client-side seller-rating floor, unless ratings are missing
    wholesale.

    One listing without feedback is ordinary — a brand new seller. Every
    listing without it means the actor's `seller_feedback_percent` field has
    moved. Applying the filter then would drop every listing (it excludes
    None), and the user would be told their criteria matched nothing — sending
    them to widen filters that were never the problem. So we keep the listings,
    skip the filter, and hand the caller a warning to show.

    This heuristic needs enough listings to be meaningful: a sample of one or
    two "no feedback" listings cannot distinguish "genuinely new sellers" from
    "the actor renamed the field" — with a small result set, all-missing is
    plausible by chance and shouldn't override the user's explicit filter. So
    require at least 3 listings before treating an all-missing run as a
    broken actor contract; below that, apply the filter normally.
    """
    if criteria.min_seller_rating is None or not listings:
        return listings, None

    if len(listings) >= 3 and all(l.seller_rating is None for l in listings):
        log.error(
            "ebay_search: no listing carried a seller feedback percentage; "
            "the actor's output shape may have changed. Rating filter skipped."
        )
        return listings, WARNING_NO_SELLER_FEEDBACK

    threshold = criteria.min_seller_rating
    return [
        l for l in listings if l.seller_rating is not None and l.seller_rating >= threshold
    ], None


def _explain(exc: Exception) -> str:
    """Turn a client exception into something a friend can act on.

    These messages are shown verbatim on a failed search, so they name the
    remedy rather than the stack frame.
    """
    status = getattr(exc, "status_code", None)
    if status == 402:
        return (
            "Your monthly search budget with this broker is used up. Ask the "
            "broker's owner to raise it, or set your own APIFY_TOKEN to pay "
            "for your own usage."
        )
    if status == 503:
        return (
            "The shared broker is not available right now. Try again later, or "
            "set your own APIFY_TOKEN and unset SCRAPERAGENT_BROKER_URL to run "
            "searches on your own account."
        )
    return f"eBay search failed: {exc}"
