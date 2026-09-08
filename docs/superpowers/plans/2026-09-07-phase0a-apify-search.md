# Phase 0a — Apify Listing Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the eBay Browse API with an Apify actor so listing discovery works without eBay developer credentials — the single thing that currently makes an invited friend's search fail at step one.

**Architecture:** A new `integrations/ebay_search.py` mirrors the existing Apify pattern in `pricing/google_shopping.py` and keeps `search_ebay(criteria, limit) -> list[Listing]` byte-identical in signature, so the `discover` node changes by one import. `browser/` is deleted. Discovery now costs money, so the search's spend is recorded on the `searches` row and the per-search budget becomes a vendor-enforced cap rather than an estimate.

**Tech Stack:** Python 3.12, `apify-client` 3.0.2, Pydantic, SQLite, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-07-phase0a-apify-search-design.md`

## Global Constraints

- **Actor:** `delicious_zebu/ebay-product-listing-scraper`, id `8bXnzCF4JVgMMA5cM`, $0.002 per result.
- **`EBAY_SEARCH_BUDGET_USD = 0.15`** — discovery's own ceiling. It must NOT reuse `APIFY_BUDGET_USD`: the broker debits friends the declared ceiling provisionally, so an over-declared ceiling is a real charge against their budget.
- **`APIFY_BUDGET_USD` default 0.50 → 0.90.**
- **Reference pricing receives the *remaining* per-search budget** as `max_total_charge_usd`, never the full budget. This is what makes `APIFY_BUDGET_USD` a hard per-search cap.
- **`buyingFormat: "LH_BO"`** is always in the actor input — every result is Best-Offer eligible.
- **Condition is a floor:** `condition_floor == "new"` → `condition: "1000"`. `"used"` or `None` → **omit** the condition filter entirely. Sending `3000` for `"used"` would wrongly exclude new listings.
- **`EBAY_USER_TOKEN`, `integrations/ebay_trading.py`, `api/routes/ebay_notifications.py`, and the `/ebay/account-deletion` entry in `EXEMPT_PATHS` all STAY.** Phase 0b removes them; this plan does not.
- All existing tests must pass. The suite is **405** before Task 1.
- No new entries in `requirements.txt` or `pyproject.toml`.
- Tests never touch the network. The Apify client is mocked at module level, as in `tests/test_google_shopping.py`.
- Interpreter is `.venv/Scripts/python.exe` run from the repo root. Windows; Git Bash available.

## File Structure

| File | Responsibility |
|---|---|
| `integrations/ebay_search.py` | **New.** Apify-backed listing discovery: `Listing`, `EbaySearchError`, `search_ebay()`, actor input building, output mapping |
| `browser/ebay.py`, `browser/__init__.py` | **Deleted.** Name means eBay's *Browse API* and collides with `integrations/ebay_browser.py`, the actual Playwright browser |
| `config.py` | `EBAY_SEARCH_BUDGET_USD` added; `APIFY_BUDGET_USD` raised; `EBAY_APP_ID`/`EBAY_CERT_ID` removed |
| `db/repo.py` | Two column migrations (`searches.search_cost_usd`, `searches.warning_message`), setters, and `sum_total_cost` gaining a third component |
| `agents/graph.py` | Import swap; `discover` records cost and handles the zero-results and degraded-feedback cases; `reference_prices` passes the remaining budget |
| `pricing/google_shopping.py` | `max_total_charge_usd` becomes the remaining budget rather than the full one |
| `templates/search_detail.html` | Renders `warning_message` as a banner |
| `scratch_ebay_search.py` | Rewritten as the live smoke test against the real actor |

---

### Task 1: Config and schema groundwork

Everything downstream needs these constants and columns. Isolated and independently testable, so it goes first.

**Files:**
- Modify: `config.py`
- Modify: `db/repo.py`
- Test: `tests/test_config.py`, `tests/test_repo.py`

**Interfaces:**
- Consumes: `config.DATA_DIR`, the existing `_COLUMN_MIGRATIONS` list
- Produces: `config.EBAY_SEARCH_BUDGET_USD` (float, 0.15), `config.APIFY_BUDGET_USD` (float, 0.90 default), `repo.set_search_cost(search_id: int, cost_usd: float) -> None`, `repo.set_search_warning(search_id: int, message: str) -> None`, `repo.sum_total_cost(search_id) -> dict[str, float]` now returning keys `{"search", "apify", "claude", "total"}`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
def test_ebay_search_budget_default():
    assert config.EBAY_SEARCH_BUDGET_USD == 0.15


def test_apify_budget_default_covers_discovery_plus_pricing():
    # Google Shopping costs ~$0.49 and discovery ~$0.05; the cap must clear both
    # or cost_guard blocks reference pricing on every search.
    assert config.APIFY_BUDGET_USD >= 0.60
```

