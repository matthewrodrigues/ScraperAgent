"""Tests for pricing.google_shopping. Mocks the Apify client at the module level."""

from unittest.mock import MagicMock, patch

import pytest

import config
from agents.criteria_parser import ParsedCriteria
from pricing import google_shopping


@pytest.fixture(autouse=True)
def _reset_client():
    google_shopping._client = None
    yield
    google_shopping._client = None


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", "test-apify-token")


def _make_run(status: str = "SUCCEEDED", usage: float = 0.03, dataset_id: str = "ds-1") -> dict:
    return {"status": status, "defaultDatasetId": dataset_id, "usageUsd": usage}


def _mock_client(run: dict, items: list[dict]) -> MagicMock:
    client = MagicMock()
    client.actor.return_value.call.return_value = run
    client.dataset.return_value.iterate_items.return_value = iter(items)
    return client


def test_fetch_returns_parsed_points_and_cost(configured):
    items = [
        {"title": "Sony WH-1000XM5", "price": 278.99, "currency": "USD", "productUrl": "https://x/1"},
        {"title": "Bose 700", "priceValue": 249.50, "productUrl": "https://x/2"},
    ]
    fake_client = _mock_client(_make_run(usage=0.04), items)
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        points, cost = google_shopping.fetch(ParsedCriteria(title_keywords="headphones", max_price=300.0))

    assert cost == 0.04
    assert [p["price"] for p in points] == [278.99, 249.5]
    assert points[0]["url"] == "https://x/1"


def test_fetch_sends_search_query_country_language_limit(configured):
    fake_client = _mock_client(_make_run(), [])
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        google_shopping.fetch(ParsedCriteria(title_keywords="sony wh-1000xm5", max_price=250.0))

    call_kwargs = fake_client.actor.return_value.call.call_args.kwargs
    run_input = call_kwargs["run_input"]
    assert run_input["searchQuery"] == "sony wh-1000xm5"
    assert run_input["country"] == "us"
    assert run_input["language"] == "en"
    assert run_input["limit"] == 25
    assert run_input["maxPrice"] == 250.0


def test_fetch_omits_max_price_when_none(configured):
    fake_client = _mock_client(_make_run(), [])
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        google_shopping.fetch(ParsedCriteria(title_keywords="x", max_price=None))
    run_input = fake_client.actor.return_value.call.call_args.kwargs["run_input"]
    assert "maxPrice" not in run_input


def test_fetch_uses_correct_actor_id(configured):
    fake_client = _mock_client(_make_run(), [])
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        google_shopping.fetch(ParsedCriteria(title_keywords="x"))
    fake_client.actor.assert_called_once_with("burbn/google-shopping-scraper")


def test_fetch_drops_items_without_price(configured):
    items = [
        {"title": "with price", "price": 100.0},
        {"title": "no price field"},
        {"title": "garbage price", "price": "not a number"},
    ]
    fake_client = _mock_client(_make_run(), items)
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        points, _ = google_shopping.fetch(ParsedCriteria(title_keywords="x"))
    assert [p["title"] for p in points] == ["with price"]


def test_fetch_extracts_price_from_string(configured):
    items = [{"title": "x", "price": "$278.99"}]
    fake_client = _mock_client(_make_run(), items)
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        points, _ = google_shopping.fetch(ParsedCriteria(title_keywords="x"))
    assert points[0]["price"] == 278.99


def test_fetch_raises_when_run_not_succeeded(configured):
    items: list[dict] = []
    fake_client = _mock_client(_make_run(status="FAILED"), items)
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        with pytest.raises(google_shopping.PricingSourceError):
            google_shopping.fetch(ParsedCriteria(title_keywords="x"))


def test_fetch_raises_when_client_raises(configured):
    client = MagicMock()
    client.actor.return_value.call.side_effect = RuntimeError("network down")
    with patch("pricing.google_shopping.ApifyClient", return_value=client):
        with pytest.raises(google_shopping.PricingSourceError) as exc_info:
            google_shopping.fetch(ParsedCriteria(title_keywords="x"))
    assert "network down" in str(exc_info.value)


def test_fetch_raises_when_token_missing(monkeypatch):
    monkeypatch.setattr(config, "APIFY_TOKEN", None)
    monkeypatch.setattr(config, "BROKER_URL", "")
    monkeypatch.setattr(config, "BROKER_TOKEN", "")
    with pytest.raises(google_shopping.PricingSourceError) as exc_info:
        google_shopping.fetch(ParsedCriteria(title_keywords="x"))
    assert "APIFY_TOKEN" in str(exc_info.value)
    assert "BROKER" in str(exc_info.value)


def test_get_client_succeeds_in_broker_only_mode(monkeypatch):
    """Regression test: a friend with only broker vars set (no APIFY_TOKEN)
    must be able to construct a client — this is the bug from live e2e testing."""
    monkeypatch.setattr(config, "APIFY_TOKEN", None)
    monkeypatch.setattr(config, "BROKER_URL", "https://broker.example.ts.net")
    monkeypatch.setattr(config, "BROKER_TOKEN", "sa_friendtoken")
    fake_client = MagicMock()
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client) as mock_ctor:
        client = google_shopping._get_client()
    assert client is fake_client
    mock_ctor.assert_called_once_with(
        token="sa_friendtoken", api_url="https://broker.example.ts.net/apify"
    )


def test_condition_defaults_to_new_when_absent(configured):
    items = [{"title": "x", "price": 100.0}]
    fake_client = _mock_client(_make_run(), items)
    with patch("pricing.google_shopping.ApifyClient", return_value=fake_client):
        points, _ = google_shopping.fetch(ParsedCriteria(title_keywords="x"))
    assert points[0]["condition"] == "new"
