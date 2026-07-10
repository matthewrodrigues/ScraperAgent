"""Integration smoke: graph → real eBay → real local DB. Safe to delete later."""

from agents.graph import build_graph
from db import repo


def main() -> None:
    repo.init_db()
    sid = repo.create_search(
        criteria_nl="bose nc 700 navy headphones",
        criteria_structured={
            "title_keywords": "bose nc 700 navy",
            "must_not_keywords": [],
            "condition_floor": "used",
            "min_seller_rating": 95.0,
        },
        max_price=300.0,
    )

    build_graph().invoke({"search_id": sid})

    row = repo.get_search(sid)
    print(f"\nSearch #{sid} — status: {row['status']}")
    if row["error_message"]:
        print(f"error: {row['error_message']}")
    for l in repo.list_listings(sid)[:5]:
        rating = f"{l['seller_rating']:.1f}%" if l['seller_rating'] is not None else "  ? "
        print(f"  ${l['price']:>7.2f}  {rating}  {l['title'][:60]}")
    print()


if __name__ == "__main__":
    main()
