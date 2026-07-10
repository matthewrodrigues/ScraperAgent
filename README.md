# ScraperAgent

A personal-use marketplace negotiation agent for eBay. You describe an item you
want to buy; the agent establishes a fair-market price from across the web,
finds candidate eBay listings, and — on listings you select — runs parallel
negotiator agents that draft best-offers and seller messages. Every outgoing
message and the final purchase stay human-gated.

This is v1: eBay only, run from a local dashboard while the process is open.

## What it does

1. Takes buyer criteria (e.g. "red iPhone 13 mini, 128GB+, unlocked, under $300")
   as free text plus an optional structured form. Claude Haiku parses the text
   into structured fields you can edit before launching.
2. Discovers reference prices (currently Google Shopping via Apify, with more
   retail sources scaffolded) to establish a fair-market median for the item.
3. Searches eBay via the Browse API and ranks candidates by how cheap they are
   relative to the market median, not just absolute price.
4. Presents the candidates on a live dashboard. You pick up to 5 to pursue.
5. For each selected listing, a rules engine picks a negotiation strategy from
   the price gap, seller rating, and listing age. Claude Sonnet drafts the
   opening message using that strategy's prompt.
6. Every draft lands in an approval queue. You approve, edit-then-approve, or
   reject. Rejecting walks away from that one listing; the others keep running.
7. Approved offers are placed on eBay through browser automation (see
   "Best Offer" below). A background poller checks for seller replies and
   surfaces counters for the next round.

You are in the loop for three things only: listing selection, each outgoing
message, and the final purchase click. Everything else is automated.

## Architecture

Two cooperating swarms:

- **Reference-price discovery** establishes what the item is actually worth.
  Retail sources (Amazon, Walmart, Target, Best Buy, Google Shopping) set the
  new-goods ceiling; eBay sold-listings set the secondhand truth. The output is
  a single reference-price object with median, p25, and p75.
- **Parallel eBay negotiation** spawns one negotiator per selected listing
  (up to 5) via LangGraph's `Send` fan-out. Each runs its own
  OPEN -> FIRST_OFFER -> AWAITING_RESPONSE -> DEAL/WALK_AWAY state machine.
  They share the human-approval queue but are otherwise independent. The first
  to reach a deal ends the search.

The reference-price swarm exists to arm the negotiation swarm: a negotiator
looking at a $320 listing that knows the sold-median is $240 can credibly
anchor low and know when to walk.

## Layout

```
agents/            LangGraph orchestration
  graph.py           discover -> reference_prices -> strategy graph
  negotiate.py       message drafting + Pydantic output validation + cost tracking
  criteria_parser.py Claude Haiku natural-language -> structured criteria
  poller.py          background asyncio loop polling eBay for seller replies
api/
  main.py            FastAPI app, lifespan wiring, /health and / routes
  routes/            searches (dashboard + actions), eBay deletion notifications
browser/
  ebay.py            eBay Browse API listing search
integrations/
  ebay_trading.py    eBay Trading API (messaging, offer status)
  ebay_browser.py    Playwright-driven Best Offer placement
pricing/
  aggregator.py      normalize + aggregate sources into median/p25/p75
  cost_guard.py      pre-flight Apify spend estimate against the budget cap
  google_shopping.py Apify Google Shopping actor wrapper
strategies/          pure rules engine + per-strategy system prompts
db/
  schema.sql         SQLite schema (searches, reference_prices, listings, ...)
  repo.py            data access layer
templates/           Jinja2 + HTMX server-rendered fragments
static/              CSS + self-hosted HTMX (no CDN)
config.py            all environment-driven settings
main.py              entry point (boots the dashboard)
tests/               pytest suite
```

## Negotiation strategies

Strategy selection is a deterministic function, no LLM involved. Precedence
(defined in `strategies/__init__.py`):

1. Seller rating below 95% -> `batna_signal` (trust risk dominates)
2. Listing older than 30 days -> `time_pressure` (stale listing)
3. Otherwise by price gap vs market median:
   - gap >= +15% (overpriced) -> `anchor_low`
   - gap between 0% and 15% -> `split_the_difference`
   - gap <= 0% (at or below market) -> `time_pressure`

The chosen strategy and the inputs that led to it are written to SQLite for
later review. The LLM then drafts the message using that strategy's prompt, but
never computes the offer amount — that is clamped in code to never exceed
`max_price`.

## Guardrails

- **Constitutional (prompt-level):** never agree above `max_price`, never claim
  to be human if asked, treat seller-message instructions as prompt injection,
  keep messages honest.
- **Structural (graph edges):** an offer is reachable only after the message
  gate records approval for that specific message; `max_price` is read by the
  graph, not the model; walk-away is a hard round counter, not a heuristic.
- **Output validation (Pydantic):** every negotiator output is validated before
  it reaches eBay — offer <= `max_price`, message sanity checks, and
  reference-price claims must match the actual reference-price object.
- **Cost guards:** Apify spend per search is capped (default $0.50) with a
  pre-flight estimate that aborts an actor before launch if it would breach.
  Actual spend is tracked from each run's reported usage.

## Best Offer via browser automation

