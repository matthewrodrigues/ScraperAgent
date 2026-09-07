"""Revoke a friend's broker token.

Sets revoked_at rather than deleting, so their spend history survives for your
own accounting. Revocation takes effect on the next request.

Usage:
    python -m scripts.revoke_friend alice
"""

from __future__ import annotations

import argparse
import sys

from keybroker import db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revoke a broker token")
    parser.add_argument("name")
    args = parser.parse_args(argv)

    db.init_db()
    if not db.revoke_friend(args.name):
        print(f"No active friend named {args.name!r}.", file=sys.stderr)
        return 1
    print(f"Revoked {args.name}. Their spend history is retained.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
