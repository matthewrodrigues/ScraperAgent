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