Add to `tests/test_repo.py`:

```python
def test_set_search_cost_and_read_back(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_cost(search_id, 0.0512)
    assert repo.get_search(search_id)["search_cost_usd"] == pytest.approx(0.0512)


def test_search_cost_defaults_to_zero(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    assert repo.get_search(search_id)["search_cost_usd"] == 0.0


def test_set_search_warning_and_read_back(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_warning(search_id, "Seller feedback unavailable.")
    assert repo.get_search(search_id)["warning_message"] == "Seller feedback unavailable."


def test_warning_message_defaults_to_none(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    assert repo.get_search(search_id)["warning_message"] is None


def test_migration_adds_columns_to_a_preexisting_database(tmp_path, monkeypatch):
    """The tmp_db fixture builds a fresh schema, so it never exercises the
    ALTER TABLE path. A real user's broker.db predates these columns."""
    import sqlite3
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE searches ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " criteria_nl TEXT NOT NULL,"
        " criteria_structured_json TEXT NOT NULL,"
        " max_price REAL NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    conn.close()

    repo.init_db()

    with repo.get_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(searches)").fetchall()}
    assert "search_cost_usd" in cols
    assert "warning_message" in cols


def test_sum_total_cost_includes_search_cost(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_cost(search_id, 0.05)
    repo.add_reference_price(
        search_id, source="google_shopping", raw_data={}, median=100.0,
        p25=90.0, p75=110.0, condition="used", cost_usd=0.49,
    )
    totals = repo.sum_total_cost(search_id)
    assert totals["search"] == pytest.approx(0.05)
    assert totals["apify"] == pytest.approx(0.49)
    assert totals["total"] == pytest.approx(0.54)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_repo.py -q`
Expected: FAIL — `AttributeError: module 'config' has no attribute 'EBAY_SEARCH_BUDGET_USD'` and `AttributeError: module 'db.repo' has no attribute 'set_search_cost'`

- [ ] **Step 3: Update config**

In `config.py`, change the `APIFY_BUDGET_USD` default and add the discovery budget beneath it:

```python
APIFY_BUDGET_USD = float(os.getenv("APIFY_BUDGET_USD", "0.90"))

# Discovery's own ceiling, deliberately separate from APIFY_BUDGET_USD above.
# 25 results at $0.002 is ~$0.05; 0.15 leaves 3x headroom. It must not reuse the
# reference-pricing budget: the key broker debits a friend the *declared*
# ceiling provisionally, so an over-declared ceiling is a real charge against
# their monthly budget until reconciliation settles it.
EBAY_SEARCH_BUDGET_USD = float(os.getenv("EBAY_SEARCH_BUDGET_USD", "0.15"))
```

Delete the `EBAY_APP_ID` and `EBAY_CERT_ID` assignments and their comments. Leave `EBAY_USER_TOKEN` alone — the Trading API still needs it until Phase 0b.

- [ ] **Step 4: Add the column migrations**

Append to `_COLUMN_MIGRATIONS` in `db/repo.py` (the list is ordered oldest-first, so these go at the end):

```python
    # Discovery moved from the free Browse API to a paid Apify actor, so a
    # search now has a cost of its own that is not a reference price.
    ("searches", "search_cost_usd", "REAL NOT NULL DEFAULT 0"),
    # Non-fatal degradation notice shown as a banner. Distinct from
    # error_message, which means the search failed.
    ("searches", "warning_message", "TEXT"),
```

