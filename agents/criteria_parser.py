"""Claude Haiku-backed natural-language parser for the criteria intake form.

The form sends a free-text "describe what you want" string; we ask Haiku to
extract the five structured fields the eBay Browse API can filter on. We use
the Anthropic SDK's tool-use feature so the model's output is validated against
a JSON schema server-side — we never parse free-form JSON out of a text reply.
"""

from typing import Literal

from pydantic import BaseModel, Field


ConditionFloor = Literal["new", "refurbished", "used", "any"]


class ParsedCriteria(BaseModel):
    """The five v1 structured fields. Every field is optional so a parse with
    low-confidence extraction still validates — the form lets the user fill in
    anything Haiku missed."""

    title_keywords: str = Field(
        default="",
        description="Free-text query passed to eBay Browse API `q`.",
    )
    must_not_keywords: list[str] = Field(
        default_factory=list,
        description="Terms to exclude; rendered as `-word` tokens in the eBay query.",
    )
    condition_floor: ConditionFloor | None = Field(
        default=None,
        description="Minimum acceptable condition; maps to eBay conditionIds filter.",
    )
    max_price: float | None = Field(
        default=None,
        description="USD ceiling. Required at form-submit time but may be None at parse time.",
    )
    min_seller_rating: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Percentage 0-100.",
    )
