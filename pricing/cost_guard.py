"""Per-search Apify budget enforcement.

Single check used before every actor call: would launching this run push our
spend past `config.APIFY_BUDGET_USD` for this search? If so, skip the call.

Today there's only one source (Google Shopping, ~$0.02 per run), so the budget
will never bite. Wiring this in now means adding amazon/walmart/etc. later is a
drop-in — they all go through the same guard.
"""

import config
from db import repo


def under_budget(search_id: int, planned_cost_usd: float) -> bool:
    """True if launching a `planned_cost_usd` call would stay under the cap."""
    spent = repo.sum_apify_cost(search_id)
    return spent + planned_cost_usd <= config.APIFY_BUDGET_USD
