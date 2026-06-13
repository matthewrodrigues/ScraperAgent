"""LangGraph wiring.

Uses SqliteSaver so graph state survives restarts — important because eBay
Best Offer negotiations span hours-to-days while we wait for sellers and
human approvals.

The graph topology here is the v0 single-listing flow:
    search -> negotiate -> human_gate -> [browser | walk_away] -> ...

Build step 8 (parallel negotiator fan-out) will rewire this into the
two-swarm shape from the PRD. Until then this stub is what main.py drives.
"""

import sqlite3

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

import config
from graph.state import AgentState
from graph.nodes.search import search_node
from graph.nodes.negotiate import negotiate_node
from graph.nodes.human_gate import human_gate_node
from graph.nodes.purchase import purchase_node
from graph.nodes.orchestrator import route_after_human_gate, route_after_browser, walk_away_node


def browser_node(state: AgentState) -> AgentState:
    """Placeholder send action; gets replaced by integrations/ebay.send_best_offer in step 9."""
    print(f"[Browser] Sending message: {state['pending_message']}")
    updated_messages = state.get("messages", []) + [
        {"role": "agent", "content": state["pending_message"]}
    ]
    return {
        **state,
        "messages": updated_messages,
        "pending_message": None,
        "negotiation_rounds": state.get("negotiation_rounds", 0) + 1,
    }


def _build_graph(checkpointer):
    builder = StateGraph(AgentState)

    builder.add_node("search", search_node)
    builder.add_node("negotiate", negotiate_node)
    builder.add_node("human_gate", human_gate_node)
    builder.add_node("browser", browser_node)
    builder.add_node("purchase", purchase_node)
    builder.add_node("walk_away", walk_away_node)

    builder.set_entry_point("search")
    builder.add_edge("search", "negotiate")
    builder.add_edge("negotiate", "human_gate")

    builder.add_conditional_edges("human_gate", route_after_human_gate, {
        "browser": "browser",
        "walk_away": "walk_away",
    })

    builder.add_conditional_edges("browser", route_after_browser, {
        "negotiate": "negotiate",
        "purchase": "purchase",
        "end": END,
    })

    builder.add_edge("purchase", END)
    builder.add_edge("walk_away", END)

    return builder.compile(checkpointer=checkpointer, interrupt_before=["human_gate"])


# Module-level shared checkpointer connection. SqliteSaver wants its own
# connection it can use across threads; check_same_thread=False is required
# because LangGraph may invoke checkpoint ops from worker threads.
_checkpoint_conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
checkpointer = SqliteSaver(_checkpoint_conn)

graph = _build_graph(checkpointer)