- [ ] **Step 5: Add the setters and extend the cost summary**

In `db/repo.py`:

```python
def set_search_cost(search_id: int, cost_usd: float) -> None:
    """Record what discovery cost for this search."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE searches SET search_cost_usd = ? WHERE id = ?",
            (round(cost_usd, 6), search_id),
        )


def set_search_warning(search_id: int, message: str) -> None:
    """Attach a non-fatal notice to a search. Rendered as a banner; unlike
    error_message it does not mean the search failed."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE searches SET warning_message = ? WHERE id = ?",
            (message, search_id),
        )


def sum_search_cost(search_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(search_cost_usd, 0.0) AS c FROM searches WHERE id = ?",
            (search_id,),
        ).fetchone()
        return float(row["c"]) if row else 0.0
```

Then extend `sum_total_cost` to report discovery as its own component:

```python
def sum_total_cost(search_id: int) -> dict[str, float]:
    """Cost breakdown for a search: discovery + Apify pricing + Claude drafting.

    Returns `{"search": S, "apify": A, "claude": C, "total": S+A+C}` rounded to
    4 decimals. Single source of truth for the dashboard cost line."""
    search = round(sum_search_cost(search_id), 4)
    apify = round(sum_apify_cost(search_id), 4)
    claude = round(sum_claude_cost(search_id), 4)
    return {
        "search": search,
        "apify": apify,
        "claude": claude,
        "total": round(search + apify + claude, 4),
    }
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_config.py tests/test_repo.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS. Any test asserting `sum_total_cost` has exactly three keys must be updated to expect four — that is a real interface change, not a break. Do not delete such a test; extend it.

- [ ] **Step 8: Commit**

```bash
git add config.py db/repo.py tests/test_config.py tests/test_repo.py
git commit -m "feat(search): budget constants and per-search cost/warning columns"
```

---

### Task 2: Actor payload parsing and mapping

The actor returns every value as a string, so this is parsing, not renaming. These are pure functions with no I/O, which makes them the cheapest place to get the fiddly cases right before any network shape is involved.

**Files:**
- Create: `integrations/ebay_search.py`
- Test: `tests/test_ebay_search_mapping.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces: `integrations.ebay_search.Listing` (Pydantic model, fields exactly as the old `browser.ebay.Listing`), `EbaySearchError`, `_parse_price(raw) -> float | None`, `_parse_shipping(raw) -> float | None`, `_parse_percent(raw) -> float | None`, `_parse_count(raw) -> int | None`, `_map_item(item: dict) -> Listing | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_ebay_search_mapping.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ebay_search_mapping.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'integrations.ebay_search'`

- [ ] **Step 3: Write the module skeleton and parsers**

Create `integrations/ebay_search.py`:

```python
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
```

- [ ] **Step 4: Write the mapper**

Append to `integrations/ebay_search.py`:

```python
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ebay_search_mapping.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add integrations/ebay_search.py tests/test_ebay_search_mapping.py
git commit -m "feat(search): Apify actor payload parsing and Listing mapping"
```

---

### Task 3: `search_ebay` — actor call, filtering, degraded feedback, errors

> **Deviation from the spec, deliberate.** Spec §3 says the signature is preserved as `search_ebay(...) -> list[Listing]`. That is not achievable: §6 requires the caller to record the run's cost and §5.1 requires it to record a warning, and neither can be returned through a bare list. This task returns a `SearchResult` carrying all three, mirroring `pricing/google_shopping.fetch()` which already returns `tuple[list[dict], float]` for exactly this reason. `Listing` and `EbaySearchError` are unchanged, so nothing else about the contract moves.

**Files:**
- Modify: `integrations/ebay_search.py`
- Test: `tests/test_ebay_search.py` (rewrite — the existing Browse API tests in this file are replaced, not deleted)

