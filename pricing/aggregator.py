"""Pure aggregation helpers for reference price points.

Kept dependency-free (stdlib only) so unit tests don't need any mocks. The node
that calls these is responsible for IO; this module only does math.
"""

import statistics
from collections.abc import Iterable
from typing import Any


def by_condition(points: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Group price points by their `condition` field.

    Items missing/empty condition are bucketed as "new" — Google Shopping is a
    new-goods aggregator, so absent-condition almost always means new in practice.
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for pt in points:
        cond = (pt.get("condition") or "new").lower()
        buckets.setdefault(cond, []).append(pt)
    return buckets


def percentiles(points: Iterable[dict[str, Any]]) -> dict[str, float | None]:
    """Compute median, p25, p75 of `price` over the given points.

    Returns `None` for any percentile that can't be computed (empty input).
    With a single point, all three percentiles equal that point's price —
    that's fine; the caller is free to ignore aggregates below a sample-size threshold.
    """
    prices = [float(p["price"]) for p in points if p.get("price") is not None]
    if not prices:
        return {"median": None, "p25": None, "p75": None}
    if len(prices) == 1:
        v = prices[0]
        return {"median": v, "p25": v, "p75": v}
    prices.sort()
    median = statistics.median(prices)
    # statistics.quantiles with n=4 returns [Q1, Q2, Q3]; needs >= 2 datapoints.
    q1, _, q3 = statistics.quantiles(prices, n=4, method="inclusive")
    return {"median": median, "p25": q1, "p75": q3}
