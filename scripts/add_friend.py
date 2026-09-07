"""Issue a broker token to a friend.

The token is shown once and never stored in recoverable form — only its SHA-256
lands in broker.db, so a leaked database holds nothing spendable. Lose the
token and the fix is to revoke and reissue.

Usage:
    python -m scripts.add_friend alice
    python -m scripts.add_friend alice --budget 7.50
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from keybroker import auth, db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue a broker token")
    parser.add_argument("name", help="short handle for the friend, e.g. alice")
    parser.add_argument("--budget", type=float, default=5.0,
                        help="monthly cap in USD (default: 5.00)")
    args = parser.parse_args(argv)

    db.init_db()
    token = auth.generate_token()
    try:
        db.create_friend(args.name, auth.hash_token(token), args.budget)
    except sqlite3.IntegrityError:
        print(f"A friend named {args.name!r} already exists.", file=sys.stderr)
        return 1

    print(f"Friend:  {args.name}")
    print(f"Budget:  ${args.budget:.2f}/month")
    print(f"Token:   {token}")
    print()
    print("Shown once. Have them add to their .env:")
    print(f"  SCRAPERAGENT_BROKER_TOKEN={token}")
    print("  SCRAPERAGENT_BROKER_URL=<your broker URL>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
