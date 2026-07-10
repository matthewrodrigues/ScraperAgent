"""Google Shopping reference-price source via Apify.

Wraps the `burbn/google-shopping-scraper` Apify Actor. The actor returns a
flat list of product offers across the major US retailers — exactly the
cross-merchant snapshot we want for a price anchor.

Pricing note: pay-per-event, ~$0.008 actor-start + tiered per-result. A 25-item
run typically lands under $0.05, well below the $0.50 per-search cap.

Defensive parsing: actor outputs evolve, so we extract only the fields we care
about and ignore the rest. Items missing a `price` are dropped (no way to use
them in aggregation).
"""

import logging
import re
from datetime import timedelta
from decimal import Decimal
from typing import Any

from apify_client import ApifyClient

import config
from agents.criteria_parser import ParsedCriteria


log = logging.getLogger(__name__)

_ACTOR_ID = "burbn/google-shopping-scraper"
_DEFAULT_RUN_TIMEOUT_SECS = 90
# Pay-per-event start cost from the actor's pricing schema. Used for the pre-flight
# budget check; actual cost is read back from the run's usageUsd field.
_PLANNED_COST_USD = 0.10  # generous; actual is usually ~$0.02-0.05

_PRICE_RE = re.compile(r"[\d]+(?:[.,][\d]+)?")


class PricingSourceError(RuntimeError):
    """Raised when the Apify actor fails to start, errors out, or returns no data."""


_client: ApifyClient | None = None


def _get_client() -> ApifyClient:
    global _client
    if _client is None:
        if not config.APIFY_TOKEN:
            raise PricingSourceError("APIFY_TOKEN is not set in .env")
        _client = ApifyClient(config.APIFY_TOKEN)
    return _client


def fetch(criteria: ParsedCriteria) -> tuple[list[dict[str, Any]], float]:
    """Run the actor and return (price_points, cost_usd).

    `price_points` is a list of dicts: {price, currency, condition, url, title, source_item}.
    Caller is responsible for bucketing/aggregation.
    """
    client = _get_client()

    run_input: dict[str, Any] = {
        "searchQuery": criteria.title_keywords or "",
        "country": "us",
        "language": "en",
        "limit": 25,
    }
    if criteria.max_price is not None:
        run_input["maxPrice"] = criteria.max_price

    try:
        run = client.actor(_ACTOR_ID).call(
            run_input=run_input,
            wait_duration=timedelta(seconds=_DEFAULT_RUN_TIMEOUT_SECS),
            # Platform-side cap as a redundant safety net on top of our cost_guard.
            # SDK rejects the call before launch if estimated cost exceeds this.
            max_total_charge_usd=Decimal(str(config.APIFY_BUDGET_USD)),
        )
    except Exception as exc:
        # apify-client raises various exceptions for network/auth/timeouts; collapse
        # them into our domain error so the graph node only catches one type.
        raise PricingSourceError(f"actor call failed: {exc}") from exc

    if run is None:
        raise PricingSourceError("actor returned no run")
    # apify-client 3.x returns a Pydantic Run model; older mocks return dicts.
    # Normalize both to a dict so downstream code only handles one shape.
    run_d = run.model_dump() if hasattr(run, "model_dump") else run
    if (run_d.get("status") or "").upper() != "SUCCEEDED":
        raise PricingSourceError(f"actor run did not succeed: status={run_d.get('status')}")

    # The SDK exposes both snake_case (real models) and camelCase (some dict mocks).
    dataset_id = run_d.get("default_dataset_id") or run_d.get("defaultDatasetId")
    if not dataset_id:
        raise PricingSourceError(f"run had no default_dataset_id: {run_d}")

    items = list(client.dataset(dataset_id).iterate_items())
    points = _parse_items(items)
    cost_usd = float(run_d.get("usage_total_usd") or run_d.get("usageUsd") or 0.0)

    log.info(
        "google_shopping: %d items parsed (from %d raw) at cost $%.4f",
        len(points), len(items), cost_usd,
    )
    return points, cost_usd


def _parse_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        price = _extract_price(item)
        if price is None:
            continue
        # The burbn actor uses snake_case product_* fields; older builds + our
        # test fixtures use shorter names. Check both so a future actor update
        # doesn't silently zero out our titles/store names again.
        title = item.get("product_title") or item.get("productTitle") or item.get("title") or ""
        store = item.get("store_name") or item.get("store") or item.get("merchant") or ""
        url = (
            item.get("product_url") or item.get("productUrl")
            or item.get("url") or item.get("link") or ""
        )
        out.append({
            "price": price,
            "currency": item.get("currency") or "USD",
            "condition": (item.get("condition") or item.get("productCondition") or "new").lower(),
            "url": url,
            "title": title,
            "store": store,
            "source_item": item,
        })
    return out


def _extract_price(item: dict[str, Any]) -> float | None:
    """Find a numeric price in an actor item.

    Different actor builds put the price in different fields (`price`, `priceValue`,
    or a string like '$278.99'). Try the most likely fields, then regex-extract
    digits from string values as a last resort.
    """
    for key in ("priceValue", "price", "currentPrice", "salePrice"):
        v = item.get(key)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            m = _PRICE_RE.search(v.replace(",", ""))
            if m:
                try:
                    return float(m.group(0))
                except ValueError:
                    continue
    return None
