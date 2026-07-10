"""Tests for pricing.aggregator. Pure-math; no fixtures needed."""

import pytest

from pricing import aggregator


def test_by_condition_groups_correctly():
    points = [
        {"price": 10.0, "condition": "new"},
        {"price": 15.0, "condition": "used"},
        {"price": 11.0, "condition": "new"},
    ]
    out = aggregator.by_condition(points)
    assert sorted(out.keys()) == ["new", "used"]
    assert len(out["new"]) == 2
    assert len(out["used"]) == 1


def test_by_condition_defaults_missing_condition_to_new():
    """Google Shopping rarely reports condition; treat missing as 'new'."""
    points = [{"price": 10.0}, {"price": 11.0, "condition": ""}, {"price": 12.0, "condition": None}]
    out = aggregator.by_condition(points)
    assert list(out.keys()) == ["new"]
    assert len(out["new"]) == 3


def test_by_condition_normalizes_case():
    points = [{"price": 10.0, "condition": "NEW"}, {"price": 11.0, "condition": "New"}]
    out = aggregator.by_condition(points)
    assert list(out.keys()) == ["new"]


def test_percentiles_empty_returns_none_triple():
    assert aggregator.percentiles([]) == {"median": None, "p25": None, "p75": None}


def test_percentiles_single_point_equals_itself():
    out = aggregator.percentiles([{"price": 42.5}])
    assert out == {"median": 42.5, "p25": 42.5, "p75": 42.5}


def test_percentiles_skips_missing_prices():
    out = aggregator.percentiles([{"price": 10.0}, {"price": None}, {"price": 20.0}])
    # Only two valid points → median = 15, p25/p75 from those two
    assert out["median"] == 15.0
    assert out["p25"] == pytest.approx(12.5)
    assert out["p75"] == pytest.approx(17.5)


def test_percentiles_typical_distribution():
    prices = [{"price": p} for p in [100, 110, 120, 130, 140, 150, 160, 170, 180, 190]]
    out = aggregator.percentiles(prices)
    assert out["median"] == pytest.approx(145.0)
    assert out["p25"] == pytest.approx(122.5)
    assert out["p75"] == pytest.approx(167.5)


def test_percentiles_all_same_price():
    out = aggregator.percentiles([{"price": 50.0}] * 5)
    assert out == {"median": 50.0, "p25": 50.0, "p75": 50.0}
