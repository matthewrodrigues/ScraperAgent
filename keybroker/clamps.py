"""Request rewrites for untrusted clients.

The broker forwards bodies verbatim except here. Each clamp exists because the
client runs on someone else's machine, so its values are advisory at best.
Together, the max_tokens ceiling and the body cap are what make
quota.MAX_SINGLE_CALL_USD a valid reservation — change them and recompute it.
"""

import json
import logging
from typing import Any


log = logging.getLogger(__name__)

MAX_TOKENS_CEILING = 4096
MAX_BODY_BYTES = 256 * 1024


class BodyTooLarge(Exception):
    """Request body exceeds MAX_BODY_BYTES."""


class StreamingUnsupported(Exception):
    """Streaming would under-meter silently, so it is refused outright."""


def _load(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def ensure_size(body: bytes) -> None:
    if len(body) > MAX_BODY_BYTES:
        raise BodyTooLarge(f"{len(body)} bytes exceeds the {MAX_BODY_BYTES} byte cap")


def ensure_not_streaming(body: bytes) -> None:
    payload = _load(body)
    if payload is not None and payload.get("stream") is True:
        raise StreamingUnsupported("streaming responses are not supported by the broker")


def clamp_anthropic(body: bytes) -> bytes:
    """Rewrite max_tokens down to the ceiling. Unparseable bodies pass through —
    upstream is a better judge of malformed JSON than we are."""
    payload = _load(body)
    if payload is None:
        return body
    if payload.get("max_tokens", 0) > MAX_TOKENS_CEILING:
        log.info("clamping max_tokens %s -> %s", payload["max_tokens"], MAX_TOKENS_CEILING)
        payload["max_tokens"] = MAX_TOKENS_CEILING
        return json.dumps(payload).encode("utf-8")
    return body


def is_apify_run_creation(method: str, path: str) -> bool:
    """POST /v2/acts/{actorId}/runs — the only Apify call that starts spending."""
    return method.upper() == "POST" and path.rstrip("/").endswith("/runs")


def clamp_apify_run(body: bytes, remaining_usd: float) -> bytes:
    """Cap the run's own spend ceiling at the friend's remaining budget.

    Apify is the only vendor exposing a pre-spend limit, so this is the one
    place the broker prevents spend rather than measuring it afterwards.
    """
    payload = _load(body) or {}
    requested = payload.get("maxTotalChargeUsd")
    if requested is None or float(requested) > remaining_usd:
        payload["maxTotalChargeUsd"] = round(remaining_usd, 4)
    return json.dumps(payload).encode("utf-8")
