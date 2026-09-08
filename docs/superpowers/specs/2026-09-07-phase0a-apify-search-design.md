# Phase 0a — Apify listing search

**Date:** 2026-09-07
**Status:** Approved for planning
**Scope:** Replace the eBay Browse API with an Apify actor for listing discovery.
Phase 0b (replacing the three Trading API calls with Playwright) is deliberately
out of scope.

---

## 1. Problem and goals

`browser/ebay.py` fetches listings through eBay's Browse API using
`EBAY_APP_ID` + `EBAY_CERT_ID`. Those are developer-portal credentials, so an
invited friend cannot obtain them by signing into eBay in a browser. `discover`
is the **first** node in the search graph (`agents/graph.py:139`), so a friend's
search fails at step one, before any listing exists.

This is the single largest barrier to anyone but the owner running the app.

**Goals**

1. Listing discovery works with no eBay developer credentials.
2. The `discover` node's interface is unchanged: `search_ebay(criteria,
   limit) -> list[Listing]`, raising `EbaySearchError`.
3. Discovery spend is recorded and counted against the per-search budget.
4. Failures a friend can act on say so in words they can act on.

**Success criteria**

- A friend with `SCRAPERAGENT_BROKER_URL`/`TOKEN` and no eBay credentials
  completes a search and sees ranked, Best-Offer-eligible listings.
- `EBAY_APP_ID` and `EBAY_CERT_ID` no longer appear in `config.py` or
  `.env.example`.
- The existing suite passes, with Browse API tests rewritten rather than deleted.

## 2. Non-goals

Phase 0b: `send_member_message`, `get_best_offer_status`, and the poller's
`get_messages_for_item` keep using the Trading API. Consequently
`EBAY_USER_TOKEN` **stays** in config, and `api/routes/ebay_notifications.py`
plus the `/ebay/account-deletion` entry in `EXEMPT_PATHS` **stay** — deleting
them requires having no keyset at all, which is only true after Phase 0b.

Also out of scope: replacing the Google Shopping reference-pricing actor
(tracked as a follow-up in §10), and any change to Best Offer placement, which
already runs through Playwright.

## 3. Architecture

**New module `integrations/ebay_search.py`**, mirroring the established Apify
pattern in `pricing/google_shopping.py`: a module-level cached client built from
`clients.apify_kwargs()`, a domain exception, and cost returned alongside
results.

**Deleted:** `browser/ebay.py` and the `browser/` package. The name is an active
hazard — it means eBay's *Browse API*, while `integrations/ebay_browser.py` one
directory away is the actual Playwright browser. Two unrelated things called
"browser". Phase 0a replaces that file's entire contents, so this is the one
moment where fixing the name costs nothing.

**Interface preserved exactly.** `agents/graph.py:20` changes its import; the
`discover` node is otherwise untouched:

```python
def search_ebay(criteria: ParsedCriteria, limit: int = 25) -> list[Listing]
```

`Listing` and `EbaySearchError` move to the new module unchanged, so
`add_listings`, the ranking code, and the templates all keep working.

Rejected alternative: a config-switchable `SearchProvider` protocol keeping both
implementations. That would leave `EBAY_APP_ID`/`CERT_ID` in config, in
`.env.example`, and in a friend's mental model of what they must obtain — the
credential is the thing being removed, so a code path needing it defeats the
purpose.

## 4. Actor and input mapping

**Actor:** `delicious_zebu/ebay-product-listing-scraper` (id `8bXnzCF4JVgMMA5cM`),
pay-per-event at **$0.002 per result**. Chosen because it is the only candidate
whose output carries seller feedback percentage *and* count, and whose input
supports a Best-Offer filter.

Input built from `ParsedCriteria`:

| Criteria | Actor input | Note |
|---|---|---|
| `title_keywords` | `keywords: [text]` | array, one search |
| `must_not_keywords` | `excludeKeywords` | space-joined |
| `max_price` | `maxPrice` | integer |
| `condition_floor` | `condition` | see below |
| — | `buyingFormat: "LH_BO"` | Best Offer only |
| — | `ebaySite: "www.ebay.com"` | |
| — | `sortBy: "12"` | Best Match, matching today's default ranking |
| — | `maxPages: 1` | 240 items/page is well above `limit` |

**Condition is a floor, not an equality.** `condition_floor == "new"` maps to
`condition: "1000"` (new only). `condition_floor == "used"` or `None` **omits**
the filter entirely, because "used" means "used or better" and must still admit
new items. Getting this backwards would silently exclude new listings from every
used-floor search.

**`min_seller_rating` stays a client-side filter.** The actor has no input for
it, so the existing post-response filtering is retained — with one exception:
when seller ratings are unavailable for the whole run, the filter is skipped and
the user is warned instead. See §5.1.

**`limit`** is applied client-side after mapping, since the actor paginates by
page rather than item count.

## 5. Output mapping

The actor returns strings throughout, so this is parsing, not renaming.

| `Listing` field | Actor field | Transform |
|---|---|---|
| `ebay_item_id` | `item_id` | — |
| `title` | `product_title` | — |
| `price` | `price` | strip currency/separators → float |
| `shipping_cost` | `shipping_cost` | "Free" → `0.0`; "+$5.99" → `5.99`; else `None` |
| `condition` | `condition` | — |
| `seller_id` | `seller_name` | — |
| `seller_rating` | `seller_feedback_percent` | strip `%` → float |
| `seller_feedback_count` | `seller_feedback_count` | strip separators → int |
| `url` | `product_url` | — |
| `image_url` | `image_url` | — |
| `buying_options` | derived | `["BEST_OFFER"]` — every result is BO-filtered |
| `listed_at` | *unavailable* | always `None` |
| `raw_data` | whole item | as today |

**`listed_at` is knowingly lost.** `strategies/__init__.py:173`
(`_listing_age_days`) already returns `None` for a missing value, and line 125
guards on it, so the only casualty is the "listing has sat more than 30 days"
strategy signal. No code path breaks.

**A listing that fails to parse is skipped, not fatal.** One malformed price
must not fail an entire search. Skips log the offending `item_id` at WARNING.

### 5.1 Degraded seller feedback

`seller_feedback_percent` is the one mapped field whose loss is both silent and
dangerous. If the actor renames it, every `seller_rating` becomes `None`, and
the existing client-side filter — `seller_rating is not None and seller_rating
>= threshold` (`browser/ebay.py:208`) — **fails closed**, dropping every
listing. The search would then hit the zero-results path in §7 and tell the user
no Best-Offer listings matched, sending them off to widen criteria that were
never the problem.

**Detection.** One listing missing a feedback percentage is ordinary — a brand
new seller. *Every* listing in a successful run missing it is a broken contract.
So: when a run returns at least one raw item but **no** item yields a parseable
`seller_feedback_percent`, treat seller ratings as unavailable for that search.

**Response — warn and let the user choose, do not fail.** Discovery:

1. **Skips** the `min_seller_rating` filter rather than applying it to all-`None`
   data, so listings survive instead of being silently annihilated.
2. Records a warning on the search (see below) and completes normally into
   `awaiting_selection`.

The dashboard renders that warning as a banner on the search: seller feedback
could not be retrieved, the minimum-rating filter was **not** applied, and the
user should check sellers on eBay before making an offer. They may proceed at
their own risk or abandon the search — the choice is theirs, made with the facts.

A hard `EbaySearchError` was considered and rejected: the listings are still
real and useful, and refusing to show them is a worse outcome than showing them
with an honest caveat.

**Storage.** A new `warning_message TEXT` column on `searches`, nullable, added
through the same `_apply_column_migrations` path as `search_cost_usd`. It is
deliberately a generic warning slot rather than a boolean flag for this one
case, so later non-fatal degradations reuse it. It is distinct from
`error_message`, which means the search failed.

## 6. Cost accounting

Browse API search was free; Apify search is not. Two changes follow.

**Discovery gets its own ceiling.** `EBAY_SEARCH_BUDGET_USD = 0.15`
(25 results × $0.002 ≈ $0.05, with 3× headroom), passed as the run's
`max_total_charge_usd`. It deliberately does **not** reuse `APIFY_BUDGET_USD`:
the broker debits friends the *clamped ceiling* provisionally, so an
over-declared ceiling is a real temporary charge against their budget.

**`APIFY_BUDGET_USD` rises from 0.50 to 0.90.** Google Shopping costs ~$0.49 and
already sat at the old cap (noted in `agents/graph.py`). Adding ~$0.05 of
discovery would push a search past $0.50, causing `cost_guard` to block
reference pricing on every search — a silent loss of market comparison, not a
budget nicety. Total provisional exposure per search becomes $1.05, settling to
roughly $0.54.

**Where the cost lives.** A new `search_cost_usd REAL NOT NULL DEFAULT 0` column
on `searches`, added via the existing `_apply_column_migrations` path in
`db/repo.py` so live databases upgrade in place. `sum_total_cost(search_id)`
gains it as a third component beside `apify` and `claude`, and the dashboard
shows discovery separately — otherwise the total matches no visible source.

Rejected alternative: recording it as a `reference_prices` row with
`source='ebay_search'`. A listing search is not a reference price, and
`get_aggregated_ref_median` would then average it into market medians.

**`pricing/cost_guard.py` must count it.** Discovery now runs and spends before
the guard evaluates, so a guard reading only `sum_apify_cost` under-reports what
has already been spent and the budget stops meaning anything.

### 6.1 `APIFY_BUDGET_USD` must be a real cap, not an estimate

`cost_guard.under_budget()` computes `spent + planned <= APIFY_BUDGET_USD`, so
the budget is intended as a **per-search total**. Today it is not one. The
ceiling handed to the Google Shopping run is the *full* `APIFY_BUDGET_USD`
(`pricing/google_shopping.py:77`) rather than what remains of it, and the guard
validates against `_REF_PRICES_PLANNED_COST_USD = 0.50` while authorizing a run
that may cost the whole budget. With discovery added at $0.90:

```
eBay search spends              ~$0.05
guard: 0.05 + 0.50 <= 0.90       -> passes
Google Shopping ceiling          = $0.90   (full budget, not remaining)
worst-case search total          = $1.05   (exceeds the cap it was checked against)
```

**Fix: pass the remaining budget as the run ceiling**, the same clamp the broker
already applies to a friend's run:

```python
remaining = max(0.0, config.APIFY_BUDGET_USD - repo.sum_apify_cost(search_id))
# passed as max_total_charge_usd
```

`_REF_PRICES_PLANNED_COST_USD` is then the value actually being authorised
rather than an independent estimate, so the number the guard checks and the
number Apify enforces are the same number. `APIFY_BUDGET_USD` becomes a hard
per-search cap enforced by the vendor, not a hope.

This defect predates Phase 0a — it has been harmless only because the planned
estimate happens to sit near the actual cost. Raising the budget is what pulls
the two apart, so the fix belongs here.

## 7. Error handling

`discover` already catches `EbaySearchError` and marks the search failed; that
contract is preserved. What changes is which failures are possible, and that the
messages are read by a friend rather than by the owner.

| Condition | Behaviour |
|---|---|
| Actor run fails, times out, or returns non-`SUCCEEDED` | `EbaySearchError` → search failed |
| Broker returns **402** (budget exhausted) | `EbaySearchError` naming the budget and pointing at the owner |
| Broker returns **503** or is unreachable | `EbaySearchError` saying the broker is down and that setting their own `APIFY_TOKEN` is an escape hatch |
| A single item fails to parse | Skipped, logged; search continues |
| **Every** item lacks a seller feedback percentage | Not an error — listings returned, rating filter skipped, `warning_message` set (§5.1) |
| No results | Not an error — see below |

Today every Apify failure collapses into one opaque message through a broad
`except Exception`. With a friend on the receiving end, "your monthly budget is
exhausted" versus "actor call failed" is the difference between a self-service
fix and a support request.

**Zero results needs a real answer.** Filtering to Best-Offer-only makes empty
results substantially more likely — many searches match listings where no seller
accepts offers. Today an empty set reaches `awaiting_selection` with nothing to
select, which reads as a broken app.

Discovery therefore treats an empty result set exactly as it treats a search
error: `repo.set_search_error(search_id, msg)` followed by
`repo.update_search_status(search_id, "failed")`, where `msg` states that no
Best-Offer listings matched and suggests widening the price range or condition.

This is a deliberate imprecision: "no matches" is not really a failure, and a
status like `no_results` would be more truthful. It is rejected because a new
status value means touching the status enum, every template branch that renders
status, and the poller's status filters — disproportionate for a message the
user reads once. The user-visible text carries the real meaning; `failed` is
only the mechanism.

## 8. Testing

Following `tests/test_google_shopping.py` conventions: the Apify client is
mocked at module level, and no test touches the network.

- **Mapping** against realistically-shaped payloads: `"$124.99"`, shipping
  `"Free"` and `"+$5.99"`, feedback count `"1,234"`, missing optional fields.
- **A malformed item is skipped** while its siblings still parse.
- **Condition floor**: `"new"` sends `condition: "1000"`; `"used"` and `None`
  send no condition filter.
- **`buyingFormat: "LH_BO"`** is always present in the actor input.
- **Zero results** produce the distinct error message, not a crash or an empty
  selection page.
- **Broker 402 and 503** each produce their specific `EbaySearchError` text.
- **Cost lands on the `searches` row**; `sum_total_cost` reports three
  components.
- **`cost_guard` counts `search_cost_usd`** — the regression guard for discovery
  starving reference pricing.
- **The reference-pricing ceiling is the remaining budget, not the full budget**
  (§6.1): after discovery spends, the value passed as `max_total_charge_usd` is
  `APIFY_BUDGET_USD` minus what the search has already spent, and a search whose
  spend has reached the budget authorises a ceiling of zero rather than a
  negative number.
- **The column migrations** (`search_cost_usd`, `warning_message`) apply to a
  database created before they existed.
- **`min_seller_rating` still filters client-side** when ratings are present.
- **Degraded seller feedback** (§5.1): a run where every item lacks a parseable
  `seller_feedback_percent` returns its listings rather than dropping them,
  skips the rating filter, and sets `warning_message`; a run where only *some*
  items lack it behaves normally and sets no warning. This is the regression
  guard for a silent actor field rename presenting as "no listings matched".

`scratch_ebay_search.py` is rewritten as the live smoke test, matching the other
scratch scripts. It is how actor output shape gets verified against reality
before the mapping is trusted.

## 9. Deletions and config changes

**Deleted:** `browser/ebay.py`, the `browser/` package, `EBAY_APP_ID` and
`EBAY_CERT_ID` from `config.py` and `.env.example`, and the Browse API tests in
`tests/test_ebay_search.py` (rewritten against the actor payload).

**Retained:** `EBAY_USER_TOKEN`, `integrations/ebay_trading.py`,
`api/routes/ebay_notifications.py`, and the `/ebay/account-deletion` entry in
`EXEMPT_PATHS` — all still required until Phase 0b.

**Added:** `EBAY_SEARCH_BUDGET_USD = 0.15`; `APIFY_BUDGET_USD` default 0.50 →
0.90; `searches.search_cost_usd`; `searches.warning_message`.

**Changed:** `pricing/google_shopping.py` passes the *remaining* per-search
budget as `max_total_charge_usd` rather than the full `APIFY_BUDGET_USD`
(§6.1).

## 10. Accepted risks and follow-ups

- **Best-Offer-only narrows results.** Non-negotiable listings no longer appear
  at all. Deliberate: every result is actionable and the search is cheaper. If
  empty results prove common in practice, revisit as "fetch all, rank BO first".
- **Actor output shape is a third-party contract.** A rename of
  `seller_feedback_percent` is now detected automatically and surfaced to the
  user (§5.1) rather than silently disabling seller filtering. Other fields
  carry no such guard: a renamed `image_url` degrades quietly, and a renamed
  `price` or `item_id` shows up as every listing being skipped. The smoke
  script remains the way to check shape deliberately.
- **Listing age is gone**, costing one strategy signal (§5).
- **Follow-up: replace the Google Shopping actor.** At ~$0.49 it is roughly 90%
  of per-search cost, an order of magnitude more than the eBay search this spec
  adds. That is the real cost problem and deserves its own investigation.
- **Follow-up: Phase 0b.** Until it lands, a friend can search, rank, and place
  a Best Offer, but cannot message sellers, check offer status, or have replies
  detected.
