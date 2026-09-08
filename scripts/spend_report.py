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
    any_provisional = False
    for friend in friends:
        spent = db.friend_month_spend(friend["id"], args.month)
        # A provisional row is the run's clamped ceiling, not what it cost. It
        # must be marked, or the owner reads a worst case as a real bill.
        provisional = db.friend_month_provisional(friend["id"], args.month)
        mark = "*" if provisional else " "
        any_provisional = any_provisional or bool(provisional)
        status = "revoked" if friend["revoked_at"] else "active"
        print(
            f"{friend['name']:<16}{spent:>9.2f}{mark}"
            f"{friend['monthly_budget_usd']:>10.2f}  {status}"
        )

    total = db.global_month_spend(args.month)
    cap = float(config.BROKER_GLOBAL_MONTHLY_BUDGET_USD)
    print(f"{'TOTAL':<16}{total:>9.2f} {cap:>10.2f}  global cap")

    if any_provisional:
        print()
        print("* includes provisional Apify rows: worst-case estimates charged at")
        print("  the run's clamped ceiling, pending reconciliation. Settle them with:")
        print("      python -m scripts.reconcile_spend")
    return 0


if __name__ == "__main__":
    sys.exit(main())
