from graph.state import AgentState, NegotiationStatus


def route_after_human_gate(state: AgentState) -> str:
    """Routes based on human decision after message approval gate."""
    if state["human_decision"] == "reject":
        return "walk_away"
    return "browser"


def route_after_browser(state: AgentState) -> str:
    """Routes based on negotiation status after message is sent."""
    status = state["negotiation_status"]
    if status == NegotiationStatus.WALK_AWAY:
        return "end"
    if status == NegotiationStatus.DEAL:
        return "purchase"
    return "negotiate"


def walk_away_node(state: AgentState) -> AgentState:
    print(f"[Walk Away] Ending negotiation for '{state['listing_title']}'.")
    return {**state, "outcome": "passed", "negotiation_status": NegotiationStatus.WALK_AWAY}
