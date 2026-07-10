"""Playwright-driven eBay Best Offer placement.

Why this module exists: eBay's Trading API `PlaceOffer` with `Action=BestOffer`
is empirically unreachable for OAuth tokens on this account type (verified
2026-06-17 via `scratch_place_offer_debug.py`; both minimal and BlockOnWarning
variants fail with `ErrorCode=37 / Offer.Action invalid`). The legacy
Auth'n'Auth path is also not available — eBay no longer issues those for new
keysets. See memory file `feedback_ebay_trading_gotchas.md` §3 for the full
postmortem.

Approach: headed Playwright Chromium with a persistent `storage_state.json`
captured during a one-time manual login (`scratch_ebay_browser_login.py`).
Each offer placement loads the saved session, navigates to the listing,
clicks the Make Offer button, fills amount + message, submits, and reads
the resulting confirmation/error.

Selector strategy: prefer ARIA roles (`get_by_role("button", name="Make offer")`)
over CSS classes. ARIA is stable across eBay's UI refreshes; CSS classes
change with every redesign. When eBay does change the offer flow, the
selectors at the top of this module are the only thing that should need
updating — keep them named consts so the diff is obvious.

Failure mode: every exception path captures a screenshot to
`config.EBAY_BROWSER_SCREENSHOTS_DIR` so the user (or future Claude) can
see what eBay's page actually looked like at the moment of failure.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

import config


log = logging.getLogger(__name__)


# ---- Selectors (update when eBay ships UI changes) -----------------------
#
# eBay's "Make Offer" flow has been stable through 2025-2026 but the company
# does redesign the buyer experience periodically. When that happens, run
# `scratch_ebay_browser_place.py` against a known BO listing, watch the headed
# browser, and update whichever selector below failed.
#
# Preference order for each element:
#   1. ARIA role + accessible name (most stable)
#   2. data-testid attribute (eBay uses these sparingly but they exist)
#   3. CSS selector tied to a stable id/name (last resort — breaks first)

# The "Make Offer" CTA on the listing page. Sometimes labeled "Make offer"
# (lowercase O), sometimes "Submit best offer". Use a case-insensitive regex.
MAKE_OFFER_BUTTON_PATTERN = re.compile(r"^(make\s+offer|submit\s+best\s+offer)$", re.IGNORECASE)

# The amount input inside the offer modal. eBay's modal historically uses
# id="binPrice" or name="maxbid" — neither is officially documented. We try
# the most-likely accessible label first.
OFFER_AMOUNT_LABEL_PATTERN = re.compile(r"your offer|offer amount", re.IGNORECASE)

# The optional message textarea for the offer. eBay labels this varies.
OFFER_MESSAGE_LABEL_PATTERN = re.compile(r"message to seller|add a message|message \(optional\)", re.IGNORECASE)

# Final submit on the offer modal — the one that actually sends the offer
# to eBay. Distinct from the initial "Make Offer" CTA on the listing page.
# eBay's flow is typically two steps: an initial "Review offer" button takes
# you to a confirmation page, then a final "Send / Submit / Place" button
# actually transmits. The regex covers both steps so the same selector hits
# either page; the calling code clicks twice with a wait between to walk
# through both states.
SUBMIT_OFFER_BUTTON_PATTERN = re.compile(
    r"^("
    r"review offer|review your offer|"
    r"send offer|send your offer|"
    r"submit offer|submit your offer|"
    r"place offer|place your offer|"
    r"confirm offer|confirm and send|confirm"
    r")$",
    re.IGNORECASE,
)

# After-submit confirmation. eBay shows different text depending on whether
# the offer was accepted, declined, or is pending. Any of these counts as
# "the submit went through and the offer is recorded on eBay's side."
CONFIRMATION_TEXT_PATTERN = re.compile(
    r"offer (sent|placed|submitted|received)|your offer is being reviewed|you've made an offer",
    re.IGNORECASE,
)

# Sign-in redirect detection — if the URL host moves to signin.ebay.com,
# the saved session has expired and the user needs to re-run the login script.
SIGNIN_URL_FRAGMENT = "signin.ebay.com"


# Anti-fingerprinting init script — runs in every page context before page JS
# executes. eBay's anti-bot system inspects `navigator.webdriver`, plugin
# count, language list, and `window.chrome` for telltale Playwright markers.
# Without this shim, eBay degrades the session so cookies stop authenticating
# even when the headed sign-in UI looks like it worked. Keep this in sync with
# the same shim in `scratch_ebay_browser_login.py` — both surfaces must apply
# it or the session cookies captured during login won't carry through.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


class EbayBrowserError(RuntimeError):
    """Raised for any failure during browser-driven offer placement.

    Always carries a `screenshot_path` attribute (may be None if the failure
    happened before a page was loaded). The route layer surfaces this path
    so the user can see eBay's actual rendered DOM at the moment of failure.
    """

    def __init__(self, message: str, screenshot_path: Path | None = None) -> None:
        super().__init__(message)
        self.screenshot_path = screenshot_path


class EbaySessionExpiredError(EbayBrowserError):
    """Raised specifically when the saved storage_state no longer authenticates.

    Detected by checking whether the listing page redirects to signin.ebay.com.
    Route catches this distinctly so the dashboard can surface a "re-login
    required" notice with instructions to run scratch_ebay_browser_login.py."""


