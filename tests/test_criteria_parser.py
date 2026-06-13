"""Tests for agents.criteria_parser."""

import pytest

from agents.criteria_parser import ParsedCriteria


def test_parsed_criteria_all_fields_optional():
    """Every field has a safe default so a missing-info parse still validates."""
    pc = ParsedCriteria()
    assert pc.title_keywords == ""
    assert pc.must_not_keywords == []
    assert pc.condition_floor is None
    assert pc.max_price is None
    assert pc.min_seller_rating is None


def test_parsed_criteria_condition_floor_constrained():
    """condition_floor only accepts the documented literals."""
    with pytest.raises(ValueError):
        ParsedCriteria(condition_floor="brand-new")  # not in the Literal set


from unittest.mock import MagicMock, patch


def _fake_anthropic_response(tool_input: dict):
    """Build a minimal stub of the Anthropic Messages response shape."""
    response = MagicMock()
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_criteria"
    block.input = tool_input
    response.content = [block]
    return response


@patch("agents.criteria_parser.Anthropic")
def test_parse_criteria_happy_path(anthropic_cls):
    from agents.criteria_parser import parse_criteria

    client = anthropic_cls.return_value
    client.messages.create.return_value = _fake_anthropic_response({
        "title_keywords": "iPhone 13 mini red 128GB unlocked",
        "must_not_keywords": ["cracked", "broken"],
        "condition_floor": "used",
        "max_price": 300.0,
        "min_seller_rating": 98.0,
    })

    result = parse_criteria("red iPhone 13 mini, 128GB+, unlocked, no cracks, under $300, rep 98+")

    assert result.title_keywords == "iPhone 13 mini red 128GB unlocked"
    assert result.must_not_keywords == ["cracked", "broken"]
    assert result.condition_floor == "used"
    assert result.max_price == 300.0
    assert result.min_seller_rating == 98.0


@patch("agents.criteria_parser.Anthropic")
def test_parse_criteria_partial_extraction(anthropic_cls):
    """Haiku may only fill some fields — those left out get defaults."""
    from agents.criteria_parser import parse_criteria

    client = anthropic_cls.return_value
    client.messages.create.return_value = _fake_anthropic_response({
        "title_keywords": "vintage Polaroid camera",
    })

    result = parse_criteria("vintage Polaroid camera")
    assert result.title_keywords == "vintage Polaroid camera"
    assert result.must_not_keywords == []
    assert result.max_price is None


@patch("agents.criteria_parser.Anthropic")
def test_parse_criteria_no_tool_use_block_raises(anthropic_cls):
    """If Haiku ignores the tool (shouldn't happen with tool_choice=tool), surface it."""
    from agents.criteria_parser import parse_criteria, CriteriaParseError

    client = anthropic_cls.return_value
    response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    response.content = [text_block]
    client.messages.create.return_value = response

    with pytest.raises(CriteriaParseError):
        parse_criteria("anything")


def test_search_submission_requires_max_price():
    from agents.criteria_parser import SearchSubmission

    with pytest.raises(ValueError):
        SearchSubmission(criteria_nl="anything", max_price=None)


def test_search_submission_splits_must_not_keywords():
    from agents.criteria_parser import SearchSubmission

    sub = SearchSubmission(
        criteria_nl="red iPhone",
        title_keywords="iPhone 13 mini red",
        must_not_keywords_csv=" cracked , broken ,  ,water damage",
        condition_floor="used",
        max_price=300.0,
        min_seller_rating=98.0,
    )

    assert sub.must_not_keywords == ["cracked", "broken", "water damage"]


def test_search_submission_to_structured_dict():
    from agents.criteria_parser import SearchSubmission

    sub = SearchSubmission(
        criteria_nl="x",
        title_keywords="thing",
        must_not_keywords_csv="",
        condition_floor=None,
        max_price=50.0,
        min_seller_rating=None,
    )

    d = sub.to_structured_dict()
    assert d == {
        "title_keywords": "thing",
        "must_not_keywords": [],
        "condition_floor": None,
        "min_seller_rating": None,
    }
    # max_price is stored in its own column, not the JSON blob
    assert "max_price" not in d
    assert "criteria_nl" not in d
