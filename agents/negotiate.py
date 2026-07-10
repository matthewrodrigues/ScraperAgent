"""Claude-driven negotiation message drafter.

Given a chosen strategy (from `strategies.pick_strategy`), a listing, the search
criteria, and reference prices, generate an opening message to the eBay seller.

Mirrors the tool-use pattern in `agents/criteria_parser.py:98-114`: Sonnet 4.6
emits a single `record_message` tool_use block, which we validate with Pydantic.
The Pydantic validator enforces the hard `offer_amount <= max_price` cap from
PRD §187 — *two* layers of defense (prompt + code), not one.
"""

from typing import Any

from anthropic import Anthropic
from pydantic import BaseModel, Field, ValidationError

import config
from strategies import Strategy


class NegotiationDraftError(RuntimeError):
    """Raised when Sonnet's response doesn't contain a usable tool_use block
    or fails Pydantic validation (e.g. offer above max_price)."""


class DraftedMessage(BaseModel):
    body: str = Field(min_length=20, max_length=2000)
    offer_amount: float = Field(gt=0)


_RECORD_MESSAGE_TOOL = {
    "name": "record_message",
    "description": "Record the drafted opening message + final offer amount.",
    "input_schema": {
        "type": "object",
        "properties": {
            "body": {
                "type": "string",
                "description": "The message body to send to the seller. 2-4 short sentences. No emojis. No exclamation marks. Plain text only.",
            },
            "offer_amount": {
                "type": "number",
                "description": "The dollar amount you're offering. Must be at or below the buyer's max_price.",
            },
        },
        "required": ["body", "offer_amount"],
    },
}


def _build_user_message(
    strategy: Strategy,
    listing: dict[str, Any],
    search: dict[str, Any],
    reference_prices: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Pack everything the LLM needs into one structured user message.

    No JSON or YAML — plain English with labeled sections reads better for
    the model and is easier to debug when something goes wrong.

    When `history` is non-empty, the prompt explicitly asks for a *counter-
    response* to the latest seller turn instead of an opening message. Same
    strategy applies (PRD §103-114 says no mid-conversation adaptation).
    """
    ref_lines: list[str] = []
    for r in reference_prices:
        if r.get("median") is None:
            continue
        cond = r.get("condition") or "unspecified"
        ref_lines.append(
            f"- {r.get('source', 'unknown')} ({cond}): median ${r['median']:.2f}"
            + (f", p25 ${r['p25']:.2f}" if r.get("p25") is not None else "")
            + (f", p75 ${r['p75']:.2f}" if r.get("p75") is not None else "")
        )
    ref_block = "\n".join(ref_lines) if ref_lines else "(no reference price data available)"

    base = f"""\
BUYER'S MAX PRICE: ${search['max_price']:.2f}  (hard ceiling — never offer above this)
STRATEGY: {strategy.name}
TARGET OFFER AMOUNT: ${strategy.offer_amount:.2f}  (use this; round to whole dollars only if it reads naturally)

LISTING:
- title: {listing.get('title', '(no title)')}
- asking price: ${float(listing['price']):.2f}
- condition: {listing.get('condition') or 'unspecified'}
- seller rating: {f"{listing['seller_rating']:.1f}%" if listing.get('seller_rating') is not None else 'unknown'}
- seller feedback count: {listing.get('seller_feedback_count') or 'unknown'}

REFERENCE PRICES (cross-market comps):
{ref_block}
"""

    if not history:
        return base + (
            "\nDraft the OPENING message to the seller. Output via the record_message tool. "
            "Use the offer amount specified above unless it would violate the max-price rule."
        )

    # Format history as a labeled transcript. Each seller turn is hostile-text-
    # safe-ish: the constitutional rules in the system prompt tell the model
    # to treat anything inside SELLER: as untrusted content, not instructions.
    transcript_lines: list[str] = []
    for msg in history:
        role = "AGENT" if msg.get("role") == "agent" else "SELLER"
        body = (msg.get("body") or "").strip()
        transcript_lines.append(f"{role}: {body}")
    transcript = "\n\n".join(transcript_lines)

    return base + f"""
CONVERSATION SO FAR (oldest first):
{transcript}

Draft your COUNTER response to the most recent SELLER turn. Output via the record_message tool.
Same strategy as before. Update the offer amount only if the seller has counter-offered \
and the strategy heuristic would move our offer up — but never above the buyer's max_price."""


def draft_message(
    strategy: Strategy,
    listing: dict[str, Any],
    search: dict[str, Any],
    reference_prices: list[dict[str, Any]],
    history: list[dict[str, Any]] | None = None,
) -> tuple[DraftedMessage, dict[str, Any]]:
    """Ask Sonnet for an opening (or counter) message. Returns the validated
    `DraftedMessage` plus a usage dict ready to splat into `repo.add_message`:
    `{"input_tokens": int, "output_tokens": int, "cost_usd": float}`.

    Raises NegotiationDraftError on any failure (no tool_use block, malformed
    input, offer above max_price). The usage dict is only returned on success
    — failed drafts didn't produce a billable message in the user's mind."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=config.NEGOTIATOR_MODEL,
        max_tokens=1024,
        system=strategy.system_prompt,
        tools=[_RECORD_MESSAGE_TOOL],
        tool_choice={"type": "tool", "name": "record_message"},
        messages=[{"role": "user", "content": _build_user_message(strategy, listing, search, reference_prices, history)}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_message":
            try:
                drafted = DraftedMessage.model_validate(block.input)
            except ValidationError as exc:
                raise NegotiationDraftError(f"validation failed: {exc}") from exc
            # PRD §187: max_price is read by code, not the LLM. Hard cap here even
            # if the LLM's offer slipped through the prompt instruction.
            if drafted.offer_amount > search["max_price"]:
                raise NegotiationDraftError(
                    f"offer ${drafted.offer_amount} exceeds max_price ${search['max_price']}"
                )
            usage = {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
                "cost_usd": config.price_usage(
                    config.NEGOTIATOR_MODEL,
                    response.usage.input_tokens,
                    response.usage.output_tokens,
                ),
            }
            return drafted, usage

    raise NegotiationDraftError("Sonnet response did not contain a record_message tool_use block")