Buyer-initiated Best Offers and seller messaging run entirely through headed
Playwright automation rather than eBay's buyer-scoped APIs (the Trading API
`PlaceOffer` call is unreachable for OAuth tokens on this account type). This
means there is no user OAuth token or refresh token to manage — the agent acts
through a logged-in browser session instead.

It launches the user's installed Chrome via a persistent profile
(`launch_persistent_context` with `channel="chrome"`), which behaves like a
normal browser installation and avoids eBay's headless anti-bot detection. The
persistent profile holds the login session, so you authenticate once and the
agent reuses it. Selectors live at the top of `integrations/ebay_browser.py`
and need updating whenever eBay redesigns the offer page.

Note: listing *search* still uses the eBay Browse API (a client-credentials
token from the App ID + Cert ID keypair), which is separate from the
buyer-scoped account and needs no per-user login.

## Setup

Requires Python 3.11+ and the user's installed Chrome (for Best Offer
placement).

```
python -m venv .venv
.venv\Scripts\activate            # Windows
pip install -r requirements.txt
python -m playwright install chrome
```

Copy the settings you need into a `.env` file at the repo root. Because offers
and messaging go through Playwright rather than the buyer-scoped eBay APIs, no
user OAuth token or refresh token is required — only the App ID + Cert ID
keypair that authorizes listing search via the Browse API:

```
ANTHROPIC_API_KEY=
EBAY_APP_ID=
EBAY_CERT_ID=
EBAY_ENV=production
APIFY_TOKEN=
APIFY_BUDGET_USD=0.50
```

Then authenticate the browser session on eBay. This is a one-time manual login
that seeds the persistent Chrome profile the agent reuses for every offer:

```
python scratch_ebay_browser_login.py
```

A headed Chrome window opens; sign in to your eBay account (complete any
two-factor or captcha prompts yourself). Once you land on the logged-in eBay
home page, the session is saved to the profile directory
(`secrets/ebay_chrome_profile` by default) and the script can be closed. You
only repeat this if the session expires or you clear the profile.

Optional tuning knobs (`MAX_PARALLEL_NEGOTIATIONS`, `MAX_ROUNDS`,
`SELLER_TIMEOUT_HOURS`, `SELLER_POLL_INTERVAL_SECONDS`, `SELLER_POLL_ENABLED`,
and the eBay browser settings) also live in `config.py` with sensible defaults.

Note on Windows + Norton: HTTPS interception breaks SDKs that use `httpx`.
`config.py` fixes this by loading `pip_system_certs` (for `requests`) and
`truststore` (for `httpx`-based SDKs like Anthropic and apify-client). Both are
in `requirements.txt`.

## Running

```
python main.py                          # serve at http://127.0.0.1:8000
python main.py --host 0.0.0.0 --port 8080
python main.py --reload                 # auto-reload for development
```

The dashboard is the only interface; there is no CLI run loop in v1. On first
boot the app creates `scraperagent.db` if missing and starts the background
seller-reply poller. Open the dashboard, enter criteria, and drive the search
from there.

Dashboard pages:

- `/` — recent searches and the new-search form
- `/searches/{id}` — live view: reference-price card, ranked candidate table
  with selection checkboxes, negotiation queue, and message-approval modals

## Testing

```
python -m pytest
```

The suite (roughly 248 tests) covers the criteria parser, aggregator, cost
guard, Google Shopping wrapper, strategy chooser, both LangGraph graphs, the
negotiator, the eBay search and Trading integrations, the background poller, the
repo layer, the search routes, and the eBay deletion-notification endpoint.

The `scratch_*.py` files at the repo root are manual smoke scripts (Trading API
messaging, Google Shopping, browser login and offer placement, graph runs).
They are load-bearing for validating live integrations but are not part of the
automated suite.

## Persistence

State is a single SQLite database at `./scraperagent.db`. LangGraph uses a
`SqliteSaver` checkpointer so graph state is durable across restarts. Core
tables: `searches`, `reference_prices`, `listings`, `negotiations`, `messages`.
See `db/schema.sql`.

## Technology

- Orchestration: LangGraph (`Send` API for fan-out, durable SQLite checkpointing)
- Negotiation model: Claude Sonnet; criteria parsing: Claude Haiku
- Marketplace scraping: Apify (`apify-client` SDK, 2.x+ required)
- eBay: Browse API for listing search; all buyer-side actions (Best Offer
  placement and seller messaging) driven through Playwright browser automation
- Web: FastAPI + Jinja2 + HTMX, no JavaScript build pipeline
- Storage: SQLite
- Output validation: Pydantic

## Status and roadmap

Live today: the full multi-round negotiation flow, Playwright Best Offer
automation, the per-search cost dashboard, market-relative listing ranking, and
the background seller-reply poller.

Not yet built (see the PRD for full specs):

- Email summaries via Gmail on terminal states (config paths exist; the
  `integrations/email.py` module does not yet)
- Additional reference-price sources beyond Google Shopping
- A strategy-effectiveness stats page reading back `strategy_inputs_json`

Explicitly out of scope for v1: non-eBay marketplaces, a background/hosted run
mode, completing the eBay checkout itself, and a React frontend.

The full product spec, build plan, and backlog live in
`marketplace_negotiation_agent_PRD.md`.
