"""Standalone test: full negotiate subgraph end-to-end with real Claude.

Bypasses FastAPI so any LLM-shape errors surface immediately. Seeds a synthetic
search + listing + reference prices, then runs the negotiate graph.
"""

import logging
import textwrap

from agents.graph import build_negotiate_graph
from db import repo


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def main() -> None:
    repo.init_db()
    sid = repo.create_search(
        criteria_nl="sony wh-1000xm5 over-ear headphones",
        criteria_structured={"title_keywords": "sony wh-1000xm5"},
        max_price=300.0,
    )
    repo.update_search_status(sid, "negotiating")
    listing_ids = repo.add_listings(sid, [{
        "ebay_item_id": "v1|smoke|0",
        "title": "Sony WH-1000XM5 Wireless Noise Cancelling Headphones - Black",
        "price": 280.0,  # ~20% above market median → anchor_low expected
        "condition": "Used",
        "seller_rating": 99.4,
        "seller_feedback_count": 1500,
        "url": "https://example.test/smoke",
    }])
    listing_id = listing_ids[0]
    repo.mark_listing_selected(listing_id)
    repo.add_reference_price(sid, "google_shopping", "new", 230.0, 210.0, 270.0, [], 0.05)

    print(f"\nRunning negotiate graph for search {sid}, listing {listing_id}...\n")
    build_negotiate_graph().invoke({"search_id": sid, "listing_id": listing_id})

    pending = repo.get_pending_message_for_search(sid)
    if pending is None:
        n = repo.get_active_negotiation_for_listing(listing_id)
        print("No pending message. Active negotiation:")
        print(f"  status={n['status'] if n else None}")
        return

    print(f"Strategy: {pending['negotiation_strategy']}")
    print(f"Offer:    ${pending['offer_amount']:.2f}")
    print(f"Body:")
    print(textwrap.indent(pending['body'], "  "))
    print()


if __name__ == "__main__":
    main()
