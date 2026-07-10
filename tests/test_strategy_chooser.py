"""Tests for the strategy chooser.

These pin the precedence table (PRD §103-114). When the negotiation results
look wrong in production, the first question is "did we pick the right strategy?"
— and these tests are the answer.
"""

import pytest

import strategies


def _listing(price: float = 200.0, seller_rating: float | None = 99.5, listed_at: str | None = None) -> dict:
    return {"price": price, "seller_rating": seller_rating, "listed_at": listed_at}


def _ref(median: float, p25: float | None = None, p75: float | None = None) -> list[dict]:
    return [{"source": "google_shopping", "condition": "new", "median": median, "p25": p25, "p75": p75}]


# ---- pick_name precedence ------------------------------------------------

def test_seller_below_95_dominates_everything():
    """Even an overpriced listing from a low-rated seller is batna_signal first."""
    s = strategies.pick_strategy(
        _listing(price=400.0, seller_rating=80.0),  # huge gap, but bad seller
        reference_prices=_ref(median=200.0),
        max_price=500.0,
    )
    assert s.name == "batna_signal"


def test_stale_listing_dominates_price_gap():
    """A listing older than 30 days → time_pressure, even if it's overpriced."""
    s = strategies.pick_strategy(
        _listing(price=300.0, seller_rating=99.0, listed_at="2026-04-01T00:00:00Z"),
        reference_prices=_ref(median=200.0),  # 50% over market — would be anchor_low if fresh
        max_price=500.0,
        now_iso="2026-06-17T00:00:00Z",  # ~77 days later
    )
    assert s.name == "time_pressure"


def test_anchor_low_when_overpriced_by_15_percent():
    s = strategies.pick_strategy(
        _listing(price=230.0),  # 15% over $200 median
        reference_prices=_ref(median=200.0, p25=180.0),
        max_price=300.0,
    )
    assert s.name == "anchor_low"


def test_split_the_difference_when_moderately_overpriced():
    """5% over market → split_the_difference."""
    s = strategies.pick_strategy(
        _listing(price=210.0),
        reference_prices=_ref(median=200.0),
        max_price=300.0,
    )
    assert s.name == "split_the_difference"


def test_time_pressure_when_already_at_or_below_market():
    s = strategies.pick_strategy(
        _listing(price=180.0),  # below median 200
        reference_prices=_ref(median=200.0),
        max_price=300.0,
    )
    assert s.name == "time_pressure"


def test_no_reference_prices_defaults_to_split_the_difference():
    """Apify failed → no anchor → moderate compromise default."""
    s = strategies.pick_strategy(_listing(price=200.0), reference_prices=[], max_price=300.0)
    assert s.name == "split_the_difference"


def test_reference_with_null_median_treated_as_missing():
    s = strategies.pick_strategy(
        _listing(price=200.0),
        reference_prices=[{"source": "x", "median": None, "p25": None, "p75": None}],
        max_price=300.0,
    )
    assert s.name == "split_the_difference"


# ---- offer amount math ---------------------------------------------------

def test_anchor_low_uses_p25_when_available():
    s = strategies.pick_strategy(
        _listing(price=300.0),
        reference_prices=_ref(median=240.0, p25=210.0),
        max_price=500.0,
    )
    # p25=210 > 80%-of-asking=240 floor, so floor wins → 240. Both ≤ max_price 500.
    assert s.name == "anchor_low"
    assert s.offer_amount == pytest.approx(240.0)


def test_anchor_low_uses_floor_when_p25_below_it():
    """If p25 is wildly below 80% of asking, floor to 80% to avoid insult."""
    s = strategies.pick_strategy(
        _listing(price=300.0),
        reference_prices=_ref(median=240.0, p25=50.0),  # absurdly low p25
        max_price=500.0,
    )
    assert s.name == "anchor_low"
    assert s.offer_amount == pytest.approx(240.0)  # 300 * 0.80


def test_split_the_difference_midpoints():
    """midpoint of (asking, ref_median * 0.9) — given asking=210, median=200,
    target=180, midpoint=195."""
    s = strategies.pick_strategy(
        _listing(price=210.0),
        reference_prices=_ref(median=200.0),
        max_price=300.0,
    )
    assert s.name == "split_the_difference"
    assert s.offer_amount == pytest.approx(195.0)


def test_time_pressure_offers_token_discount():
    s = strategies.pick_strategy(
        _listing(price=180.0),
        reference_prices=_ref(median=200.0),
        max_price=300.0,
    )
    assert s.name == "time_pressure"
    assert s.offer_amount == pytest.approx(171.0)  # 180 * 0.95


def test_batna_signal_offers_steep_discount():
    s = strategies.pick_strategy(
        _listing(price=200.0, seller_rating=82.0),
        reference_prices=_ref(median=200.0),
        max_price=500.0,
    )
    assert s.name == "batna_signal"
    assert s.offer_amount == pytest.approx(150.0)  # 200 * 0.75


def test_offer_amount_is_never_above_max_price():
    """Even if the strategy's heuristic says higher, max_price is a hard ceiling."""
    s = strategies.pick_strategy(
        _listing(price=300.0),
        reference_prices=_ref(median=400.0),  # would push median-based target up
        max_price=100.0,  # but buyer's ceiling is way below
    )
    assert s.offer_amount <= 100.0


# ---- audit trail ---------------------------------------------------------

def test_inputs_dict_captures_all_signals():
    """Every choice should record the signals that produced it for later review."""
    s = strategies.pick_strategy(
        _listing(price=250.0, seller_rating=99.5, listed_at="2026-06-01T00:00:00Z"),
        reference_prices=_ref(median=200.0, p25=180.0),
        max_price=300.0,
        now_iso="2026-06-17T00:00:00Z",
    )
    assert s.inputs["asking"] == 250.0
    assert s.inputs["ref_median"] == 200.0
    assert s.inputs["ref_p25"] == 180.0
    assert s.inputs["seller_rating"] == 99.5
    assert s.inputs["max_price"] == 300.0
    assert s.inputs["gap_fraction"] == pytest.approx(0.25)
    assert s.inputs["listing_age_days"] == pytest.approx(16.0)


def test_unparseable_listed_at_treated_as_unknown_age():
    s = strategies.pick_strategy(
        _listing(price=210.0, listed_at="not a date"),
        reference_prices=_ref(median=200.0),
        max_price=300.0,
    )
    # Falls through to gap-based rules; gap 5% → split_the_difference
    assert s.name == "split_the_difference"
    assert s.inputs["listing_age_days"] is None


def test_strategy_carries_its_system_prompt():
    """The chosen Strategy must include the prompt the drafter will use —
    otherwise the drafter has no way to find it from just the name."""
    s = strategies.pick_strategy(_listing(price=300.0), _ref(median=200.0), max_price=500.0)
    assert s.name == "anchor_low"
    assert "anchor" in s.system_prompt.lower()
    # Constitutional rules are baked into every prompt:
    assert "max_price" in s.system_prompt
