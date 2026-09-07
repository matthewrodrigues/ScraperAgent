# Key Broker — Phase 1 Design

**Date:** 2026-09-07
**Status:** Approved for planning
**Scope:** Phase 1 only. Phase 0 (eBay API removal) is a precondition; Phase 2
(friend distribution) is deliberately out of scope.

---

## 1. Problem and goals

ScraperAgent is a local-first tool. A small number of invited friends should be
able to run their own copy without needing Anthropic or Apify accounts, and
without the owner custodying their eBay sessions or negotiation data.

The blocker for those friends is credentials, not code. They can install the
app; they cannot reasonably be asked to create vendor accounts and set up
billing for a hobby tool.

**Goals**

1. Friends run the complete app locally and reach Anthropic and Apify through a
   broker that holds the owner's keys.
2. The owner can cap and observe per-friend spend, and cap total spend.
3. No friend data — searches, listings, negotiations, messages, eBay sessions —
   reaches the broker.
4. The owner's own machine keeps working exactly as it does today.
5. Any user can bypass the broker entirely by supplying their own vendor keys.

**Success criteria**

- A friend with no Anthropic or Apify account completes a full search and
  negotiation round.
- The owner can see month-to-date spend per friend and revoke a friend in one
  command.
- A friend cannot cause spend above their configured monthly budget.
- With `SCRAPERAGENT_BROKER_URL` unset, behaviour is unchanged.

## 2. Non-goals

Billing, payment collection, and usage trials. Public signup, email
verification, and abuse protection. OAuth, token expiry, rotation, and refresh
flows. A shared dashboard over other people's activity. Multi-tenancy in the
app's own database. Horizontal scaling. Streaming support (see §6).

## 3. Precondition — Phase 0

The broker proxies exactly two vendors. The app must therefore stop requiring
eBay API credentials before any friend can use it:

| Module | Credential | Replacement |
|---|---|---|
| `browser/ebay.py` | `EBAY_APP_ID`, `EBAY_CERT_ID` | Apify eBay search actor |
| `integrations/ebay_trading.py` — `send_member_message` | `EBAY_USER_TOKEN` | Playwright |
| `integrations/ebay_trading.py` — `get_best_offer_status` | `EBAY_USER_TOKEN` | Playwright |
| `agents/poller.py` — `get_messages_for_item` | `EBAY_USER_TOKEN` | Playwright, via a serialized browser worker |

`EBAY_USER_TOKEN` is per-account and cannot be proxied, which is what makes
Phase 0 a hard precondition rather than a preference.

Phase 0 also deletes `api/routes/ebay_notifications.py` and the
`/ebay/account-deletion` entry from `EXEMPT_PATHS` in `api/auth.py`, since no
production keyset remains to require a notification endpoint. This leaves the
app with no unauthenticated routes other than `/health`, `/login`, and static
assets.

**Concurrency note carried into Phase 0:** `integrations/ebay_browser.py` uses
`launch_persistent_context` against a single profile directory, and Chrome takes
an exclusive lock on a user-data-dir. A background poller driving a browser
cannot run while a Best Offer placement holds the profile — the placement path
blocks for up to `user_click_timeout_ms` (300s) waiting for a human click. Phase
0 must serialize all browser access through a single worker.

## 4. Architecture

The broker is a transparent reverse proxy. Both SDKs accept a base-URL
override, so clients keep speaking the native vendor wire protocols and the
broker forwards bytes with the owner's key substituted.

```
Friend's machine                    Owner's PC (Tailscale Funnel)      Vendors
┌────────────────────────┐          ┌──────────────────────────┐
│ ScraperAgent (full app)│          │ keybroker (FastAPI)      │
│  dashboard, LangGraph  │          │  token -> friend         │
│  Playwright + Chrome   │  HTTPS   │  quota pre-flight        │ -> api.anthropic.com
│  SQLite (their data)   │ ───────> │  clamp request           │
│  their eBay session    │          │  inject owner key        │ -> api.apify.com
└────────────────────────┘          │  meter from response     │
                                    │  broker.db               │
                                    └──────────────────────────┘
```

The broker holds two tables and no domain model. It never sees listings,
negotiations, or eBay credentials.

**Availability.** In-flight negotiations run entirely on each friend's machine.
A sleeping broker blocks new Claude drafts and new Apify lookups; it does not
stall or desynchronize work already underway.

**Placement.** A `keybroker/` package in this repo — named to avoid visual
collision with the existing `browser/` package. It reuses `config.price_usage()`
rather than growing a second pricing table. Friends receive the code and simply
never run it.

