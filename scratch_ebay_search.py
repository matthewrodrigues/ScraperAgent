"""One-off smoke test: hits real eBay production and prints a few listings.

Run with `python scratch_ebay_search.py` from repo root. Safe to delete after
the LangGraph node is wired up — kept around for quick manual checks.
"""

from agents.criteria_parser import ParsedCriteria
from browser.ebay import search_ebay


def main() -> None:
    criteria = ParsedCriteria(
        title_keywords="sony wh-1000xm4",
        max_price=200.0,
        min_seller_rating=95.0,
    )
    results = search_ebay(criteria, limit=5)

    print(f"\nGot {len(results)} listing(s):\n")
    for r in results:
        rating = f"{r.seller_rating:.1f}%" if r.seller_rating is not None else "  ? "
        print(f"  ${r.price:>7.2f}  {rating}  {r.title[:60]:<60}  {r.url}")
    print()


if __name__ == "__main__":
    main()
