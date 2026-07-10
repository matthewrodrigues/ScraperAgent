"""Tests for the Claude message drafter. Mocks Anthropic at module level."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agents import negotiate
from strategies import Strategy


def _strategy(name: str = "anchor_low", offer: float = 80.0) -> Strategy:
    return Strategy(
        name=name,
        system_prompt=f"({name} system prompt) max_price is the hard ceiling.",
        offer_amount=offer,
        inputs={},
    )


def _listing(**overrides) -> dict:
    base = {
        "title": "Sony WH-1000XM5",
        "price": 100.0,
        "condition": "Used",
        "seller_rating": 99.5,
        "seller_feedback_count": 1000,
    }
    base.update(overrides)
    return base


def _search(max_price: float = 100.0) -> dict:
    return {"max_price": max_price, "criteria_nl": "headphones"}


def _mock_tool_response(body: str, offer: float, input_tokens: int = 1500, output_tokens: int = 80):
    """Build a fake Anthropic response with one tool_use block + a usage block.
    Defaults are realistic-ish: ~1500 in for the system prompt + listing
    context, ~80 out for a short structured tool_use response. Tests that
    care about cost math should override input_tokens/output_tokens."""
    block = SimpleNamespace(type="tool_use", name="record_message", input={"body": body, "offer_amount": offer})
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(content=[block], usage=usage)


def test_draft_returns_validated_drafted_message():
    response = _mock_tool_response("Hello, would you accept $85 for the headphones?", 85.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        drafted, _usage = negotiate.draft_message(_strategy(), _listing(), _search(max_price=100.0), [])

    assert drafted.body.startswith("Hello")
    assert drafted.offer_amount == 85.0


def test_draft_sends_strategy_system_prompt_and_correct_model():
    response = _mock_tool_response("hi there, $85 sound good?", 85.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        negotiate.draft_message(_strategy("batna_signal"), _listing(), _search(), [])

    kwargs = fake_client.messages.create.call_args.kwargs
    assert "batna_signal" in kwargs["system"]
    assert kwargs["tool_choice"] == {"type": "tool", "name": "record_message"}
    # Model is config.NEGOTIATOR_MODEL = sonnet 4.6
    import config
    assert kwargs["model"] == config.NEGOTIATOR_MODEL


def test_draft_passes_listing_and_reference_context_in_user_message():
    response = _mock_tool_response("hi there, $85 sound good?", 85.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        negotiate.draft_message(
            _strategy(),
            _listing(title="Sony WH-1000XM5", price=100.0),
            _search(max_price=100.0),
            [{"source": "google_shopping", "condition": "new", "median": 90.0, "p25": 80.0, "p75": 100.0}],
        )

    user_msg = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "Sony WH-1000XM5" in user_msg
    assert "$100.00" in user_msg  # asking
    assert "google_shopping" in user_msg
    assert "median $90.00" in user_msg
    assert "MAX PRICE: $100.00" in user_msg


def test_draft_handles_empty_reference_prices_gracefully():
    response = _mock_tool_response("hi there, $90 sound good?", 90.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        drafted, _usage = negotiate.draft_message(_strategy(), _listing(), _search(), [])

    assert drafted.offer_amount == 90.0
    user_msg = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "no reference price data" in user_msg


def test_draft_raises_when_no_tool_use_block():
    """If Sonnet text-replies instead of calling the tool, we surface a clear error."""
    bad = SimpleNamespace(content=[SimpleNamespace(type="text", text="I refuse")])
    fake_client = MagicMock()
    fake_client.messages.create.return_value = bad

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        with pytest.raises(negotiate.NegotiationDraftError):
            negotiate.draft_message(_strategy(), _listing(), _search(), [])


def test_draft_raises_on_validation_failure():
    """Body too short (< 20 chars) fails Pydantic validation."""
    response = _mock_tool_response("too short", 80.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        with pytest.raises(negotiate.NegotiationDraftError):
            negotiate.draft_message(_strategy(), _listing(), _search(), [])


def test_draft_hard_caps_offer_at_max_price():
    """Even if the LLM ignored the prompt and offered above max_price, we reject."""
    response = _mock_tool_response("Would you accept $150 for the headphones?", 150.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        with pytest.raises(negotiate.NegotiationDraftError) as exc_info:
            negotiate.draft_message(_strategy(), _listing(), _search(max_price=100.0), [])

    assert "exceeds max_price" in str(exc_info.value)


def test_draft_with_history_asks_for_counter_and_includes_transcript():
    response = _mock_tool_response("I appreciate the offer; could we meet at $90?", 90.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    history = [
        {"role": "agent", "body": "Would you accept $80?"},
        {"role": "seller", "body": "Can't go below $95."},
    ]
    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        drafted, _usage = negotiate.draft_message(_strategy(), _listing(), _search(max_price=100.0), [], history=history)

    user_msg = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "COUNTER response" in user_msg
    assert "AGENT: Would you accept $80?" in user_msg
    assert "SELLER: Can't go below $95." in user_msg
    assert drafted.body.startswith("I appreciate")


def test_draft_without_history_asks_for_opening():
    response = _mock_tool_response("Opening message, would you accept $80?", 80.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        negotiate.draft_message(_strategy(), _listing(), _search(), [])

    user_msg = fake_client.messages.create.call_args.kwargs["messages"][0]["content"]
    assert "OPENING message" in user_msg
    assert "CONVERSATION SO FAR" not in user_msg


def test_draft_offer_exactly_at_max_price_is_allowed():
    """The cap is <=, not <."""
    response = _mock_tool_response("Would $100 work for the headphones?", 100.0)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        drafted, _usage = negotiate.draft_message(_strategy(), _listing(), _search(max_price=100.0), [])
    assert drafted.offer_amount == 100.0


# ---- Usage / cost tracking ----

def test_draft_returns_usage_dict_with_three_keys():
    """The usage dict is what add_message expects; its three keys map directly."""
    response = _mock_tool_response("Would you accept $85 for the headphones?", 85.0,
                                   input_tokens=2000, output_tokens=100)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        _drafted, usage = negotiate.draft_message(_strategy(), _listing(), _search(max_price=100.0), [])

    assert set(usage.keys()) == {"input_tokens", "output_tokens", "cost_usd"}
    assert usage["input_tokens"] == 2000
    assert usage["output_tokens"] == 100


def test_draft_cost_matches_price_usage_helper():
    """Cost on the usage dict is computed via config.price_usage with the
    NEGOTIATOR_MODEL — verify the math is end-to-end correct for pinned tokens."""
    import config

    response = _mock_tool_response("Would you accept $85 for the headphones?", 85.0,
                                   input_tokens=10_000, output_tokens=500)
    fake_client = MagicMock()
    fake_client.messages.create.return_value = response

    with patch("agents.negotiate.Anthropic", return_value=fake_client):
        _drafted, usage = negotiate.draft_message(_strategy(), _listing(), _search(max_price=100.0), [])

    expected = config.price_usage(config.NEGOTIATOR_MODEL, 10_000, 500)
    assert usage["cost_usd"] == expected
    # Sanity: with default Sonnet rates ($3 in / $15 out per Mtok), 10k in + 500 out
    # = (10000*3 + 500*15) / 1_000_000 = 0.0375. Default-rate regression guard.
    assert usage["cost_usd"] == pytest.approx(0.0375, rel=1e-6)


def test_price_usage_unknown_model_returns_zero(caplog):
    """Unknown model id → 0.0 + warning. Never crash a draft because pricing
    is stale or a new model id slipped through."""
    import logging
    import config

    with caplog.at_level(logging.WARNING):
        cost = config.price_usage("not-a-real-model", 1000, 1000)
    assert cost == 0.0
    assert any("not-a-real-model" in r.message for r in caplog.records)
