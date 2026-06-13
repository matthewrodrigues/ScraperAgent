from graph.state import AgentState


def purchase_node(state: AgentState) -> AgentState:
    """Surfaces deal details for human to manually complete payment."""
    print("\n" + "="*50)
    print("DEAL REACHED — Human action required to complete purchase")
    print("="*50)
    print(f"  Item:          {state['listing_title']}")
    print(f"  URL:           {state['listing_url']}")
    print(f"  Listed price:  ${state['listed_price']}")
    print(f"  Agreed price:  ${state['agreed_price']}")
    savings_pct = ((state['listed_price'] - state['agreed_price']) / state['listed_price']) * 100
    print(f"  Savings:       {savings_pct:.1f}%")
    print("="*50)
    print("Please complete the purchase manually in your browser.")

    return {**state, "outcome": "purchased"}
