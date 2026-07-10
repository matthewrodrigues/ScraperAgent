"""One-time eBay browser-session capture (persistent Chrome profile mode).

Launches a headed Chrome window with a persistent user-data profile under
`config.EBAY_BROWSER_PROFILE_DIR`. You sign in manually (2FA / CAPTCHA /
device-verification all handled by you in the browser), press Enter when
logged in, and the script verifies + closes — the profile dir IS the saved
state (no separate JSON file). `integrations/ebay_browser.py` reuses the
same profile dir on every offer placement.

Why a Chrome profile instead of a storage_state.json: eBay's anti-bot
detection fingerprints below the cookie layer — TLS handshake, HTTP/2 frame
ordering, font enumeration. A fresh browser context loaded with saved cookies
still gets the degraded session; a real persistent Chrome profile with the
installed Chrome binary does not. See memory
`project_ebay_browser_automation.md` for the full history.

When to re-run:
  * First-time setup
  * After the dashboard surfaces an "eBay session expired" notice
  * If you've recently changed your eBay password

Usage:
    .venv/Scripts/python.exe scratch_ebay_browser_login.py
"""

import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

import config


SIGNIN_URL = "https://signin.ebay.com/"

# Verification URLs we try in order. eBay's auth gating differs per page —
# some bounce-back to signin reliably when unauthenticated, others serve
# limited content. Trying multiple URLs and accepting the first that stays
# off the signin host avoids account-type quirks (brand-new accounts may not
# have a populated My eBay summary yet, etc.).
VERIFY_URLS = [
    "https://www.ebay.com/mys/home",
    "https://www.ebay.com/mye/myebay/summary",
    "https://www.ebay.com/usr/_my",
]


# Anti-fingerprinting init script. eBay (and most sites that take anti-bot
# seriously) check `navigator.webdriver`, a few plugin-related properties,
# and `window.chrome` for telltale Playwright signatures. Setting these via
# `add_init_script` runs the patch before the page's own JS gets a chance
# to read them, which means the values look "normal" from the first frame.
#
# This isn't a full stealth shim — packages like playwright-stealth do more —
# but these three are the highest-signal markers and patching them alone is
# usually enough for site-scoped auth flows. If eBay starts blocking again,
# add the rest of the stealth bag (chrome.runtime, navigator.languages,
# WebGL vendor, etc.).
STEALTH_INIT_SCRIPT = """
// Mask the most-detected Playwright fingerprint markers.
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
window.chrome = window.chrome || { runtime: {} };
"""


def _is_authenticated(final_url: str) -> bool:
    """True if a navigation to a verify URL did NOT bounce to the signin host."""
    host = (urlparse(final_url).hostname or "").lower()
    return not host.startswith("signin.")


def _launch_persistent(p, headless: bool = False):
    """Open the persistent Chrome profile, falling back to bundled Chromium
    if Chrome isn't installed. Same fallback semantics as the integration."""
    try:
        return p.chromium.launch_persistent_context(
            user_data_dir=str(config.EBAY_BROWSER_PROFILE_DIR),
            channel=config.EBAY_BROWSER_CHANNEL or None,
            headless=headless,
        ), True
    except Exception as exc:  # noqa: BLE001
        print(f"  (channel={config.EBAY_BROWSER_CHANNEL!r} not available: {exc})")
        print("  Falling back to Playwright's bundled Chromium.")
        print("  eBay may still detect this; for best results install Chrome Stable.")
        return p.chromium.launch_persistent_context(
            user_data_dir=str(config.EBAY_BROWSER_PROFILE_DIR),
            headless=headless,
        ), False


def main() -> None:
    config.EBAY_BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 72)
    print("eBay browser-session capture")
    print("=" * 72)
    print()
    print("A Chrome window will open in a moment. Sign in to your eBay")
    print("account fully (including 2FA / device verification if prompted).")
    print()
    print(f"Profile directory:")
    print(f"  {config.EBAY_BROWSER_PROFILE_DIR}")
    print()

    with sync_playwright() as p:
        context, used_real_chrome = _launch_persistent(p, headless=False)
        if used_real_chrome:
            print(f"Using installed Chrome via channel={config.EBAY_BROWSER_CHANNEL!r}.")
        # Apply the anti-fingerprint shim before any page loads.
        context.add_init_script(STEALTH_INIT_SCRIPT)

        # Persistent contexts ship with one initial page open; use it instead
        # of creating a new one to avoid a stray about:blank tab.
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(SIGNIN_URL)

        print()
        print("Browser is open. Complete sign-in, then return here and press Enter.")
        print("(If you closed the browser by accident, kill this script with Ctrl-C and re-run.)")
        try:
            input("\nPress Enter when you're fully signed in and looking at the eBay home / account page: ")
        except KeyboardInterrupt:
            print("\nAborted by user.")
            context.close()
            sys.exit(1)

        # Verify auth by navigating to a series of gated pages. The first one
        # that stays off the signin host means we're authenticated.
        print()
        print("Verifying auth by navigating to known-authenticated pages...")
        authenticated = False
        for verify_url in VERIFY_URLS:
            try:
                page.goto(verify_url, wait_until="domcontentloaded", timeout=30_000)
            except Exception as exc:  # noqa: BLE001
                print(f"  {verify_url} -> nav failed: {exc}")
                continue
            final_url = page.url
            if _is_authenticated(final_url):
                print(f"  {verify_url} -> {final_url}  authenticated")
                authenticated = True
                break
            else:
                print(f"  {verify_url} -> {final_url}  bounced to signin")

        if not authenticated:
            print()
            print("AUTH CHECK FAILED — every verify URL bounced to signin.")
            print("Possible causes:")
            print("  1. Sign-in didn't fully complete (2FA / device verification pending).")
            print("  2. eBay's anti-bot detection still flagged the session even with")
            print("     the persistent Chrome profile. This is rarer than the storage-state")
            print("     approach but can still happen on aggressive listing flows.")
            print()
            print("You can still try keeping the profile and seeing if it works for offer")
            print("placement — verify URLs are stricter than the listing page in some cases.")
            try:
                confirm = input("Keep profile anyway? [y/N]: ").strip().lower()
            except KeyboardInterrupt:
                print("\nAborted.")
                context.close()
                sys.exit(1)
            if confirm != "y":
                print("Closing without saving. Re-run when you're fully signed in.")
                context.close()
                sys.exit(1)
        else:
            print("AUTH CHECK PASSED — session is authenticated.")

        # Persistent contexts flush to disk on close — no explicit save needed.
        context.close()
        print()
        print(f"Profile persisted at {config.EBAY_BROWSER_PROFILE_DIR}")
        print("Next: run `scratch_ebay_browser_place.py <listing_url> <amount>` to test placement.")


if __name__ == "__main__":
    main()
