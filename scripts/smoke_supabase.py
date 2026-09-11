"""Diagnostic: exercise the broker's Postgres backend against the real project.

Why this exists: the test suite runs on SQLite, so three things are never
exercised by CI — the Postgres DDL, the AT TIME ZONE 'UTC' month comparison,
and information_schema-based column migrations. All three are Postgres-only by
construction. This script is what checks them against reality before a deploy.

It writes to whatever SUPABASE_DB_URL points at, then cleans up after itself.
The throwaway friend it creates is prefixed 'smoke-' and deleted at the end.

Usage:
    .venv/Scripts/python.exe -m scripts.smoke_supabase
"""

from __future__ import annotations

import argparse
import sys
import uuid
from urllib.parse import urlparse

import config
from keybroker import db, dialect


def _safe_target(url: str) -> str:
    """Host and database only. Never the user or password — this output gets
    pasted into issues and chat logs."""
    parts = urlparse(url)
    return f"{parts.hostname}{parts.path}"


def _run_checks(marker: str) -> None:
    print(f"  dialect:        {dialect.active().name}")

    db.init_db()
    print("  schema:         created or already present")

    fid = db.create_friend(marker, f"hash-{marker}", 1.00)
    print(f"  create_friend:  id={fid}")

    db.record_spend(fid, "apify", 0.90, upstream_ref=f"run-{marker}", provisional=True)
    prov = db.friend_month_provisional(fid)
    assert prov == 0.90, f"provisional should be 0.90, got {prov}"
    print(f"  provisional:    ${prov:.2f}")

    row = db.get_spend_by_ref("apify", f"run-{marker}")
    db.settle_spend(row["id"], 0.53)
    spent = db.friend_month_spend(fid)
    assert abs(spent - 0.53) < 1e-9, f"settled spend should be 0.53, got {spent}"
    print(f"  settled:        ${spent:.2f}")

    # The check that only a real Postgres run can make: a row written "now"
    # must fall inside the current UTC month and outside a past one. A session
    # timezone leak shows up here and nowhere else.
    this_month = db.month_bounds()[0][:7]
    assert db.friend_month_spend(fid, month=this_month) == spent, "row missed the current month"
    assert db.friend_month_spend(fid, month="2020-01") == 0.0, "row leaked into a past month"
    print(f"  month bounds:   correct for {this_month}")

    assert db.revoke_friend(marker) is True
    assert db.get_friend_by_token_hash(f"hash-{marker}") is None
    print("  revoke:         token no longer resolves")


def _cleanup(marker: str) -> None:
    """Remove the throwaway friend and its spend.

    Called from a finally, NOT inline after the assertions: this script writes
    to the PRODUCTION project, so a failed check must not strand a row there.
    Cleanup that only runs on success is cleanup that runs exactly when it is
    least needed.
    """
    try:
        with db.get_conn() as conn:
            conn.execute(db._q("DELETE FROM friends WHERE name = ?"), (marker,))
            if dialect.active().name == "postgres":
                conn.commit()
        print("  cleanup:        throwaway friend and its spend removed")
    except Exception as exc:
        print(f"  cleanup FAILED — remove friend '{marker}' by hand: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the broker's Postgres backend")
    parser.parse_args(argv)

    if not config.SUPABASE_DB_URL:
        print(
            "SUPABASE_DB_URL is not set, so the broker is using SQLite and there "
            "is nothing to smoke-test. Set it in .env first.",
            file=sys.stderr,
        )
        return 1

    print(f"target: {_safe_target(config.SUPABASE_DB_URL)}")
    marker = f"smoke-{uuid.uuid4().hex[:8]}"
    try:
        _run_checks(marker)
    except AssertionError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        _cleanup(marker)
        dialect.active().close_pool()

    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
