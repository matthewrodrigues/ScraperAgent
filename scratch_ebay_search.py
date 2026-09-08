"""Diagnostic: run a real eBay search through the Apify actor.

Why this exists: the actor's output shape is a third-party contract. If
`delicious_zebu` renames a field, the mapping degrades silently — a renamed
`seller_feedback_percent` is caught at runtime (the search warns), but a
renamed `image_url` just becomes None. Running this after any actor update is
how you find out.

This spends real money — up to $0.15 per run, since the actor has no
input-level result cap and a broad query reaches the full budget ceiling.

Usage:
    .venv/Scripts/python.exe scratch_ebay_search.py "sony wh-1000xm5" --max-price 250
"""

from __future__ import annotations

import argparse
import json
import sys

from agents.criteria_parser import ParsedCriteria
from integrations import ebay_search


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live eBay search smoke test")
    parser.add_argument("keywords")
    parser.add_argument("--max-price", type=float, default=None)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--raw", action="store_true", help="dump the first raw item")
    args = parser.parse_args(argv)

    criteria = ParsedCriteria(
        title_keywords=args.keywords,
        must_not_keywords=[],
        condition_floor=None,
        max_price=args.max_price,
        min_seller_rating=None,
    )

    print("actor input:", json.dumps(ebay_search._build_actor_input(criteria), indent=2))
    try:
        result = ebay_search.search_ebay(criteria, limit=args.limit)
    except ebay_search.EbaySearchError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1

    print(f"\ncost: ${result.cost_usd:.4f}   listings: {len(result.listings)}")
    if result.warning:
        print(f"WARNING: {result.warning}")

    for listing in result.listings:
        ship = "?" if listing.shipping_cost is None else f"{listing.shipping_cost:.2f}"
        rating = "?" if listing.seller_rating is None else f"{listing.seller_rating}%"
        print(f"  ${listing.price:>8.2f} +{ship:>6}  {rating:>7} {listing.seller_id or '?':<20} {listing.title[:50]}")

    # Field-by-field presence check: this is the actual point of the script.
    if result.listings:
        first = result.listings[0]
        missing = [f for f in ("shipping_cost", "condition", "seller_id",
                               "seller_rating", "seller_feedback_count", "image_url")
                   if getattr(first, f) is None]
        if missing:
            print(f"\nfields absent on the first listing: {', '.join(missing)}")
            print("If a field is absent across ALL listings, the actor's output shape may have changed.")
        if args.raw:
            print("\nraw item:", json.dumps(first.raw_data, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
