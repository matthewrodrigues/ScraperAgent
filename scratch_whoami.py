"""Diagnostic: call eBay GetUser to confirm which account EBAY_USER_TOKEN belongs to.

When PlaceOffer returns 200 but the offer doesn't show up on the account you're
logged into in the browser, the most common cause is a token / account mismatch:
the token's user is not the user you're viewing eBay as. Run this script once
to confirm.

Usage:
    .venv/Scripts/python.exe scratch_whoami.py

Prints the eBay username, email, and registration date for whoever owns
EBAY_USER_TOKEN. Compare that against the account you have open in the browser.
"""

import config
from integrations.ebay_trading import _post


def main() -> None:
    if not config.EBAY_USER_TOKEN:
        print("ERROR: EBAY_USER_TOKEN is not set in .env")
        return

    # GetUser with no UserID argument returns the *authenticated* user — i.e.
    # the owner of the token. Cheapest possible Trading call.
    root = _post("GetUser", "")
    username = root.findtext("User/UserID") or "(unknown)"
    email = root.findtext("User/Email") or "(hidden by eBay — normal)"
    registered = root.findtext("User/RegistrationDate") or "(unknown)"
    site = root.findtext("User/Site") or "(unknown)"
    feedback = root.findtext("User/FeedbackScore") or "(unknown)"

    print("=" * 60)
    print("eBay token owner — this is the account PlaceOffer acts on")
    print("=" * 60)
    print(f"  Username:           {username}")
    print(f"  Email:              {email}")
    print(f"  Registered:         {registered}")
    print(f"  Site:               {site}")
    print(f"  Feedback score:     {feedback}")
    print("=" * 60)
    print("If this username is NOT the eBay account you're logged into in")
    print("the browser, your offers are being placed on the OTHER account.")


if __name__ == "__main__":
    main()
