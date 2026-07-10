"""Centralized settings loaded from .env.

Anything that varies by environment (keys, paths, tuning knobs) lives here.
Hard-coded constants that don't vary (e.g. the strategy rules table) live in
the module that uses them.
"""

import logging
from dotenv import load_dotenv
import os
from pathlib import Path

import pip_system_certs.wrapt_requests  # noqa: F401  patches `requests` to use Windows cert store


log = logging.getLogger(__name__)

# `pip_system_certs` only patches `requests`. The Anthropic SDK and apify-client
# both use `httpx` instead, which has its own SSL context — so Norton's HTTPS
# interception breaks them. `truststore` patches the stdlib ssl module to read
# the OS trust store (where Norton's MITM cert lives), which fixes httpx and
# anything else that uses `ssl.create_default_context()`.
import truststore
truststore.inject_into_ssl()

load_dotenv()

# ---- API keys / secrets ----
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")

# eBay developer keys + user OAuth token (user confirmed all four are present)
EBAY_APP_ID = os.getenv("EBAY_APP_ID")
EBAY_CERT_ID = os.getenv("EBAY_CERT_ID")
EBAY_DEV_ID = os.getenv("EBAY_DEV_ID")
EBAY_USER_TOKEN = os.getenv("EBAY_USER_TOKEN")
EBAY_OAUTH_REFRESH_TOKEN = os.getenv("EBAY_OAUTH_REFRESH_TOKEN")
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
ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "scraperagent.db"
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"
SECRETS_DIR = ROOT_DIR / "secrets"

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