**Interfaces:**
- Consumes: `config.EBAY_SEARCH_BUDGET_USD`, `integrations.clients.apify_kwargs()`, `agents.criteria_parser.ParsedCriteria`, and from Task 2: `Listing`, `EbaySearchError`, `_map_item`
- Produces: `SearchResult` (Pydantic model: `listings: list[Listing]`, `cost_usd: float`, `warning: str | None`), `search_ebay(criteria: ParsedCriteria, limit: int = 25) -> SearchResult`, `_build_actor_input(criteria: ParsedCriteria) -> dict`, `WARNING_NO_SELLER_FEEDBACK` (str constant)

- [ ] **Step 1: Write the failing test**

Replace the entire contents of `tests/test_ebay_search.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ebay_search.py -q`
Expected: FAIL — `AttributeError: module 'integrations.ebay_search' has no attribute '_build_actor_input'`

- [ ] **Step 3: Add the client, input builder, and result model**

Append to `integrations/ebay_search.py` (and add `from decimal import Decimal`, `from datetime import timedelta`, `from apify_client import ApifyClient`, `import config`, `from agents.criteria_parser import ParsedCriteria`, `from integrations import clients` to the imports):

```python
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
```

- [ ] **Step 4: Write `search_ebay`**

Append to `integrations/ebay_search.py`:

```python
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
        )
    except Exception as exc:
        raise EbaySearchError(_explain(exc)) from exc

    if run is None:
        raise EbaySearchError("eBay search returned no run.")
    run_d = run.model_dump() if hasattr(run, "model_dump") else run
    if (run_d.get("status") or "").upper() != "SUCCEEDED":
        raise EbaySearchError(f"eBay search did not complete (status={run_d.get('status')}).")

    cost_usd = float(
        run_d.get("usage_total_usd") or run_d.get("usageTotalUsd") or 0.0
    )
    dataset_id = run_d.get("default_dataset_id") or run_d.get("defaultDatasetId")
    raw_items = list(client.dataset(dataset_id).iterate_items()) if dataset_id else []

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
    """
    if criteria.min_seller_rating is None or not listings:
        return listings, None

    if all(l.seller_rating is None for l in listings):
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
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_ebay_search.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS. `browser/ebay.py` still exists and is still imported by `agents/graph.py` at this point — that is expected; Task 4 removes it.

- [ ] **Step 7: Commit**

```bash
git add integrations/ebay_search.py tests/test_ebay_search.py
git commit -m "feat(search): Apify-backed search_ebay with degraded-feedback handling"
```

---

### Task 4: Wire discovery into the graph and delete `browser/`

**Files:**
- Modify: `agents/graph.py` (import at line 20; the `discover` node)
- Delete: `browser/ebay.py`, `browser/__init__.py`, and the `browser/` directory
- Test: `tests/test_graph.py`

**Interfaces:**
- Consumes: from Task 3 `search_ebay(criteria, limit) -> SearchResult`, `EbaySearchError`, `WARNING_NO_SELLER_FEEDBACK`; from Task 1 `repo.set_search_cost`, `repo.set_search_warning`
- Produces: a `discover` node that records cost, surfaces warnings, and ends a zero-result search with an explanatory message

- [ ] **Step 1: Write the failing test**

Add to `tests/test_graph.py`:

```python
from integrations import ebay_search as _ebay_search_mod


def _result(listings, cost_usd=0.05, warning=None):
    return _ebay_search_mod.SearchResult(
        listings=listings, cost_usd=cost_usd, warning=warning
    )


def _listing(item_id="1", price=200.0, rating=99.5):
    return _ebay_search_mod.Listing(
        ebay_item_id=item_id, title=f"Listing {item_id}", price=price,
        url=f"https://www.ebay.com/itm/{item_id}", seller_rating=rating,
        buying_options=["BEST_OFFER"], raw_data={},
    )


