"""Standalone test: hit the real Apify actor for Google Shopping prices.

Bypasses FastAPI/BackgroundTasks so any error surfaces immediately instead of
being swallowed by the request-response cycle.
"""

import logging
import traceback

from agents.criteria_parser import ParsedCriteria
from pricing import google_shopping


logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def main() -> None:
    criteria = ParsedCriteria(
        title_keywords="sony wh-1000xm5 headphones",
        max_price=400.0,
    )
    print(f"Calling Apify actor {google_shopping._ACTOR_ID}...")
    try:
        points, cost = google_shopping.fetch(criteria, max_charge_usd=0.90)
    except google_shopping.PricingSourceError as exc:
        print(f"\nFAILED with PricingSourceError: {exc}")
        traceback.print_exc()
        return
    except Exception as exc:
        print(f"\nFAILED with unexpected exception: {type(exc).__name__}: {exc}")
        traceback.print_exc()
        return

    print(f"\nGot {len(points)} price points at cost ${cost:.4f}")
    for p in points[:5]:
        print(f"  ${p['price']:>7.2f} {p['condition']:>10s}  {p['title'][:60]}")


if __name__ == "__main__":
    main()
