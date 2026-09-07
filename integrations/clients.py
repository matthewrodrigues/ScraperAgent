"""Vendor SDK credential resolution.

Returns constructor *kwargs*, not constructed clients. Call sites keep their own
`Anthropic(...)` / `ApifyClient(...)` construction because the test suite patches
those classes where they are used; moving construction here would break ~20
existing patches. See the Phase 1 design spec, section 10.
"""

from typing import Any

import config


def anthropic_kwargs() -> dict[str, Any]:
    """Constructor kwargs for `anthropic.Anthropic`."""
    if config.BROKER_URL:
        return {
            "api_key": config.BROKER_TOKEN,
            "base_url": f"{config.BROKER_URL}/anthropic",
        }
    return {"api_key": config.ANTHROPIC_API_KEY}


def apify_kwargs() -> dict[str, Any]:
    """Constructor kwargs for `apify_client.ApifyClient`."""
    if config.BROKER_URL:
        return {
            "token": config.BROKER_TOKEN,
            "api_url": f"{config.BROKER_URL}/apify",
        }
    return {"token": config.APIFY_TOKEN}
