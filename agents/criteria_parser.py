"""Claude Haiku-backed natural-language parser for the criteria intake form.

The form sends a free-text "describe what you want" string; we ask Haiku to
extract the five structured fields the eBay Browse API can filter on. We use
the Anthropic SDK's tool-use feature so the model's output is validated against
a JSON schema server-side — we never parse free-form JSON out of a text reply.
"""

from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel, Field

import config
from integrations import clients


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


class CriteriaParseError(RuntimeError):
    """Raised when Haiku's response doesn't contain a usable tool-use block."""


_SYSTEM_PROMPT = (
    "You extract structured shopping criteria from a buyer's natural-language "
    "description of an item they want to find on eBay. Call the `record_criteria` "
    "tool exactly once. Leave any field you can't confidently infer at its default "
    "(empty string, empty list, or null). Never guess a max price — only fill it "
    "when the buyer states one explicitly."
)


_RECORD_CRITERIA_TOOL = {
    "name": "record_criteria",
    "description": "Record the structured shopping criteria extracted from the buyer's description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title_keywords": {
                "type": "string",
                "description": "Concise eBay search query — the words you'd type into the search bar.",
            },
            "must_not_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Terms to exclude (e.g. 'broken', 'parts only').",
            },
            "condition_floor": {
                "type": ["string", "null"],
                "enum": ["new", "refurbished", "used", "any", None],
                "description": "Minimum acceptable condition, or null if unspecified.",
            },
            "max_price": {
                "type": ["number", "null"],
                "description": "USD price ceiling if the buyer stated one; otherwise null.",
            },
            "min_seller_rating": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 100,
                "description": "Minimum seller feedback percentage if specified.",
            },
        },
        "required": [],
    },
}


def parse_criteria(nl_text: str) -> ParsedCriteria:
    """Ask Haiku to extract structured criteria from a free-text description."""
    client = Anthropic(**clients.anthropic_kwargs())
    response = client.messages.create(
        model=config.PARSER_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[_RECORD_CRITERIA_TOOL],
        tool_choice={"type": "tool", "name": "record_criteria"},
        messages=[{"role": "user", "content": nl_text}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_criteria":
            return ParsedCriteria.model_validate(block.input)

    raise CriteriaParseError("Haiku response did not contain a record_criteria tool_use block")


class SearchSubmission(BaseModel):
    """Validated form payload for `POST /searches`.

    Separate from `ParsedCriteria` because:
      - `max_price` is required here (we won't persist a search without one)
      - the form sends `must_not_keywords` as a single CSV string; we split here
      - `criteria_nl` is part of the persisted row but not part of the parsed shape
    """

    criteria_nl: str
    title_keywords: str = ""
    must_not_keywords_csv: str = ""
    condition_floor: ConditionFloor | None = None
    max_price: float = Field(gt=0)
    min_seller_rating: float | None = Field(default=None, ge=0, le=100)

    @property
    def must_not_keywords(self) -> list[str]:
        return [kw.strip() for kw in self.must_not_keywords_csv.split(",") if kw.strip()]

    def to_structured_dict(self) -> dict:
        """Shape stored in `searches.criteria_structured_json`. Excludes `criteria_nl`
        and `max_price` because those have dedicated columns."""
        return {
            "title_keywords": self.title_keywords,
            "must_not_keywords": self.must_not_keywords,
            "condition_floor": self.condition_floor,
            "min_seller_rating": self.min_seller_rating,
        }