@dataclass
class OfferResult:
    """Structured result of place_best_offer_via_browser.

    On success: success=True; offer_ref optionally captured if eBay's
    confirmation page exposes a reference id; screenshot is still taken for
    audit purposes so the user can verify what eBay confirmed.

    On failure: success=False; error_message is the human-readable cause;
    screenshot points at the captured DOM state. Caller decides whether to
    raise or surface to the UI based on context."""

    success: bool
    offer_ref: str | None
    screenshot_path: Path | None
    error_message: str | None = None


def _screenshot_path(prefix: str) -> Path:
    """Generate a unique screenshot path under config.EBAY_BROWSER_SCREENSHOTS_DIR.
    Uses ISO timestamp so chronological order matches filesystem order."""
    config.EBAY_BROWSER_SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return config.EBAY_BROWSER_SCREENSHOTS_DIR / f"{prefix}_{ts}.png"


def _save_screenshot(page: Page | None, prefix: str) -> Path | None:
    """Best-effort screenshot. Returns None if the page is gone or capture fails.
    A debug-only path; we never let screenshot failure mask the real exception."""
    if page is None:
        return None
    try:
        path = _screenshot_path(prefix)
        page.screenshot(path=str(path), full_page=True)
        return path
    except Exception as exc:  # noqa: BLE001 — diagnostic best-effort
        log.warning("screenshot capture failed: %s", exc)
        return None


def _launch_context(p: Playwright) -> BrowserContext:
    """Launch Chrome with the persistent user-data profile from
    `config.EBAY_BROWSER_PROFILE_DIR`.

    Persistent-context (vs storage_state) is required because eBay fingerprints
    at a lower layer than cookies: TLS handshake, HTTP/2 framing, font set,
    binary signature. A fresh browser context loaded with saved cookies still
    gets the degraded session; a real persistent profile with the user's actual
    Chrome binary does not.

    Channel="chrome" uses the installed Chrome Stable on the user's machine
    instead of Playwright's bundled Chromium build. If Chrome isn't installed,
    we fall back to bundled Chromium with a clear warning — the offer flow
    may still work but the anti-bot risk is higher.
    """
    if not config.EBAY_BROWSER_PROFILE_DIR.exists() or not any(config.EBAY_BROWSER_PROFILE_DIR.iterdir()):
        raise EbayBrowserError(
            f"No saved Chrome profile at {config.EBAY_BROWSER_PROFILE_DIR}. "
            "Run `python scratch_ebay_browser_login.py` first to log in and persist the profile."
        )
    try:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(config.EBAY_BROWSER_PROFILE_DIR),
            channel=config.EBAY_BROWSER_CHANNEL or None,
            headless=not config.EBAY_BROWSER_HEADED,
        )
    except Exception as exc:
        # Most common cause: channel="chrome" but Chrome Stable isn't installed.
        # Fall back to the bundled Chromium build with a clear warning.
        log.warning(
            "launch_persistent_context with channel=%r failed (%s); "
            "falling back to bundled Chromium. eBay's anti-bot detection is more "
            "likely to flag this — install Chrome Stable if you hit auth issues.",
            config.EBAY_BROWSER_CHANNEL, exc,
        )
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(config.EBAY_BROWSER_PROFILE_DIR),
            headless=not config.EBAY_BROWSER_HEADED,
        )
    # Apply the stealth shim. Less critical now that we're using real Chrome,
    # but still useful — eBay's JS-level checks fire regardless of the binary
    # underneath, and masking these markers is cheap.
    context.add_init_script(STEALTH_INIT_SCRIPT)
    return context


