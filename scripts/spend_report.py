"""Month-to-date broker spend, per friend.

Usage:
    python -m scripts.spend_report
    python -m scripts.spend_report --month 2026-08
"""

from __future__ import annotations

import argparse
import sys

import config
from keybroker import db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Broker spend report")
    parser.add_argument("--month", help="YYYY-MM (default: current UTC month)")
    args = parser.parse_args(argv)

    db.init_db()
    friends = db.list_friends()
    if not friends:
        print("No friends yet. Issue a token with: python -m scripts.add_friend <name>")
        return 0

    start, _ = db.month_bounds(args.month)
    print(f"Spend for {start[:7]}")
    print(f"{'friend':<16}{'spent':>10}{'budget':>10}  status")
    for friend in friends:
        spent = db.friend_month_spend(friend["id"], args.month)
        status = "revoked" if friend["revoked_at"] else "active"
        print(f"{friend['name']:<16}{spent:>10.2f}{friend['monthly_budget_usd']:>10.2f}  {status}")

    total = db.global_month_spend(args.month)
    cap = float(config.BROKER_GLOBAL_MONTHLY_BUDGET_USD)
    print(f"{'TOTAL':<16}{total:>10.2f}{cap:>10.2f}  global cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
