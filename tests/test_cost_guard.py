"""Tests for pricing.cost_guard. Uses tmp_db + real repo writes."""

import pytest

import config
from db import repo
from pricing import cost_guard


@pytest.fixture
def search_id(tmp_db) -> int:
    return repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=100.0
    )


def test_under_budget_true_when_no_spend(search_id, monkeypatch):
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.50)
    assert cost_guard.under_budget(search_id, planned_cost_usd=0.10) is True


def test_under_budget_false_when_planned_exceeds_cap(search_id, monkeypatch):
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.05)
    assert cost_guard.under_budget(search_id, planned_cost_usd=0.10) is False


def test_under_budget_accounts_for_prior_spend(search_id, monkeypatch):
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.20)
    repo.add_reference_price(search_id, "google_shopping", "new", 1.0, 1.0, 1.0, [], cost_usd=0.18)
    # 0.18 already spent + 0.05 planned = 0.23 > 0.20 cap → False
    assert cost_guard.under_budget(search_id, planned_cost_usd=0.05) is False
    # 0.18 + 0.01 = 0.19 ≤ 0.20 → True
    assert cost_guard.under_budget(search_id, planned_cost_usd=0.01) is True


def test_under_budget_is_scoped_per_search(tmp_db, monkeypatch):
    """One search exhausting its budget must not block another search."""
    monkeypatch.setattr(config, "APIFY_BUDGET_USD", 0.10)
    a = repo.create_search(criteria_nl="a", criteria_structured={"title_keywords": "a"}, max_price=10.0)
    b = repo.create_search(criteria_nl="b", criteria_structured={"title_keywords": "b"}, max_price=10.0)
    repo.add_reference_price(a, "google_shopping", "new", 1.0, 1.0, 1.0, [], cost_usd=0.10)

    assert cost_guard.under_budget(a, planned_cost_usd=0.01) is False
    assert cost_guard.under_budget(b, planned_cost_usd=0.05) is True
