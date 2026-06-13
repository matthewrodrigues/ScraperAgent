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
