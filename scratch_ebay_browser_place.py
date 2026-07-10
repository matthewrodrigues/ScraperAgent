"""Standalone test of place_best_offer_via_browser against a real eBay listing.

Use this to verify the saved browser session works AND that the selectors in
`integrations/ebay_browser.py` still match eBay's current offer-page DOM.
Run BEFORE wiring browser automation to the dashboard route — if this script
fails on a real listing, the route would fail the same way.

Usage:
    .venv/Scripts/python.exe scratch_ebay_browser_place.py <listing_url> <amount> [message]

Examples:
    .venv/Scripts/python.exe scratch_ebay_browser_place.py "https://www.ebay.com/itm/206345077177" 35.99
    .venv/Scripts/python.exe scratch_ebay_browser_place.py "https://www.ebay.com/itm/206345077177" 35.99 "Hi, would you consider this offer?"

Prerequisites:
    1. Run scratch_ebay_browser_login.py first to capture the session.
    2. Pick a real BO-eligible listing URL from your search history.
       (Or any listing in your DB with buying_options containing BEST_OFFER.)

What happens:
    A Chromium window opens, navigates to the listing, finds the Make Offer
    button, fills the amount + message, submits. Screenshots are saved to
    config.EBAY_BROWSER_SCREENSHOTS_DIR for every success and every failure
    path so you can see exactly what eBay rendered at each step.

This will place a REAL offer on a REAL listing if it succeeds. Pick something
you'd actually be willing to buy at the offer price.
"""

import sys

from integrations import ebay_browser


def main() -> None:
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print("Usage: scratch_ebay_browser_place.py <listing_url> <amount> [message]")
        sys.exit(1)

    listing_url = sys.argv[1]
    try:
        amount = float(sys.argv[2])
    except ValueError:
        print(f"amount must be a number, got: {sys.argv[2]!r}")
        sys.exit(1)
    message = sys.argv[3] if len(sys.argv) == 4 else ""

    print("=" * 72)
    print("Placing test offer via browser")
    print("=" * 72)
    print(f"Listing: {listing_url}")
    print(f"Amount:  ${amount:.2f}")
    print(f"Message: {message!r}")
    print()
    print("This places a REAL offer on a REAL listing if it succeeds.")
    try:
        confirm = input("Proceed? [y/N]: ").strip().lower()
    except KeyboardInterrupt:
        print("\nAborted.")
        sys.exit(1)
    if confirm != "y":
        print("Aborted by user.")
        sys.exit(0)

    print("\nLaunching Chromium...")
    try:
        result = ebay_browser.place_best_offer_via_browser(listing_url, amount, message)
    except ebay_browser.EbaySessionExpiredError as exc:
        print()
        print("SESSION EXPIRED")
        print(f"  {exc}")
        if exc.screenshot_path:
            print(f"  Screenshot: {exc.screenshot_path}")
        print("\nRun `scratch_ebay_browser_login.py` to refresh the session.")
        sys.exit(2)
    except ebay_browser.EbayBrowserError as exc:
        print()
        print("FAILED")
        print(f"  {exc}")
        if exc.screenshot_path:
            print(f"  Screenshot: {exc.screenshot_path}")
        sys.exit(3)

    print()
    if result.success:
        print("SUCCESS — offer placed")
        if result.offer_ref:
            print(f"  Offer reference: {result.offer_ref}")
        if result.screenshot_path:
            print(f"  Audit screenshot: {result.screenshot_path}")
        print("\nCheck your eBay account under My eBay → Activity → Offers")
        print("to confirm the offer appears there.")
    else:
        print(f"NOT SUCCESS — {result.error_message}")
        if result.screenshot_path:
            print(f"  Screenshot: {result.screenshot_path}")
        sys.exit(4)


if __name__ == "__main__":
    main()
