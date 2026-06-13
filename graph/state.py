from typing import TypedDict, Optional
from enum import Enum


class NegotiationStatus(str, Enum):
    OPEN = "open"
    FIRST_OFFER_SENT = "first_offer_sent"
    AWAITING_RESPONSE = "awaiting_response"
    RESPONSE_RECEIVED = "response_received"
    RE_EVALUATE = "re_evaluate"
    COUNTER_SENT = "counter_sent"
    DEAL = "deal"
    WALK_AWAY = "walk_away"


class AgentState(TypedDict):
    # Run config
    item_query: str
    marketplace: str
    strategy: str
    max_price: float

    # Current listing
    listing_url: Optional[str]
    listing_title: Optional[str]
    listed_price: Optional[float]
    seller_id: Optional[str]

    # Negotiation state
    negotiation_status: NegotiationStatus
    negotiation_rounds: int
    current_offer: Optional[float]
    messages: list[dict]  # [{"role": "agent"|"seller", "content": str}]

    # Human gate
    awaiting_human: bool
    human_decision: Optional[str]  # "approve" | "reject"
    pending_message: Optional[str]  # message staged for human approval

    # Outcome
    agreed_price: Optional[float]
    outcome: Optional[str]  # purchased | passed | fell_through | abandoned
