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
  main.py            FastAPI app, lifespan + middleware wiring, /health and / routes
  auth.py            session-cookie auth: middleware, exempt list, login/logout
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
scripts/
  backup_profile.py  archives the Chrome profile (the one unreproducible file)
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

## Dashboard authentication

Every route is closed by default. The dashboard spends real money (Anthropic
tokens, Apify actor runs) and drives a Chrome session logged into your eBay
account, so an unauthenticated dashboard is a spending and impersonation hole,
not merely a data leak.

Auth is a password (`DASHBOARD_PASSWORD`) exchanged for a signed session cookie,
implemented in `api/auth.py`. Four paths stay open, and that list is the entire
security boundary:

| Path | Why it is exempt |
|---|---|
| `/health` | Liveness probe; returns a constant |
| `/login` | The door itself |
| `/static/*` | CSS and HTMX, needed to render the login page |
| `/ebay/account-deletion` | eBay's servers call it and cannot authenticate |

The eBay webhook is safe to expose because it is genuinely inert: the GET
returns `sha256(challenge + token + endpoint)`, which reveals nothing to anyone
without the verification token, and the POST only logs and returns 204.

Two deliberate behaviors worth knowing:

- **Fail closed.** An unset `DASHBOARD_PASSWORD` returns 503 on every protected
  path rather than allowing access. A misconfigured deploy locks you out instead
  of opening up.
- **HTMX-aware.** The dashboard polls itself. On an expired session an HTMX
  request gets `401` plus `HX-Redirect: /login` so the browser navigates, rather
  than a 303 that HTMX would follow and swap a whole login page into a fragment
  slot.

To rotate the password, edit `.env` and restart. Changing `SESSION_SECRET`
additionally invalidates every existing session.

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

Copy `.env.example` to `.env` and fill it in — it documents every setting the
app reads, including which are required. Because offers and messaging go through
Playwright rather than the buyer-scoped eBay APIs, no user OAuth token or refresh
token is required for placing offers — only the App ID + Cert ID keypair that
authorizes listing search via the Browse API.

The minimum to boot:

```
DASHBOARD_PASSWORD=       # required - an empty value locks the dashboard
SESSION_SECRET=           # python -c "import secrets; print(secrets.token_hex(32))"
ANTHROPIC_API_KEY=
EBAY_APP_ID=
EBAY_CERT_ID=
EBAY_ENV=production
APIFY_TOKEN=
```

`.env` holds every credential the app has, in plaintext. Restrict it to your
user account so other accounts on the machine can't read it:

```
icacls .env /inheritance:r /grant:r "%USERNAME%:F"
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

The suite (roughly 280 tests) covers the criteria parser, aggregator, cost
guard, Google Shopping wrapper, strategy chooser, both LangGraph graphs, the
negotiator, the eBay search and Trading integrations, the background poller, the
repo layer, the search routes, the eBay deletion-notification endpoint, dashboard
auth, environment-driven paths, and the profile-backup script.

Route tests sign in through the real login form rather than bypassing auth, so
the middleware is exercised on every request the suite makes. There is
deliberately no auth-bypass flag — that would be a second, less-tested path
through the security boundary, and exactly the kind of thing that gets left
enabled by accident.

CI runs the suite on `ubuntu-latest` (`.github/workflows/ci.yml`). The Windows
certificate shims in `config.py` are guarded behind `sys.platform == "win32"`
and marked win32-only in `requirements.txt`, so Linux never installs a
workaround for a problem it does not have.

The `scratch_*.py` files at the repo root are manual smoke scripts (Trading API
messaging, Google Shopping, browser login and offer placement, graph runs).
They are load-bearing for validating live integrations but are not part of the
automated suite.

## Persistence

State is a single SQLite database, by default at `./scraperagent.db` and relocatable via `SCRAPERAGENT_DB_PATH` or `SCRAPERAGENT_DATA_DIR`. LangGraph uses a
`SqliteSaver` checkpointer so graph state is durable across restarts. Core
tables: `searches`, `reference_prices`, `listings`, `negotiations`, `messages`.
See `db/schema.sql`.

## Backing up the browser profile

`secrets/ebay_chrome_profile/` is the only local state that cannot be
regenerated by rerunning something — recreating it means an interactive eBay
sign-in through `scratch_ebay_browser_login.py`, with whatever 2FA or captcha
eBay serves that day. Everything else rebuilds itself: the database recreates on
boot, tokens refetch, listings re-search.

```
python -m scripts.backup_profile              # defaults from config
python -m scripts.backup_profile --keep 10
```

Archives land in `~/ScraperAgentBackups` by default — outside the repo and
outside the OneDrive-synced desktop path, since the profile runs to hundreds of
MB and a weekly copy into a synced folder would push all of it to the cloud
every run. Override with `SCRAPERAGENT_BACKUP_DIR`. The five newest are kept;
files the script did not create are never touched.

Files Chrome has locked are skipped with a warning rather than aborting the run,
so a scheduled backup works while the browser is open.

To schedule it weekly (elevated prompt, adjusting the path):

```
schtasks /create /tn "ScraperAgent profile backup" /sc weekly /d SUN /st 03:00 ^
  /tr "\"C:\Users\<you>\...\ScraperAgent\.venv\Scripts\python.exe\" -m scripts.backup_profile" ^
  /sd 01/01/2026
