"""End-to-end tests for the searches router using FastAPI's TestClient."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agents.criteria_parser import ParsedCriteria
from api.main import app


@pytest.fixture
def client(tmp_db):
    return TestClient(app)


@patch("api.routes.searches.parse_criteria")
def test_post_searches_parse_returns_fragment_with_values(mock_parse, client):
    mock_parse.return_value = ParsedCriteria(
        title_keywords="iPhone 13 mini red 128GB",
        must_not_keywords=["cracked", "broken"],
        condition_floor="used",
        max_price=300.0,
        min_seller_rating=98.0,
    )

    resp = client.post("/searches/parse", data={"criteria_nl": "red iPhone 13 mini"})

    assert resp.status_code == 200
    body = resp.text
    assert 'name="title_keywords"' in body
    assert 'value="iPhone 13 mini red 128GB"' in body
    assert "cracked, broken" in body
    assert 'value="300.0"' in body
    # selected option for used
    assert 'value="used"        selected' in body or 'value="used" selected' in body


@patch("api.routes.searches.parse_criteria", side_effect=RuntimeError("api down"))
def test_post_searches_parse_haiku_failure_renders_empty_with_banner(mock_parse, client):
    resp = client.post("/searches/parse", data={"criteria_nl": "anything"})
    assert resp.status_code == 200
    assert "Couldn't parse" in resp.text


from db import repo


def test_post_searches_persists_and_redirects(client):
    resp = client.post(
        "/searches",
        data={
            "criteria_nl": "red iPhone 13 mini under $300",
            "title_keywords": "iPhone 13 mini red 128GB",
            "must_not_keywords_csv": "cracked, broken",
            "condition_floor": "used",
            "max_price": "300.00",
            "min_seller_rating": "98",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/searches/")

    search_id = int(resp.headers["location"].rsplit("/", 1)[1])
    row = repo.get_search(search_id)
    assert row is not None
    assert row["criteria_nl"] == "red iPhone 13 mini under $300"
    assert row["max_price"] == 300.00
    assert row["criteria_structured"]["must_not_keywords"] == ["cracked", "broken"]
    assert row["criteria_structured"]["title_keywords"] == "iPhone 13 mini red 128GB"
    assert row["criteria_structured"]["condition_floor"] == "used"


def test_post_searches_missing_max_price_returns_400(client):
    resp = client.post(
        "/searches",
        data={
            "criteria_nl": "anything",
            "title_keywords": "thing",
            "must_not_keywords_csv": "",
            "condition_floor": "",
            "max_price": "",
            "min_seller_rating": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_post_searches_empty_condition_floor_treated_as_null(client):
    """Form sends `condition_floor=""` for "any" — we must coerce to None."""
    resp = client.post(
        "/searches",
        data={
            "criteria_nl": "thing",
            "title_keywords": "thing",
            "must_not_keywords_csv": "",
            "condition_floor": "",
            "max_price": "50",
            "min_seller_rating": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    search_id = int(resp.headers["location"].rsplit("/", 1)[1])
    row = repo.get_search(search_id)
    assert row["criteria_structured"]["condition_floor"] is None


def test_get_search_detail_renders_persisted_row(client):
    search_id = repo.create_search(
        criteria_nl="red iPhone 13 mini under $300",
        criteria_structured={
            "title_keywords": "iPhone 13 mini red 128GB",
            "must_not_keywords": ["cracked"],
            "condition_floor": "used",
            "min_seller_rating": 98.0,
        },
        max_price=300.0,
    )

    resp = client.get(f"/searches/{search_id}")
    assert resp.status_code == 200
    assert "red iPhone 13 mini under $300" in resp.text
    assert "iPhone 13 mini red 128GB" in resp.text
    assert "cracked" in resp.text
    assert "$300.00" in resp.text


def test_get_search_detail_unknown_id_returns_404(client):
    resp = client.get("/searches/999999")
    assert resp.status_code == 404