## 5. Routing and authentication

The SDKs disagree on auth header, so token extraction is per-prefix rather than
one middleware.

| Prefix | Upstream | Friend token read from | Replaced with |
|---|---|---|---|
| `/anthropic/*` | `https://api.anthropic.com/*` | `X-Api-Key` | `config.ANTHROPIC_API_KEY` |
| `/apify/*` | `https://api.apify.com/*` | `Authorization: Bearer` | `config.APIFY_TOKEN` |

Path suffixes map straight through: the Anthropic SDK requests
`{base_url}/v1/messages`, the Apify SDK `{api_url}/v2/acts/...`. All other
headers forward unchanged, including `anthropic-version`.

The incoming credential is **stripped and never forwarded** — a friend's token
identifies them to the broker and has no meaning upstream.

**Token format.** `sa_` + `secrets.token_urlsafe(32)`. Stored as SHA-256 hex,
displayed exactly once at creation. Verification hashes the presented token and
does an indexed lookup on `token_sha256`; because the token carries 256 bits of
entropy, SHA-256 destroys any prefix relationship and no constant-time compare
is required.

**Fail closed.** If the broker's own `ANTHROPIC_API_KEY` or `APIFY_TOKEN` is
unset, every proxied route returns 503 — the same stance `api/auth.py` takes on
an unset `DASHBOARD_PASSWORD`. An unknown, revoked, or missing token returns a
flat 401 with no body detail and no redirect; this is a machine endpoint, so the
`HX-Redirect` handling in `_unauthenticated_response` has no analogue. Failed
attempts are logged with the client address.

**Network.** Tailscale Funnel, with the bearer token as the gate. A hardening
variant — putting friends on the tailnet and restricting them to the broker port
by ACL, removing all public exposure — is available but constrained by
free-plan user limits and is not part of this phase.

## 6. Request clamps

Request bodies are forwarded verbatim with three exceptions. Each exists because
the client runs on someone else's machine and its values cannot be trusted.

1. **`max_tokens` ceiling — 4096.** Both call sites use 1024
   (`agents/negotiate.py:137`, `agents/criteria_parser.py:103`). Requests above
   the ceiling are rewritten down, not rejected.
2. **Request body cap — 256 KB.** Bounds input-token cost, which is otherwise
   unbounded and is the larger term at these `max_tokens` values. Oversized
   requests get 413.
3. **`max_total_charge_usd` clamp (Apify run creation only).** Rewritten down to
   the friend's remaining budget. Apify is the only vendor exposing a pre-spend
   ceiling, so this is the one place spend is capped before it happens rather
   than measured after.

**Streaming is rejected with 400.** Nothing in the app streams, and a proxy that
partially handles SSE would under-meter silently. Detected by `"stream": true`
in the request body.

Together, clamps 1 and 2 make the worst-case single Anthropic call
deterministic: 4096 output tokens at Sonnet 4.6's $15/MTok is $0.061, plus
~64k input tokens at $3/MTok is $0.192 — about **$0.25**. This is the basis for
the headroom constant in §8.

## 7. Metering

The broker is the authoritative spend ledger. Metering is response-driven and
differs per vendor.

**Anthropic.** Responses are non-streaming, so `usage.input_tokens` and
`usage.output_tokens` are read from the buffered body and priced via
`config.price_usage()`. `cache_read_input_tokens` and
`cache_creation_input_tokens` are recorded as well — both zero today, but
recording them now means adding prompt caching later does not silently
mis-price. `upstream_ref` is the response `id`.

**Apify.** Cost appears as `usageTotalUsd` on the run object. The broker
records spend when a JSON response body carries a `data` object containing both
a terminal `status` (`SUCCEEDED`, `FAILED`, `ABORTED`, `TIMED-OUT`) and
`usageTotalUsd` — which covers both the run-creation response and the poll
responses without the broker needing to route-match. `upstream_ref` is
`data.id`. Because `.call()` polls, the same terminal run object is seen
repeatedly, so writes are idempotent on the run id via a partial unique index
and `INSERT OR IGNORE` — mirroring the `idx_messages_pending` idiom already in
`db/schema.sql`.

**Two ledgers, deliberately.** The app's existing per-search cost tracking
(`reference_prices.cost_usd`, `messages.cost_usd`) is untouched and continues to
answer "what did this search cost". The broker's ledger answers "what has this
friend cost me". They may diverge slightly because the broker observes SDK
retries the client never attributes to a search; the broker's figure is the
correct one for quota purposes.