```

Verify with `schtasks /query /tn "ScraperAgent profile backup"`, and run it once
manually to confirm an archive appears before trusting the schedule.

## Deployment

This is a local-first tool by design. The Best Offer flow opens a visible Chrome
window and waits up to five minutes for you to click eBay's final "Send Offer"
button — there is a human at a screen in the critical path, so a headless host
cannot run it unattended. The supported setup is therefore: run it on your own
machine, and put a stable public HTTPS front door on it.

That front door exists for one hard requirement: eBay production keysets need a
permanent HTTPS endpoint for account-deletion notifications. An ngrok URL that
changes on every restart means re-registering with eBay each time.

**Cloudflare Tunnel + Access**, in outline:

1. `cloudflared tunnel login`, then `cloudflared tunnel create scraperagent`
2. Route a hostname you control to the tunnel and point it at
   `http://127.0.0.1:8000`
3. Install the tunnel as a Windows service so it survives reboots
   (`cloudflared service install`)
4. In the Cloudflare Zero Trust dashboard, add an **Access application** covering
   the hostname, with a policy allowing only your email
5. Add a **bypass policy** for the path `/ebay/account-deletion` — eBay's
   servers cannot complete an Access login
6. Set `EBAY_DELETION_ENDPOINT_URL` to the new permanent URL, rotate
   `EBAY_DELETION_VERIFICATION_TOKEN`, restart, and re-register in eBay's
   developer portal

Cloudflare Access is the outer gate; `DASHBOARD_PASSWORD` remains the inner one.
Keeping both means a misconfigured tunnel — or an accidental
`python main.py --host 0.0.0.0` — degrades to "asks for a password" rather than
"wide open".

Two things that do **not** change for this setup: the app stays a single process
(SQLite plus an in-process poller, so no horizontal scaling), and nothing needs
containerizing.

## Key broker

Both API keys in this app cost money per call. If a friend wants to run their
own copy — their own eBay account, their own dashboard, their own negotiations
— the simplest thing is *not* to hand them your `ANTHROPIC_API_KEY` and
`APIFY_TOKEN` outright. The key broker is a small reverse proxy that lets a
friend's full copy of ScraperAgent reach Anthropic and Apify through your keys
instead, under a token you issue and can revoke, with a monthly spend cap on
both the friend and the broker as a whole.

**Running it.** On your machine (the one holding the real keys):

```
python -m keybroker
```

It binds `127.0.0.1:8001` only — never all interfaces, since exposing it is a
separate, deliberate step. To make it reachable from a friend's machine, put it
behind Tailscale Funnel:

```
tailscale funnel 8001
```

Give the resulting URL to your friend as `SCRAPERAGENT_BROKER_URL` in their
`.env` (see `.env.example`), and the token from the next step as
`SCRAPERAGENT_BROKER_TOKEN`. With both set, their copy of the app sends
Anthropic and Apify traffic to your broker instead of the vendors directly;
with both empty (the default), it talks to the vendors with its own keys as
usual.

**Issuing and revoking tokens:**

```
python -m scripts.add_friend alice --budget 7.50
python -m scripts.revoke_friend alice
```

`add_friend` prints the token once and stores only its SHA-256 — if it's lost,
revoke and reissue rather than trying to recover it. `revoke_friend` sets a
`revoked_at` timestamp rather than deleting the row, so past spend stays in the
report for your own accounting.

**Reading the spend report:**

```
python -m scripts.spend_report
python -m scripts.spend_report --month 2026-08
```

Lists each friend's month-to-date spend against their budget, plus a global
total against `BROKER_GLOBAL_MONTHLY_BUDGET_USD`.

**The two caps.** Every friend has a `--budget` (default $5.00/month); once
they hit it, the broker starts rejecting their requests until the calendar
month turns over. There is also one global cap,
`BROKER_GLOBAL_MONTHLY_BUDGET_USD` (default $25.00), that applies across all
friends combined — a backstop against several friends each staying under their
own budget while your total bill still runs away. The broker reserves the cost
of one worst-case call before allowing a request, so the last permitted call
lands at or under the budget; requests already in flight are not reserved
against each other, so a burst of concurrent calls can overshoot by roughly that
reservation per call in flight.

**What the broker will forward.** Only the two Anthropic endpoints this app
uses — `/v1/messages` and `/v1/messages/count_tokens` — anything else on the
Anthropic side gets a 404, because paths like the Batches API slip past the
per-request clamps and the spend meter. The model named in a request must also
be one the broker knows how to price (`config.MODEL_PRICING`); an unknown model
is refused with a 400 rather than being billed to you and metered at zero. If a
friend's copy is pinned to a newer model, add it to `MODEL_PRICING` first.

**Two things worth knowing before you turn this on.** First, a friend's
prompts and API responses pass through your machine's memory on their way to
Anthropic and Apify — the broker is a proxy, not an escrow service, and it does
not shield that traffic from a process running on your host. Second, the
broker only governs traffic your friend's copy chooses to send it: nothing
stops them from setting their own `ANTHROPIC_API_KEY` or `APIFY_TOKEN` in their
own `.env` and bypassing you entirely. The broker is a convenience and a cost
control for a friend acting in good faith, not a security boundary.

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