def test_discover_records_the_run_cost(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    with patch("agents.graph.search_ebay", return_value=_result([_listing()], cost_usd=0.052)):
        graph.discover({"search_id": search_id})
    assert repo.get_search(search_id)["search_cost_usd"] == pytest.approx(0.052)


def test_discover_stores_listings(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    with patch("agents.graph.search_ebay", return_value=_result([_listing("1"), _listing("2")])):
        graph.discover({"search_id": search_id})
    assert len(repo.list_listings(search_id)) == 2


def test_discover_surfaces_the_degraded_feedback_warning(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    warning = _ebay_search_mod.WARNING_NO_SELLER_FEEDBACK
    with patch("agents.graph.search_ebay", return_value=_result([_listing()], warning=warning)):
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph.py -q`
Expected: FAIL — `AttributeError: <module 'agents.graph'> does not have the attribute 'search_ebay'` resolving to the old import, or a `search_cost_usd` KeyError

- [ ] **Step 3: Swap the import**

In `agents/graph.py`, replace line 20:

```python
from integrations.ebay_search import EbaySearchError, search_ebay
```

- [ ] **Step 4: Rewrite the `discover` body**

Replace the `try`/`except` block and what follows it in `discover` with:

```python
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
```

- [ ] **Step 5: Delete the `browser/` package**

```bash
git rm -r browser/
```

Then confirm nothing still imports it:

```bash
grep -rn "from browser\|import browser" --include=*.py . | grep -v __pycache__
```

Expected: no output. If `scratch_ebay_search.py` appears, leave it for now — Task 6 rewrites it.

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_graph.py -q`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add agents/graph.py tests/test_graph.py
git commit -m "feat(search): route discovery through Apify and delete browser/"
```

---

### Task 5: Make `APIFY_BUDGET_USD` a real per-search cap

Discovery now spends before the guard runs, and the guard's budget was never enforced as a total. Spec §6.1.

**Files:**
- Modify: `pricing/cost_guard.py`
- Modify: `pricing/google_shopping.py` (the `max_total_charge_usd` argument)
- Modify: `agents/graph.py` (`_REF_PRICES_PLANNED_COST_USD` and the `reference_prices` node)
- Test: `tests/test_cost_guard.py`, `tests/test_google_shopping.py`

**Interfaces:**
- Consumes: from Task 1 `repo.sum_search_cost`, `config.APIFY_BUDGET_USD`
- Produces: `cost_guard.spent_so_far(search_id) -> float`, `cost_guard.remaining_budget(search_id) -> float`, and `google_shopping.fetch(criteria, max_charge_usd: float)` taking an explicit ceiling

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cost_guard.py`:

```python
def test_spent_so_far_includes_discovery(tmp_db):
    search_id = repo.create_search("x", {"title_keywords": "x"}, 100.0)
    repo.set_search_cost(search_id, 0.05)
    repo.add_reference_price(
        search_id, source="google_shopping", raw_data={}, median=10.0,
        p25=9.0, p75=11.0, condition="used", cost_usd=0.49,
    )
    assert cost_guard.spent_so_far(search_id) == pytest.approx(0.54)


def test_remaining_budget_subtracts_discovery(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.90)
    search_id = repo.create_search("x", {"title_keywords": "x"}, 100.0)
    repo.set_search_cost(search_id, 0.05)
    assert cost_guard.remaining_budget(search_id) == pytest.approx(0.85)


def test_remaining_budget_never_negative(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.10)
    search_id = repo.create_search("x", {"title_keywords": "x"}, 100.0)
    repo.set_search_cost(search_id, 0.50)
    assert cost_guard.remaining_budget(search_id) == 0.0


def test_under_budget_counts_discovery_spend(tmp_db, monkeypatch):
    """Regression guard: before this, discovery spend was invisible to the
    guard, so a search could authorise pricing it could not afford."""
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.50)
    search_id = repo.create_search("x", {"title_keywords": "x"}, 100.0)
    repo.set_search_cost(search_id, 0.30)
    assert cost_guard.under_budget(search_id, 0.30) is False
```

Add to `tests/test_google_shopping.py`:

```python
def test_ceiling_is_the_caller_supplied_remaining_budget(configured):
    """Regression guard for spec 6.1: passing the FULL budget as the ceiling
    let a search exceed the cap it had just been checked against."""
    fake_client = _mock_client(_make_run(), [])
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        google_shopping.fetch(
            ParsedCriteria(title_keywords="headphones", max_price=300.0),
            max_charge_usd=0.31,
        )
    kwargs = fake_client.actor.return_value.call.call_args.kwargs
    assert float(kwargs["max_total_charge_usd"]) == pytest.approx(0.31)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cost_guard.py tests/test_google_shopping.py -q`
Expected: FAIL — `AttributeError: module 'pricing.cost_guard' has no attribute 'spent_so_far'`

- [ ] **Step 3: Extend the cost guard**

Replace the body of `pricing/cost_guard.py` below its docstring:

```python
import config
from db import repo


def spent_so_far(search_id: int) -> float:
    """Every Apify dollar this search has already committed — discovery plus
    reference pricing. Discovery used to be free (Browse API), so it was not
    counted; it is now the first thing a search spends."""
    return repo.sum_search_cost(search_id) + repo.sum_apify_cost(search_id)


def remaining_budget(search_id: int) -> float:
    """What is left of APIFY_BUDGET_USD for this search, floored at zero.

    Callers pass this to Apify as the run's `max_total_charge_usd`, which is
    what makes the budget a cap the vendor enforces rather than an estimate we
    check and then exceed."""
    return max(0.0, config.APIFY_BUDGET_USD - spent_so_far(search_id))


def under_budget(search_id: int, planned_cost_usd: float) -> bool:
    """True if launching a `planned_cost_usd` call would stay under the cap."""
    return spent_so_far(search_id) + planned_cost_usd <= config.APIFY_BUDGET_USD
```

- [ ] **Step 4: Make the pricing ceiling explicit**

In `pricing/google_shopping.py`, change `fetch` to require the ceiling from its caller rather than reading the global budget:

```python
def fetch(criteria: ParsedCriteria, max_charge_usd: float) -> tuple[list[dict[str, Any]], float]:
```

and inside the actor call replace the `max_total_charge_usd` argument with:

```python
            # The caller passes what is LEFT of this search's budget, not the
            # whole budget. Passing the full budget here let a search spend
            # past the cap it had just been checked against (spec 6.1).
            max_total_charge_usd=Decimal(str(max_charge_usd)),
```

- [ ] **Step 5: Update the caller**

In `agents/graph.py`, inside `reference_prices`, replace the guard block and the `fetch` call:

```python
    remaining = cost_guard.remaining_budget(search_id)
    if not cost_guard.under_budget(search_id, _REF_PRICES_PLANNED_COST_USD):
        log.warning("Apify budget cap reached for search_id=%s; skipping ref-prices", search_id)
        repo.update_search_status(search_id, "awaiting_selection")
        return {}
```

and pass the ceiling through at the `google_shopping.fetch(...)` call site:

```python
        points, cost = google_shopping.fetch(criteria, max_charge_usd=remaining)
```

Update the comment above `_REF_PRICES_PLANNED_COST_USD` to say it is the expected cost of a Google Shopping run (~$0.49) used for the pre-flight check, while the enforced ceiling is now `cost_guard.remaining_budget`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_cost_guard.py tests/test_google_shopping.py -q`
Expected: PASS. Existing `test_google_shopping.py` tests calling `fetch(criteria)` positionally must gain the new `max_charge_usd` argument — update them; do not delete them.

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add pricing/cost_guard.py pricing/google_shopping.py agents/graph.py tests/test_cost_guard.py tests/test_google_shopping.py
git commit -m "fix(pricing): enforce APIFY_BUDGET_USD as a real per-search cap"
```

---

### Task 6: Warning banner, cost line, smoke script, and config cleanup

The user-visible half. A warning nobody sees is not a warning, and the cost line currently shows two components where there are now three.

**Files:**
- Modify: `templates/_search_content.html` (cost summary at lines 31-37; error banner at lines 62-66)
- Modify: `static/app.css`
- Rewrite: `scratch_ebay_search.py`
- Modify: `.env.example`, `README.md`
- Test: `tests/test_searches_routes.py`

**Interfaces:**
- Consumes: from Task 1 `repo.sum_total_cost` returning `{"search", "apify", "claude", "total"}` and the `warning_message` column; from Task 3 `search_ebay`
- Produces: nothing further tasks depend on

- [ ] **Step 1: Write the failing test**

Add to `tests/test_searches_routes.py`:

```python
def test_warning_banner_renders_when_present(client, tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_warning(search_id, "Seller feedback could not be retrieved.")
    body = client.get(f"/searches/{search_id}").text
    assert "Seller feedback could not be retrieved." in body


def test_no_warning_banner_when_absent(client, tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    assert "warning-banner" not in client.get(f"/searches/{search_id}").text


def test_warning_shows_alongside_listings_not_instead_of_them(client, tmp_db):
    """A warning is not a failure — the listings are real and still usable."""
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.add_listings(search_id, [{
        "ebay_item_id": "1", "title": "Sony WH-1000XM5", "price": 200.0,
        "url": "https://www.ebay.com/itm/1", "buying_options": ["BEST_OFFER"],
        "raw_data": {},
    }])
    repo.set_search_warning(search_id, "Seller feedback could not be retrieved.")
    repo.update_search_status(search_id, "awaiting_selection")
    body = client.get(f"/searches/{search_id}").text
    assert "Seller feedback could not be retrieved." in body
    assert "Sony WH-1000XM5" in body


def test_cost_line_shows_discovery_separately(client, tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_cost(search_id, 0.05)
    body = client.get(f"/searches/{search_id}").text
    assert "0.05" in body
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_searches_routes.py -q`
Expected: FAIL — the warning text is absent from the rendered body

- [ ] **Step 3: Render the warning banner**

In `templates/_search_content.html`, immediately **before** the `{% if search.status == 'failed' %}` block at line 62, add:

```jinja
    {# Non-fatal degradation (e.g. seller feedback unavailable). Deliberately
       outside the status branches below: a warning accompanies real results
       rather than replacing them, so the user decides whether to proceed. #}
    {% if search.warning_message %}
        <div class="warning-banner">
            <h2>Heads up</h2>
            <p>{{ search.warning_message }}</p>
        </div>
    {% endif %}
```

- [ ] **Step 4: Show discovery in the cost line**

In `templates/_search_content.html`, replace the breakdown span at line 37:

```jinja
        <span class="cost-breakdown">(Search ${{ "%.2f"|format(costs.search) }}, Apify ${{ "%.2f"|format(costs.apify) }}, Claude ${{ "%.2f"|format(costs.claude) }})</span>
```

- [ ] **Step 5: Style the banner**

Append to `static/app.css`, matching the existing `.error-banner` rules (find them and mirror their spacing and radius, changing only the colours):

```css
/* Non-fatal notice: the search worked, but something degraded and the user
   should know before acting. Amber rather than the error banner's red. */
.warning-banner {
    background: #fff8e1;
    border-left: 4px solid #f0ad4e;
    padding: 0.75rem 1rem;
    margin: 1rem 0;
    border-radius: 4px;
}
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_searches_routes.py -q`
Expected: PASS

- [ ] **Step 7: Rewrite the smoke script**

Replace `scratch_ebay_search.py` entirely. It is how actor output shape gets checked against reality — the mapping's only defence against a silent field rename:

```python
"""Diagnostic: run a real eBay search through the Apify actor.

Why this exists: the actor's output shape is a third-party contract. If
`delicious_zebu` renames a field, the mapping degrades silently — a renamed
`seller_feedback_percent` is caught at runtime (the search warns), but a
renamed `image_url` just becomes None. Running this after any actor update is
how you find out.

This spends real money (~$0.05 per run).

Usage:
    .venv/Scripts/python.exe scratch_ebay_search.py "sony wh-1000xm5" --max-price 250
"""

from __future__ import annotations

import argparse
import json
import sys

from agents.criteria_parser import ParsedCriteria
from integrations import ebay_search


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live eBay search smoke test")
    parser.add_argument("keywords")
    parser.add_argument("--max-price", type=float, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--raw", action="store_true", help="dump the first raw item")
    args = parser.parse_args(argv)

    criteria = ParsedCriteria(
        title_keywords=args.keywords,
        must_not_keywords=[],
        condition_floor=None,
        max_price=args.max_price,
        min_seller_rating=None,
    )

    print("actor input:", json.dumps(ebay_search._build_actor_input(criteria), indent=2))
    try:
        result = ebay_search.search_ebay(criteria, limit=args.limit)
    except ebay_search.EbaySearchError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"\ncost: ${result.cost_usd:.4f}   listings: {len(result.listings)}")
    if result.warning:
        print(f"WARNING: {result.warning}")

    for listing in result.listings:
        ship = "?" if listing.shipping_cost is None else f"{listing.shipping_cost:.2f}"
        rating = "?" if listing.seller_rating is None else f"{listing.seller_rating}%"
        print(f"  ${listing.price:>8.2f} +{ship:>6}  {rating:>7} {listing.seller_id or '?':<20} {listing.title[:50]}")

    # Field-by-field presence check: this is the actual point of the script.
    if result.listings:
        first = result.listings[0]
        missing = [f for f in ("shipping_cost", "condition", "seller_id",
                               "seller_rating", "seller_feedback_count", "image_url")
                   if getattr(first, f) is None]
        if missing:
            print(f"\nfields absent on the first listing: {', '.join(missing)}")
            print("If a field is absent across ALL listings, the actor's output shape may have changed.")
        if args.raw:
            print("\nraw item:", json.dumps(first.raw_data, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: Clean up config docs**

In `.env.example`, delete the `EBAY_APP_ID` and `EBAY_CERT_ID` lines and add:

```
# Ceiling for one eBay discovery run (~$0.05 actual). Separate from
# APIFY_BUDGET_USD because the key broker debits friends the declared ceiling.
EBAY_SEARCH_BUDGET_USD=0.15
```

Leave `EBAY_USER_TOKEN` in place — the Trading API still needs it until Phase 0b.

In `README.md`, update any setup text that tells the reader to obtain an eBay developer keyset for *search*, and say discovery now runs through Apify. Keep whatever the README says about `EBAY_USER_TOKEN` and the account-deletion endpoint, both of which remain required.

- [ ] **Step 9: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add templates/_search_content.html static/app.css scratch_ebay_search.py .env.example README.md tests/test_searches_routes.py
git commit -m "feat(search): warning banner, discovery cost line, live smoke script"
```

---

## Verification

After Task 6, confirm the spec's success criteria hold:

- [ ] `.venv/Scripts/python.exe -m pytest -q` — all green.
- [ ] `grep -rn "EBAY_APP_ID\|EBAY_CERT_ID" --include=*.py --include=*.example . | grep -v __pycache__` returns nothing.
- [ ] `grep -rn "from browser\|import browser" --include=*.py . | grep -v __pycache__` returns nothing.
- [ ] `grep -rn "EBAY_USER_TOKEN" config.py` still finds it — Phase 0b removes it, not this plan.
- [ ] **Live**: `.venv/Scripts/python.exe scratch_ebay_search.py "sony wh-1000xm5" --max-price 250` returns real listings, reports a cost near $0.05, and lists no absent fields. This is the only check that validates the mapping against the real actor.
- [ ] **Live, as a friend**: with `SCRAPERAGENT_BROKER_URL`/`TOKEN` set and `ANTHROPIC_API_KEY`/`APIFY_TOKEN` blank, run a full search from the dashboard and confirm listings appear.

## Notes for the executor

- **Do not delete `integrations/ebay_trading.py`, `api/routes/ebay_notifications.py`, or the `/ebay/account-deletion` entry in `EXEMPT_PATHS`.** They look like dead weight after this plan and are not — Phase 0b removes them.
- **`condition_floor` is a floor.** `"used"` sends no condition filter. If you find yourself mapping `"used"` to `"3000"`, stop: that silently excludes every new listing.
- **`EBAY_SEARCH_BUDGET_USD` must not be replaced by `APIFY_BUDGET_USD`.** They look redundant and are not; the broker debits friends whichever ceiling is declared.
- **Task 3 deviates from spec §3** on the return type, for the reason stated in that task. Everything else follows the spec as written.
