"""Per-search Apify budget enforcement.

Single check used before every actor call: would launching this run push our
spend past `config.APIFY_BUDGET_USD` for this search? If so, skip the call.

Today there's only one source (Google Shopping, ~$0.02 per run), so the budget
will never bite. Wiring this in now means adding amazon/walmart/etc. later is a
drop-in — they all go through the same guard.
"""

import config
from db import repo


def spent_so_far(search_id: int) -> float:
    """Every Apify dollar this search has already committed — discovery plus
    reference pricing. Discovery used to be free (Browse API), so it was not
    counted; it is now the first thing a search spends."""
    return repo.sum_search_cost(search_id) + repo.sum_apify_cost(search_id)


def remaining_budget(search_id: int) -> float:
    """What is left of APIFY_BUDGET_USD for this search, floored at zero.

    Callers pass this to Apify as the run's `max_total_charge_usd`, which is
    what makes the budget a cap the vendor enforces rather than an estimate we
    check and then exceed."""
    return max(0.0, config.APIFY_BUDGET_USD - spent_so_far(search_id))


def under_budget(search_id: int, planned_cost_usd: float) -> bool:
    """True if launching a `planned_cost_usd` call would stay under the cap."""
    return spent_so_far(search_id) + planned_cost_usd <= config.APIFY_BUDGET_USD
