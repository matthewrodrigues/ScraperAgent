"""Direct PlaceOffer probe — no DB, no app routes, no extra fields. Just XML in,
XML out, printed verbatim. Lets us iterate on the PlaceOffer envelope without
re-running the whole app.

Usage:
    .venv/Scripts/python.exe scratch_place_offer_debug.py <item_id> <offer_amount>

Example:
    .venv/Scripts/python.exe scratch_place_offer_debug.py 256460558255 35.00

The script tries TWO variants of the PlaceOffer XML against the production
Trading endpoint and shows the full request + full response for each. That
gives us enough material to diagnose what eBay is actually rejecting:

  Variant A: Bare minimum per eBay docs — Action / MaxBid / Quantity inside <Offer>
  Variant B: Same but with BlockOnWarning added (our current production code)

If A passes and B fails, BlockOnWarning is the culprit.
If both fail with the same error, the cause is something more fundamental
(account eligibility, token scope, listing-specific Best Offer config, etc).
"""

import sys

import requests

import config


_ENDPOINT = "https://api.ebay.com/ws/api.dll"
_COMPATIBILITY_LEVEL = "1199"


def _headers(call_name: str) -> dict[str, str]:
    h = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": _COMPATIBILITY_LEVEL,
        "X-EBAY-API-DEV-NAME": config.EBAY_DEV_ID or "",
        "X-EBAY-API-APP-NAME": config.EBAY_APP_ID or "",
        "X-EBAY-API-CERT-NAME": config.EBAY_CERT_ID or "",
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": "0",
        "Content-Type": "text/xml; charset=utf-8",
    }
    token = config.EBAY_USER_TOKEN or ""
    if token.startswith("v^") and "#" in token:
        h["X-EBAY-API-IAF-TOKEN"] = token
    return h


def _build_xml(item_id: str, amount: float, include_block_on_warning: bool) -> str:
    token = config.EBAY_USER_TOKEN or ""
    is_oauth = token.startswith("v^") and "#" in token
    creds = "" if is_oauth else f"<RequesterCredentials><eBayAuthToken>{token}</eBayAuthToken></RequesterCredentials>"
    bow = "<BlockOnWarning>true</BlockOnWarning>" if include_block_on_warning else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<PlaceOfferRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f"{creds}"
        f"<EndUserIP>0.0.0.0</EndUserIP>"
        f"<ItemID>{item_id}</ItemID>"
        f"<Offer>"
        f"<Action>BestOffer</Action>"
        f"{bow}"
        f'<MaxBid currencyID="USD">{amount:.2f}</MaxBid>'
        f"<Quantity>1</Quantity>"
        f"</Offer>"
        "</PlaceOfferRequest>"
    )


def _try(label: str, item_id: str, amount: float, include_bow: bool) -> None:
    print("=" * 72)
    print(f" {label}")
    print("=" * 72)
    xml = _build_xml(item_id, amount, include_bow)
    # Mask token in the printed request so we can paste output to a chat safely.
    token = config.EBAY_USER_TOKEN or ""
    safe_xml = xml.replace(token, "<REDACTED-TOKEN>") if token else xml
    print("REQUEST:")
    print(safe_xml)
    print()
    resp = requests.post(_ENDPOINT, headers=_headers("PlaceOffer"), data=xml.encode("utf-8"), timeout=30)
    print(f"HTTP STATUS: {resp.status_code}")
    print("RESPONSE BODY:")
    print(resp.text)
    print()


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: scratch_place_offer_debug.py <item_id> <offer_amount>")
        print('Example: scratch_place_offer_debug.py 256460558255 35.00')
        sys.exit(1)
    item_id = sys.argv[1]
    amount = float(sys.argv[2])

    if not config.EBAY_USER_TOKEN:
        print("ERROR: EBAY_USER_TOKEN is not set in .env")
        sys.exit(1)

    print(f"Target item: {item_id}    Offer: ${amount:.2f}")
    token_kind = "OAuth (v^...)" if config.EBAY_USER_TOKEN.startswith("v^") else "Legacy AuthnAuth"
    print(f"Token type: {token_kind}")
    print()

    _try("Variant A — minimal (Action, MaxBid, Quantity)", item_id, amount, include_bow=False)
    _try("Variant B — with BlockOnWarning", item_id, amount, include_bow=True)


if __name__ == "__main__":
    main()
