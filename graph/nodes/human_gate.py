from langgraph.types import interrupt
from graph.state import AgentState


def human_gate_node(state: AgentState) -> AgentState:
    """Pauses graph and waits for human approval or rejection."""
    decision = interrupt({
        "prompt": "Approve or reject this message?",
        "pending_message": state["pending_message"],
        "listing_url": state["listing_url"],
        "listing_title": state["listing_title"],
        "listed_price": state["listed_price"],
        "current_offer": state["current_offer"],
        "conversation": state["messages"],
    })

    return {
        **state,
        "awaiting_human": False,
        "human_decision": decision,
    }