def _check_session_alive(page: Page) -> None:
    """Raise EbaySessionExpiredError if the current URL indicates a sign-in redirect.

    eBay redirects to signin.ebay.com when cookies have expired or been invalidated
    server-side. The dashboard catches this exception and shows a re-login banner.
    """
    if SIGNIN_URL_FRAGMENT in page.url:
        screenshot = _save_screenshot(page, "session_expired")
        raise EbaySessionExpiredError(
            f"Saved session expired (redirected to {page.url}). "
            "Re-run `python scratch_ebay_browser_login.py` to refresh.",
            screenshot_path=screenshot,
        )


def place_best_offer_via_browser(
    listing_url: str,
    amount: float,
    message: str,
    *,
    timeout_ms: int = 30_000,
    user_click_timeout_ms: int = 300_000,
) -> OfferResult:
    """Place a Best Offer on an eBay listing — automation + final human click.

    Flow:
      1. Open the listing page in a real Chrome window (visible to user).
      2. Click eBay's "Make Offer" CTA.
      3. Fill the offer amount and optional message.
      4. Click eBay's "Review offer" button (which moves to the review page).
      5. **Wait up to `user_click_timeout_ms`** for the user to click the
         final "Send Offer" button on eBay's UI. Detected by URL change or
         confirmation text appearing.
      6. On confirmation: success.
         On timeout / browser closed without confirming: failure.

    Why we stop short of the final click:
      eBay's two-step Review → Send flow imposes a final human confirmation
      that's actually a natural fit for the agent's design — the human
      already approved the draft in the dashboard, and the final eBay click
      adds a second confirmation that the offer is intended. Trying to fully
      automate the final click is fragile (eBay's button labels vary across
      A/B tests and listing types) and offers no real safety improvement.

    Args:
        listing_url: Full eBay listing URL.
        amount: Dollar amount to offer.
        message: Free-text message; eBay may not show the field on every listing.
        timeout_ms: Per-action timeout for finding/filling elements (~30s default).
        user_click_timeout_ms: How long to wait for the user to click Send on
            eBay's UI before declaring failure (~5 min default).

    Returns OfferResult. On success, `screenshot_path` captures eBay's
    confirmation page for audit. On failure, captures whatever state the
    page was in at the moment of failure.
    """
    if not listing_url:
        raise EbayBrowserError("listing_url required")
    if amount is None or amount <= 0:
        raise EbayBrowserError(f"amount must be positive, got {amount!r}")

    amount_str = f"{float(amount):.2f}"
    log.info(
        "BO via browser: url=%s amount=$%s message_len=%d user_wait=%ds",
        listing_url, amount_str, len(message or ""), user_click_timeout_ms // 1000,
    )

    page: Page | None = None
    with sync_playwright() as p:
        context = _launch_context(p)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(timeout_ms)

            page.goto(listing_url, wait_until="domcontentloaded")
            _check_session_alive(page)

            # Step 1 — click the listing-page "Make Offer" CTA.
            try:
                make_offer = page.get_by_role("button", name=MAKE_OFFER_BUTTON_PATTERN).first
                make_offer.click(timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                screenshot = _save_screenshot(page, "no_make_offer_button")
                raise EbayBrowserError(
                    "Couldn't find the 'Make Offer' button on the listing page. "
                    "Listing may not accept Best Offer, or eBay's button label has changed. "
                    f"See screenshot: {screenshot}",
                    screenshot_path=screenshot,
                ) from exc

            _check_session_alive(page)

            # Step 2 — fill the offer amount.
            try:
                amount_input = page.get_by_label(OFFER_AMOUNT_LABEL_PATTERN).first
                amount_input.fill(amount_str, timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                screenshot = _save_screenshot(page, "no_amount_input")
                raise EbayBrowserError(
                    "Couldn't locate the offer-amount input. eBay's offer modal "
                    f"may have a new structure. See screenshot: {screenshot}",
                    screenshot_path=screenshot,
                ) from exc

            # Step 3 — fill the optional message.
            if message:
                try:
                    message_field = page.get_by_label(OFFER_MESSAGE_LABEL_PATTERN).first
                    message_field.fill(message, timeout=5_000)
                except PlaywrightTimeoutError:
                    log.info("offer message field not present on this listing; skipping message")

            # Step 4 — click "Review offer" (or whichever first-submit label eBay
            # is using). This moves us to the review page where the user makes
            # the final-send click.
            try:
                submit = page.get_by_role("button", name=SUBMIT_OFFER_BUTTON_PATTERN).first
                submit.click(timeout=timeout_ms)
            except PlaywrightTimeoutError as exc:
                screenshot = _save_screenshot(page, "no_submit_button")
                raise EbayBrowserError(
                    "Couldn't find the offer submit button. Review the screenshot "
                    f"to see eBay's current modal state. See: {screenshot}",
                    screenshot_path=screenshot,
                ) from exc

            # Step 5 — wait for the USER to click eBay's final "Send Offer"
            # button. Detected two ways: a URL change matching the confirmation
            # pattern, OR confirmation text appearing on the current page (eBay
            # sometimes updates inline without a URL change). First-success
            # wins. Browser-closed mid-wait also surfaces here — Playwright
            # raises an error which we catch below.
            log.info("waiting up to %ds for user to click final Send on eBay's UI", user_click_timeout_ms // 1000)
            confirmed = False
            try:
                # wait_for_url returns when the URL matches the pattern; we use
                # a permissive pattern that catches eBay's variety of post-send
                # URLs (thanks page, confirmation page, etc.).
                page.wait_for_url(
                    re.compile(r"(?i)thanks|confirmation|offer/placed|offerplaced|offer-confirmation"),
                    timeout=user_click_timeout_ms,
                )
                confirmed = True
            except PlaywrightTimeoutError:
                # URL didn't change in the allotted window. Before giving up,
                # check for inline confirmation text — eBay's offer flow
                # sometimes updates without changing the URL.
                try:
                    page.get_by_text(CONFIRMATION_TEXT_PATTERN).first.wait_for(
                        state="visible", timeout=2_000,
                    )
                    confirmed = True
                except PlaywrightTimeoutError:
                    pass

            if not confirmed:
                screenshot = _save_screenshot(page, "user_no_click")
                return OfferResult(
                    success=False,
                    offer_ref=None,
                    screenshot_path=screenshot,
                    error_message=(
                        f"User didn't click Send Offer within {user_click_timeout_ms // 1000}s, "
                        "or closed the browser before confirming. The draft is preserved; "
                        "click Send Browser again to retry."
                    ),
                )

            # Confirmed — eBay shows the offer-sent page. Audit screenshot for
            # the user's records.
            screenshot = _save_screenshot(page, "offer_placed")
            log.info("BO placed successfully (user clicked Send): screenshot=%s", screenshot)
            return OfferResult(success=True, offer_ref=None, screenshot_path=screenshot)
        except EbaySessionExpiredError:
            raise
        except EbayBrowserError:
            raise
        except Exception as exc:  # noqa: BLE001 — wrap unexpected failures with screenshot
            screenshot = _save_screenshot(page, "unexpected")
            # Most common "unexpected" failure: user closed the browser window
            # mid-wait, which raises an opaque "Target closed" or similar. Map
            # that to a clearer failure shape so the route can show a useful
            # message instead of a 502.
            err_str = str(exc).lower()
            if "target" in err_str and "closed" in err_str:
                return OfferResult(
                    success=False,
                    offer_ref=None,
                    screenshot_path=screenshot,
                    error_message="Browser was closed before the offer was sent. Click Send Browser to retry.",
                )
            raise EbayBrowserError(
                f"Unexpected error during BO placement: {exc}",
                screenshot_path=screenshot,
            ) from exc
        finally:
            # Persistent context flushes session state on close.
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
