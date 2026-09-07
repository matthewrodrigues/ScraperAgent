"""Spend caps.

Metering happens after a response arrives, so a naive "spent < budget" check
would let the final call cross the line. Reserving the worst-case single call
closes that: the last permitted call lands at or under the budget, so the owner
sets the true ceiling and this module does the subtraction.

MAX_SINGLE_CALL_USD is only valid while keybroker.clamps holds max_tokens to
4096 and the request body to 256 KB. Change either and recompute this.
"""

from typing import Any

import config
from keybroker import db


MAX_SINGLE_CALL_USD = 0.30


class QuotaExceeded(Exception):
    """Raised when a request would exceed a per-friend or global cap."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def remaining_usd(friend: dict[str, Any]) -> float:
    """Budget left this month, floored at zero. Used to clamp Apify runs."""
    spent = db.friend_month_spend(friend["id"])
    return max(0.0, float(friend["monthly_budget_usd"]) - spent)


def check(friend: dict[str, Any]) -> None:
    """Raise QuotaExceeded when either cap lacks room for one more call."""
    budget = float(friend["monthly_budget_usd"])
    spent = db.friend_month_spend(friend["id"])
    if spent + MAX_SINGLE_CALL_USD > budget:
        raise QuotaExceeded(
            f"{friend['name']} has spent ${spent:.2f} of a ${budget:.2f} "
            f"monthly budget; no headroom for another call."
        )

    global_budget = float(config.BROKER_GLOBAL_MONTHLY_BUDGET_USD)
    global_spent = db.global_month_spend()
    if global_spent + MAX_SINGLE_CALL_USD > global_budget:
        raise QuotaExceeded(
            f"Global monthly cap reached: ${global_spent:.2f} of "
            f"${global_budget:.2f} spent across all friends."
        )
