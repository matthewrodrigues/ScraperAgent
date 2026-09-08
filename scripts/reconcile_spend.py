"""Settle provisional Apify spend rows against Apify's final usage figures.

Why this exists: Apify's `usageTotalUsd` is not final at the moment a run first
reports a terminal status. It settles some seconds later. Two real runs read
$0.032 and $0.000 when the broker saw them go terminal, and $0.492 each when
re-fetched afterwards — a 15x under-meter, which is the one direction a spend
cap cannot survive. So the broker never meters from that reading. It debits the
friend the *ceiling* it clamped the run to the moment the run is created
(`provisional = 1`), which is the most the run can possibly cost, and this
script hands back the difference once the real figure exists.

Run it periodically (a scheduled task every 15 minutes is plenty). Until it
runs, every unreconciled Apify run is charged at its worst case, so friends see
less headroom than they really have — never more.

This talks to Apify with the owner's own `APIFY_TOKEN` directly rather than
through the broker: it is owner-side maintenance, not a friend's request, and
routing it through the proxy would meter the lookup as the friend's spend.

Usage:
    python -m scripts.reconcile_spend
    python -m scripts.reconcile_spend --dry-run
    python -m scripts.reconcile_spend --min-age-seconds 600

`--min-age-seconds` (default 120) is the settling delay: rows younger than this
are left alone, because re-fetching them would just record the same premature
figure the broker already refused to trust.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Callable

import httpx

import config
from keybroker import db, meter


log = logging.getLogger(__name__)

# How long after run creation Apify's usageTotalUsd can be trusted. Empirical,
# with generous margin: the observed settling was within a few seconds.
DEFAULT_MIN_AGE_SECONDS = 120

APIFY_RUN_URL = "https://api.apify.com/v2/actor-runs/{run_id}"


def fetch_run(run_id: str, token: str) -> dict[str, Any] | None:
    """Fetch one run object from Apify. Returns the `data` object, or None."""
    try:
        response = httpx.get(
            APIFY_RUN_URL.format(run_id=run_id),
            headers={"authorization": f"Bearer {token}"},
            timeout=30.0,
        )
    except httpx.HTTPError as exc:
        log.warning("could not fetch run %s: %s", run_id, exc)
        return None
    if response.status_code >= 400:
        log.warning("run %s: Apify returned %s", run_id, response.status_code)
        return None
    try:
        payload = response.json()
    except ValueError:
        log.warning("run %s: Apify returned a non-JSON body", run_id)
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else None


def reconcile(
    token: str,
    *,
    min_age_seconds: int = DEFAULT_MIN_AGE_SECONDS,
    dry_run: bool = False,
    fetch: Callable[[str, str], dict[str, Any] | None] | None = None,
) -> tuple[int, float]:
    """Settle eligible provisional rows. Returns (rows settled, net $ change)."""
    # Resolved here, not as a default argument, so the module global is looked
    # up at call time and tests can substitute a stub.
    fetch = fetch or fetch_run
    rows = db.list_provisional_spend("apify", min_age_seconds=min_age_seconds)
    settled = 0
    net_change = 0.0

    for row in rows:
        run_id = row["upstream_ref"]
        data = fetch(run_id, token)
        if data is None:
            continue
        if data.get("status") not in meter.TERMINAL_RUN_STATUSES:
            # Still running: the provisional debit is doing its job. Leave it.
            log.info("run %s is %s; leaving provisional", run_id, data.get("status"))
            continue
        usage = data.get("usageTotalUsd")
        if usage is None:
            log.warning("run %s is terminal but carries no usageTotalUsd", run_id)
            continue

        actual = float(usage)
        delta = actual - float(row["cost_usd"])
        print(
            f"{'would settle' if dry_run else 'settled'} {run_id}: "
            f"{row['cost_usd']:.4f} -> {actual:.4f} ({delta:+.4f})"
        )
        if not dry_run:
            db.settle_spend(row["id"], actual)
        settled += 1
        net_change += delta

    return settled, net_change


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--min-age-seconds", type=int, default=DEFAULT_MIN_AGE_SECONDS,
        help="settling delay; rows younger than this are skipped",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print what would change without writing",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    token = config.APIFY_TOKEN or ""
    if not token:
        # Non-zero so a scheduled task shows this as a failure rather than
        # silently reporting "0 rows settled" forever.
        log.error("APIFY_TOKEN is not set; cannot re-fetch runs from Apify.")
        return 1

    db.init_db()
    settled, net_change = reconcile(
        token, min_age_seconds=args.min_age_seconds, dry_run=args.dry_run
    )

    verb = "would settle" if args.dry_run else "settled"
    print(f"{verb} {settled} provisional row(s); net change ${net_change:+.4f}")
    if args.dry_run:
        print("(dry run — nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
