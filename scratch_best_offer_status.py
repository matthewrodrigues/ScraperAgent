"""Diagnostic: dump the GetBestOffers state for every offer placed via PlaceOffer.

Background: after PlaceOffer returns Ack=Success with a BestOfferID, the offer
should appear on eBay's Offers page for the buyer account. If it doesn't, three
likely causes:
  1. Seller's auto-decline threshold rejected the offer instantly. Offer still
     exists on eBay's side — under "Declined" — but doesn't show in default views.
  2. Offer is genuinely Pending but you're looking in the wrong eBay UI tab.
  3. eBay placed the offer but on a different account than expected.

This script answers (1) and (2) directly by asking eBay what state each
persisted BestOfferID is in. (3) is what scratch_whoami.py is for.

Usage:
    .venv/Scripts/python.exe scratch_best_offer_status.py
"""

import sqlite3

import config
from integrations import ebay_trading


def main() -> None:
    if not config.EBAY_USER_TOKEN:
        print("ERROR: EBAY_USER_TOKEN is not set")
        return

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT n.id AS negotiation_id, n.ebay_best_offer_id, n.status AS neg_status,
               n.current_offer, l.ebay_item_id, l.title, l.price AS asking_price
          FROM negotiations n
          JOIN listings l ON l.id = n.listing_id
         WHERE n.ebay_best_offer_id IS NOT NULL
         ORDER BY n.id DESC
        """
    ).fetchall()

    if not rows:
        print("No PlaceOffer calls recorded in the DB (no negotiations.ebay_best_offer_id).")
        print("If you just sent an offer, check that the send route succeeded.")
        return

    print(f"Found {len(rows)} placed offer(s). Querying eBay for current state...\n")

    for r in rows:
        print("=" * 72)
        print(f"Negotiation #{r['negotiation_id']} on '{r['title'][:60]}'")
        print(f"  Asking: ${r['asking_price']:.2f}  |  Our offer: ${r['current_offer']:.2f}")
        print(f"  eBay BestOfferID: {r['ebay_best_offer_id']}")
        print(f"  Our DB status: {r['neg_status']}")
        try:
            status = ebay_trading.get_best_offer_status(
                r["ebay_item_id"], r["ebay_best_offer_id"]
            )
            print(f"  eBay says:    status={status['status']}")
            if status.get("counter_amount") is not None:
                print(f"                counter_amount=${status['counter_amount']:.2f}")
            if status.get("seller_message"):
                print(f"                seller_message={status['seller_message']!r}")
        except ebay_trading.EbayTradingError as exc:
            print(f"  eBay error querying this offer: {exc}")
        print()

    print("Status meanings:")
    print("  Pending   — offer is live; seller hasn't responded yet")
    print("  Accepted  — seller accepted; this is a closed deal")
    print("  Declined  — seller declined (often by auto-decline threshold)")
    print("  Countered — seller sent a counter-offer back")
    print("  Expired   — offer aged out before seller responded")
    print("  Retracted — buyer (you) cancelled the offer")
    print("  Unknown   — eBay couldn't locate the id; investigate further")


if __name__ == "__main__":
    main()
