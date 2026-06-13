"""Centralized settings loaded from .env.

Anything that varies by environment (keys, paths, tuning knobs) lives here.
Hard-coded constants that don't vary (e.g. the strategy rules table) live in
the module that uses them.
"""

from dotenv import load_dotenv
import os
from pathlib import Path

import pip_system_certs.wrapt_requests  # noqa: F401  patches SSL to use Windows cert store (Norton SSL scanning)

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

# Back-compat aliases used by legacy v0 code paths (negotiate.py); replaced in step 8.
MAX_NEGOTIATION_ROUNDS = MAX_ROUNDS
DEFAULT_STRATEGY = "anchor_low"

# ---- Models ----
NEGOTIATOR_MODEL = "claude-sonnet-4-6"
PARSER_MODEL = "claude-haiku-4-5-20251001"

# ---- Paths ----
ROOT_DIR = Path(__file__).resolve().parent
DB_PATH = ROOT_DIR / "scraperagent.db"
TEMPLATES_DIR = ROOT_DIR / "templates"
STATIC_DIR = ROOT_DIR / "static"
SECRETS_DIR = ROOT_DIR / "secrets"
