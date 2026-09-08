"""Centralized settings loaded from .env.

Anything that varies by environment (keys, paths, tuning knobs) lives here.
Hard-coded constants that don't vary (e.g. the strategy rules table) live in
the module that uses them.
"""

import logging
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

# ---- Windows HTTPS interception workaround ----
# Norton (and other Windows AV) man-in-the-middles HTTPS with a locally-trusted
# root cert, which breaks any client that ships its own CA bundle:
#
#   * `pip_system_certs` patches `requests` to use the Windows cert store.
#   * `truststore` patches the stdlib ssl module, which fixes `httpx` — used by
#     the Anthropic SDK and apify-client, neither of which `pip_system_certs`
#     touches.
#
# Guarded to win32 so Linux (CI, and any future container) neither needs these
# packages installed nor inherits a workaround for a problem it doesn't have.
if sys.platform == "win32":  # pragma: no cover - platform-specific bootstrap
    import pip_system_certs.wrapt_requests  # noqa: F401
    import truststore

    truststore.inject_into_ssl()

load_dotenv()

# ---- API keys / secrets ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# eBay credentials. Two independent auth paths, deliberately not unified:
#
#   * APP_ID + CERT_ID authorize the Browse API (listing search) through a
#     client-credentials token that `browser/ebay.py` fetches and refreshes on
#     its own. No user, no consent screen, nothing to store.
#   * USER_TOKEN is a legacy Auth'n'Auth token (valid ~18 months) for the
#     Trading API calls the seller-reply poller makes. There is no refresh
#     token to manage: Auth'n'Auth tokens aren't refreshed, they're regenerated
#     from eBay's developer portal when they expire.
#
# Buyer-side actions (Best Offer, seller messaging) use neither — they go
# through the Playwright browser session. See `integrations/ebay_browser.py`.
EBAY_APP_ID = os.getenv("EBAY_APP_ID")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID")
EBAY_DEV_ID = os.getenv("EBAY_DEV_ID")
EBAY_USER_TOKEN = os.getenv("EBAY_USER_TOKEN")
EBAY_ENV = os.getenv("EBAY_ENV", "production")  # or "sandbox"

# Marketplace Account Deletion notification — required for eBay production keysets.
# eBay POSTs deletion events here; on first registration they GET with a challenge
# that we hash with the verification token + endpoint URL.
EBAY_DELETION_ENDPOINT_URL = os.getenv("EBAY_DELETION_ENDPOINT_URL", "")
EBAY_DELETION_VERIFICATION_TOKEN = os.getenv("EBAY_DELETION_VERIFICATION_TOKEN", "")

# Apify
APIFY_TOKEN = os.getenv("APIFY_TOKEN")
APIFY_BUDGET_USD = float(os.getenv("APIFY_BUDGET_USD", "0.50"))

# Gmail (OAuth client ID/secret from Google Cloud Console; refresh token stored on first consent)
GMAIL_CLIENT_SECRETS_PATH = os.getenv("GMAIL_CLIENT_SECRETS_PATH", "./secrets/gmail_client_secret.json")
GMAIL_TOKEN_PATH = os.getenv("GMAIL_TOKEN_PATH", "./secrets/gmail_token.json")
GMAIL_SENDER = os.getenv("GMAIL_SENDER", "matthew.rodrigues@berkeley.edu")

# ---- Dashboard authentication ----
# The dashboard spends money (Anthropic, Apify) and drives a browser logged into
# the owner's eBay account, so it is closed by default. In the deployed setup
# Cloudflare Access is the outer gate and this is the inner one; keeping both
# means a misconfigured tunnel degrades to "asks for a password" rather than
# "wide open". An empty value locks the dashboard rather than opening it —
# see `api/auth.py`.
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")