**When the spend write fails**, the response is forwarded anyway and the failure
logged at ERROR. The money is already spent upstream; withholding the response
would cost the friend both the result and the dollars. Metering is bookkeeping.
The gate is the pre-flight check.

## 8. Quota enforcement

Two caps, both checked before forwarding, both scoped to the calendar month:

- **Per friend** — `friends.monthly_budget_usd`.
- **Global** — `BROKER_GLOBAL_MONTHLY_BUDGET_USD`, across all friends. This is
  the control that protects the owner if a per-friend cap is misconfigured or a
  client misbehaves.

**Headroom reservation.** The check reserves the worst-case single call rather
than comparing against raw spend:

```
if spent_this_month + BROKER_MAX_SINGLE_CALL_USD > budget:  reject
```

with `BROKER_MAX_SINGLE_CALL_USD = 0.30` (the $0.25 from §6, rounded up). This
makes overspend impossible by construction: the final permitted call lands at or
under the budget, so the owner sets the budget to the true ceiling and the
broker does the subtraction. Apify contributes no overshoot at all, since it is
clamped pre-spend.

**Rejections return 402, never 429.** The Anthropic SDK retries 429 twice by
default and the Apify SDK four times; a quota rejection sent as 429 would be
retried up to four times before surfacing. 402 sits outside both retry sets and
fails immediately and legibly. The response body names the friend, the budget,
and month-to-date spend.

## 9. Data model

`broker.db`, separate from `scraperagent.db`, under `SCRAPERAGENT_DATA_DIR`.
Conventions follow `db/schema.sql`: integer primary keys, ISO-8601 UTC text
timestamps, `cost_usd REAL`, cascading foreign keys.

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS friends (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    token_sha256       TEXT NOT NULL UNIQUE,
    monthly_budget_usd REAL NOT NULL DEFAULT 5.0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at         TEXT
);

