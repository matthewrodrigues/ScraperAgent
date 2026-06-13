from graph.state import AgentState


def search_node(state: AgentState) -> AgentState:
    """Queries marketplace for listings matching item_query and max_price."""
    # TODO: call eBay Browse API once keys are available
    # Returns the best candidate listing into state
    print(f"[Search] Searching for '{state['item_query']}' on {state['marketplace']}...")

    # Stub — hardcoded listing for development
    return {
        **state,
        "listing_url": "https://www.ebay.com/itm/stub",
        "listing_title": state["item_query"],
        "listed_price": 100.0,
        "seller_id": "stub_seller_123",
    }
