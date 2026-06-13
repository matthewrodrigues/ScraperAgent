from graph.state import AgentState

# TODO: implement once eBay API keys are approved
# Will use eBay Browse API for search and Playwright for messaging


async def search_ebay(query: str, max_price: float) -> list[dict]:
    """Search eBay listings via Browse API. Returns list of listing dicts."""
    raise NotImplementedError("eBay API keys pending approval")


async def send_ebay_message(state: AgentState, message: str) -> bool:
    """Send a message to an eBay seller via Playwright."""
    raise NotImplementedError("eBay messaging not yet implemented")