CREATE TABLE IF NOT EXISTS spend (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id             INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    vendor                TEXT NOT NULL,        -- anthropic | apify
    model_or_actor        TEXT,
    cost_usd              REAL NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    upstream_ref          TEXT,                 -- message id / run id
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spend_friend_time ON spend(friend_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_upstream
    ON spend(vendor, upstream_ref) WHERE upstream_ref IS NOT NULL;
```

Revocation sets `revoked_at`; rows are never deleted, so spend history survives
a departed friend.

## 10. Client changes

A new `integrations/clients.py` becomes the single place vendor credentials are
resolved. It returns **constructor keyword arguments, not constructed clients**:

```python
def anthropic_kwargs() -> dict[str, Any]:
    if config.BROKER_URL:
        return {"api_key": config.BROKER_TOKEN,
                "base_url": f"{config.BROKER_URL}/anthropic"}
    return {"api_key": config.ANTHROPIC_API_KEY}


def apify_kwargs() -> dict[str, Any]:
    if config.BROKER_URL:
        return {"token": config.BROKER_TOKEN,
                "api_url": f"{config.BROKER_URL}/apify"}
    return {"token": config.APIFY_TOKEN}
```

Three call sites keep constructing their own client, but source the arguments
from the factory:

| File | Line | Change |
|---|---|---|
| `agents/criteria_parser.py` | 100 | `Anthropic(api_key=...)` -> `Anthropic(**clients.anthropic_kwargs())` |
| `agents/negotiate.py` | 134 | `Anthropic(api_key=...)` -> `Anthropic(**clients.anthropic_kwargs())` |
| `pricing/google_shopping.py` | 50 | `ApifyClient(config.APIFY_TOKEN)` -> `ApifyClient(**clients.apify_kwargs())` |

**Why kwargs rather than clients.** The existing suite patches the SDK classes
where they are used — `agents.negotiate.Anthropic`,
`agents.criteria_parser.Anthropic`, `pricing.google_shopping.ApifyClient` — at
roughly twenty sites. Returning constructed clients would move construction out
of those modules and break every one of those patches. Returning kwargs leaves
each module's `Anthropic(...)` / `ApifyClient(...)` call in place, so all
existing tests keep passing untouched, which is what goal 4 requires.

`pricing/google_shopping.py` keeps its module-level client cache; only the
argument source moves. `api_public_url` stays at its default — it builds
shareable links, not authenticated calls.

The `if config.BROKER_URL` branch is the escape hatch: unset means direct vendor
calls with local keys, so the owner's machine is unaffected and any friend can
bypass a dead broker by supplying their own keys.

## 11. Configuration

**Client side** (friend's `.env`): `SCRAPERAGENT_BROKER_URL` and
`SCRAPERAGENT_BROKER_TOKEN`, exposed as `config.BROKER_URL` and
`config.BROKER_TOKEN` — the same env-var-to-attribute naming already used for
`SCRAPERAGENT_DB_PATH` -> `config.DB_PATH`. A URL set without a token raises at
startup rather than silently falling back to keys the friend does not have.

**Broker side** (owner's `.env`, existing keys reused):
`BROKER_GLOBAL_MONTHLY_BUDGET_USD` (default 25.00), `BROKER_DB_PATH` (defaults
to `SCRAPERAGENT_DATA_DIR/broker.db`), `BROKER_PORT` (default 8001).

`BROKER_MAX_SINGLE_CALL_USD`, the `max_tokens` ceiling, and the body cap are
module constants in `keybroker/`, not env vars — they are derived from the
pricing table and should change deliberately with it.

## 12. Error handling

| Condition | Response | Notes |
|---|---|---|
| Missing / unknown / revoked token | 401 | No detail, no redirect; logged with client address |
| Broker's own vendor key unset | 503 | Fail closed, mirroring `api/auth.py` |
| Per-friend or global budget exhausted | 402 | Outside both SDKs' retry sets |
| Request body over 256 KB | 413 | |
| `"stream": true` | 400 | Explicit, not silent under-metering |
| Upstream 4xx/5xx | forwarded verbatim | Client SDK's own retry logic applies |
| Upstream timeout / connection error | 502 | |
| Spend write fails after successful upstream call | response forwarded, ERROR logged | Money already spent |

Upstream request timeout is 10 minutes, matching the Anthropic SDK default, so
the broker never times out before the client does.

## 13. Testing

Tests live in `tests/`, following existing conventions (pytest, FastAPI
`TestClient`, no live network). Upstreams are stubbed at the httpx layer.

**Auth** — valid token passes; unknown, revoked, and missing tokens 401;
Anthropic and Apify tokens read from their respective headers; the friend's
token is never present in the forwarded request; the owner's key is.

**Routing** — `/anthropic/v1/messages` and `/apify/v2/acts/...` map to the right
upstream with path and query preserved; `anthropic-version` survives.

**Clamps** — `max_tokens` above 4096 is rewritten down; below is untouched;
oversized body 413s; `"stream": true` 400s; `max_total_charge_usd` on Apify run
creation is clamped to remaining budget.

**Metering** — Anthropic usage is priced through `config.price_usage()` and
written; cache token fields recorded; repeated Apify terminal-run responses
produce exactly one `spend` row; a failing spend write still returns the
upstream response.

**Quota** — a friend under budget passes; one within
`BROKER_MAX_SINGLE_CALL_USD` of budget gets 402; the global cap rejects even a
friend under their own budget; the month boundary excludes prior-month spend.

**Fail closed** — unset broker vendor key returns 503 on every proxied route.

**Client factory** — with `BROKER_URL` set, `anthropic_kwargs()` and
`apify_kwargs()` return the broker base URL and broker token; unset, they return
the real keys with no URL override; a URL set without a token raises at startup.

The existing 281 tests must continue to pass unchanged, which also serves as the
regression check on goal 4.

## 14. Operations

Scripts follow the existing `scripts/` convention:

```
python -m scripts.add_friend alice --budget 5     # prints token once
python -m scripts.revoke_friend alice
python -m scripts.spend_report                    # per-friend month-to-date
python -m scripts.spend_report --month 2026-08
```

The broker runs as `python -m keybroker` on port 8001, exposed via
`tailscale funnel 8001`. It is a separate process from the dashboard so that
restarting one does not interrupt the other.

## 15. Accepted risks

- **Owner uptime is a dependency.** Friends cannot start new work while the
  broker is down. In-flight negotiations are unaffected. Accepted: this is a
  hobby tool among friends, and the alternative is paid always-on hosting.
- **Prompt content transits the broker.** A friend's Claude prompts pass through
  the owner's machine in memory. They are not logged or stored, but they are
  visible in principle. Friends should be told this; it is inherent to proxying.
- **Trust model is social.** Friends could exhaust their own budget
  deliberately. The global cap bounds total damage; nothing else is enforced.
- **Apify metering depends on response shape.** If Apify renames
  `usageTotalUsd`, metering silently records nothing. Mitigated by the pre-spend
  `max_total_charge_usd` clamp, which does not depend on parsing.
- **Overspend within one call is bounded, not eliminated.** Metering happens
  after the response arrives, so the headroom reservation in §8 is what makes
  the budget a hard ceiling. If the clamps in §6 are weakened, the reservation
  constant must be recomputed with them.