# Signs the session cookie. Set this in .env — generate one with:
#   python -c "import secrets; print(secrets.token_hex(32))"
# Falling back to a per-boot random value is safe but logs you out on every
# restart, which is a deliberate nudge to configure it properly.
SESSION_SECRET = os.getenv("SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = secrets.token_hex(32)
    log.warning(
        "SESSION_SECRET not set; generated an ephemeral one. Dashboard sessions "
        "will not survive a restart. Set SESSION_SECRET in .env to fix."
    )

# ---- Tuning knobs ----
MAX_PARALLEL_NEGOTIATIONS = int(os.getenv("MAX_PARALLEL_NEGOTIATIONS", "5"))
MAX_ROUNDS = int(os.getenv("MAX_ROUNDS", "3"))
SELLER_TIMEOUT_HOURS = int(os.getenv("SELLER_TIMEOUT_HOURS", "24"))

# ---- Background poller for seller replies ----
# How often the background poller checks eBay's inbox for new seller messages
# on active negotiations. 300s (5 min) is a pragmatic default — fast enough
# that a reply mid-session lands within a poll cycle, slow enough that we
# don't hammer eBay's Trading API (which has a 5000/day cap per keyset).
# Per-negotiation call rate: 12/hr × 24 = 288/day. Headroom for ~17 active
# negotiations before approaching the cap.
SELLER_POLL_INTERVAL_SECONDS = int(os.getenv("SELLER_POLL_INTERVAL_SECONDS", "300"))
# Flag to disable the poller entirely (e.g. during pytest runs where it'd add
# noise) without removing the lifespan wiring. Defaults to on in production.
SELLER_POLL_ENABLED = os.getenv("SELLER_POLL_ENABLED", "true").lower() in ("1", "true", "yes")

# ---- Models ----
NEGOTIATOR_MODEL = "claude-sonnet-4-6"
PARSER_MODEL = "claude-haiku-4-5-20251001"

# ---- Anthropic pricing (per-1M-token USD) ----
# Keyed by the exact model id string we pass to the SDK so a future model swap
# doesn't strand cost math on a nickname. Defaults reflect Anthropic's
# published rates as of early 2026; verify against the pricing page when
# rates shift. All four rates can be overridden via env without code changes.
#
# `price_usage()` below is the single source of truth for cost math — used by
# `agents/negotiate.py` to compute per-message cost at write time. Unknown
# model ids return 0.0 (with a warning) so a stale pricing table never crashes
# a draft.
MODEL_PRICING: dict[str, dict[str, float]] = {
    NEGOTIATOR_MODEL: {
        "input": float(os.getenv("CLAUDE_SONNET_INPUT_USD_PER_MTOK", "3.00")),
        "output": float(os.getenv("CLAUDE_SONNET_OUTPUT_USD_PER_MTOK", "15.00")),
    },
    PARSER_MODEL: {
        "input": float(os.getenv("CLAUDE_HAIKU_INPUT_USD_PER_MTOK", "1.00")),
        "output": float(os.getenv("CLAUDE_HAIKU_OUTPUT_USD_PER_MTOK", "5.00")),
    },
}


def price_usage(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in USD for a single Claude API call. Returns 0.0 (with warning)
    for unknown models so we never crash a draft because pricing is stale.

    Rounded to 6 decimals — six is enough to capture single-token costs at
    Haiku rates without floating-point noise downstream."""
    rates = MODEL_PRICING.get(model)
    if rates is None:
        log.warning(
            "price_usage: no pricing entry for model %r; returning 0.0. "
            "Add an entry to config.MODEL_PRICING when the model is added.",
            model,
        )
        return 0.0
    cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
    return round(cost, 6)

# ---- Paths ----
# Two categories, deliberately separated:
#
#   * Code paths (TEMPLATES_DIR, STATIC_DIR) are pinned to the repo and ship
#     with the app. They must never move, or Jinja2 goes looking for templates
#     on a data volume.
#   * Data paths (DB_PATH, SECRETS_DIR) are env-overridable so the app can run
#     with its state on a mounted volume or a backed-up directory without
#     touching code. Defaults keep the historical repo-root layout, so an
#     existing checkout needs no .env change.
#
# SCRAPERAGENT_DATA_DIR moves both data paths at once; the per-path vars
# override it individually when only one needs to move.
ROOT_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"

DATA_DIR = Path(os.getenv("SCRAPERAGENT_DATA_DIR", str(ROOT_DIR)))
DB_PATH = Path(os.getenv("SCRAPERAGENT_DB_PATH", str(DATA_DIR / "scraperagent.db")))
SECRETS_DIR = Path(os.getenv("SCRAPERAGENT_SECRETS_DIR", str(DATA_DIR / "secrets")))

# ---- Backups ----
# The Chrome profile is the only unreproducible local state (see
# `scripts/backup_profile.py`). Backups default outside the repo AND outside the
# OneDrive-synced desktop path: the profile runs to hundreds of MB, and dropping
# a fresh copy into a synced folder every week would push that up to the cloud
# on every run.
BACKUP_DIR = Path(os.getenv("SCRAPERAGENT_BACKUP_DIR", str(Path.home() / "ScraperAgentBackups")))
BACKUP_KEEP = int(os.getenv("SCRAPERAGENT_BACKUP_KEEP", "5"))

# ---- eBay browser automation (Option 2 — Playwright) ----
# Persistent browser profile directory. We use `launch_persistent_context`
# rather than storage_state.json because eBay's anti-bot detection fingerprints
# at the TLS / HTTP/2 / browser-binary level — a fresh context loaded with
# saved cookies still gets degraded, but a real persistent Chrome profile
# behaves identically to a normal user's installation. The profile dir holds
# cookies, localStorage, cache, history, and Chrome preferences.
EBAY_BROWSER_PROFILE_DIR = Path(os.getenv("EBAY_BROWSER_PROFILE_DIR", str(SECRETS_DIR / "ebay_chrome_profile")))
# Channel for the browser binary. "chrome" uses the user's installed Chrome
# Stable (vs the bundled Chromium build), which carries a different binary
# signature + TLS fingerprint and is the single biggest lever for evading
# eBay's anti-bot. Falls back to bundled Chromium if Chrome isn't installed.
EBAY_BROWSER_CHANNEL = os.getenv("EBAY_BROWSER_CHANNEL", "chrome")
# Headed by default — eBay aggressively fingerprints headless Chromium, and
# personal-use frequency on a 0-feedback test account is the worst-case
# scenario for anti-bot detection. The browser window briefly pops up during
# offer placement; for "deployed" mode this can be flipped to false.
EBAY_BROWSER_HEADED = os.getenv("EBAY_BROWSER_HEADED", "true").lower() in ("1", "true", "yes")
# Where failure screenshots land. Useful for debugging selector changes on
# eBay's side — when an offer fails, the route surfaces a link to the latest
# screenshot so the user can see what eBay's page actually looked like.
EBAY_BROWSER_SCREENSHOTS_DIR = Path(os.getenv("EBAY_BROWSER_SCREENSHOTS_DIR", str(SECRETS_DIR / "screenshots")))

# ---- Key broker ----
# Client side. When BROKER_URL is set the app sends Anthropic and Apify traffic
# through a broker holding someone else's keys (see
# docs/superpowers/specs/2026-09-07-key-broker-phase1-design.md). Unset means
# direct vendor calls with local keys — the owner's machine, and the escape
# hatch for any friend whose broker is down.
BROKER_URL = os.getenv("SCRAPERAGENT_BROKER_URL", "").rstrip("/")
BROKER_TOKEN = os.getenv("SCRAPERAGENT_BROKER_TOKEN", "")

if BROKER_URL and not BROKER_TOKEN:
    # Fail fast rather than silently falling back to vendor keys a friend does
    # not have — that failure would surface as a confusing 401 from Anthropic.
    raise RuntimeError(
        "SCRAPERAGENT_BROKER_URL is set but SCRAPERAGENT_BROKER_TOKEN is empty. "
        "Set both, or neither (to use your own ANTHROPIC_API_KEY / APIFY_TOKEN)."
    )

# Server side. Only the broker host reads these.
BROKER_DB_PATH = Path(os.getenv("BROKER_DB_PATH", str(DATA_DIR / "broker.db")))
BROKER_PORT = int(os.getenv("BROKER_PORT", "8001"))
BROKER_GLOBAL_MONTHLY_BUDGET_USD = float(
    os.getenv("BROKER_GLOBAL_MONTHLY_BUDGET_USD", "25.00")
)
