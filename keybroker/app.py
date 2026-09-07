"""The proxy.

Transparent by default: forwards native vendor wire protocols with the owner's
key substituted. Bodies are parsed only where keybroker.clamps requires it, and
nothing but friends and spend is stored.

Order matters: fail closed on missing owner keys, authenticate, clamp, check
quota, forward, then meter. Metering last because it must never gate delivery.
"""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

import config
from keybroker import auth, clamps, db, meter, quota


log = logging.getLogger(__name__)

UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "apify": "https://api.apify.com",
}

_OWNER_KEY_ATTR = {"anthropic": "ANTHROPIC_API_KEY", "apify": "APIFY_TOKEN"}

# The friend's credential must never be relayed; host and content-length belong
# to the inbound hop; accept-encoding is dropped so httpx hands us decoded bytes.
_DROP_REQUEST_HEADERS = {
    "host", "content-length", "x-api-key", "authorization", "accept-encoding",
}

# httpx already decoded the body, so these would describe bytes we no longer have.
_DROP_RESPONSE_HEADERS = {
    "content-encoding", "content-length", "transfer-encoding", "connection",
}

_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    db.init_db()
    # Ten minutes matches the Anthropic SDK default, so the broker never times
    # out before the client it is serving does.
    #
    # Close the local reference, not the module global: tests swap the global
    # for a stub, and shutdown must close the client this function actually
    # opened rather than whatever the global happens to hold.
    client = httpx.AsyncClient(timeout=httpx.Timeout(600.0))
    _client = client
    try:
        yield
    finally:
        await client.aclose()
        _client = None


app = FastAPI(title="ScraperAgent key broker", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _proxy(vendor: str, path: str, request: Request) -> Response:
    owner_key = getattr(config, _OWNER_KEY_ATTR[vendor], "")
    if not owner_key:
        # Fail closed, mirroring api/auth.py on an unset DASHBOARD_PASSWORD.
        return Response(f"Broker {vendor} key is not configured.", status_code=503)

    friend = auth.friend_for_token(auth.extract_token(request.headers, vendor))
    if friend is None:
        client_host = request.client.host if request.client else "?"
        log.warning("broker: rejected %s request from %s", vendor, client_host)
        return Response("Unauthorized.", status_code=401)

    body = await request.body()
    try:
        clamps.ensure_size(body)
    except clamps.BodyTooLarge as exc:
        return Response(str(exc), status_code=413)
    try:
        clamps.ensure_not_streaming(body)
    except clamps.StreamingUnsupported as exc:
        return Response(str(exc), status_code=400)

    try:
        quota.check(friend)
    except quota.QuotaExceeded as exc:
        # 402, never 429: the Anthropic SDK retries 429 twice and Apify's four
        # times, which would bury this behind a confusing delay.
        return Response(exc.detail, status_code=402)

    if vendor == "anthropic":
        body = clamps.clamp_anthropic(body)
    elif clamps.is_apify_run_creation(request.method, path):
        body = clamps.clamp_apify_run(body, quota.remaining_usd(friend))

    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _DROP_REQUEST_HEADERS
    }
    if vendor == "anthropic":
        headers["x-api-key"] = owner_key
    else:
        headers["authorization"] = f"Bearer {owner_key}"

    try:
        upstream = await _client.request(
            request.method,
            f"{UPSTREAMS[vendor]}/{path}",
            content=body,
            headers=headers,
            params=dict(request.query_params),
        )
    except httpx.HTTPError as exc:
        log.warning("broker: upstream %s error: %s", vendor, exc)
        return Response(f"Upstream {vendor} error.", status_code=502)

    if upstream.status_code < 400:
        if vendor == "anthropic":
            meter.record_anthropic(friend["id"], upstream.content)
        else:
            meter.record_apify(friend["id"], upstream.content)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers={
            k: v for k, v in upstream.headers.items()
            if k.lower() not in _DROP_RESPONSE_HEADERS
        },
    )


@app.api_route("/anthropic/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_anthropic(path: str, request: Request) -> Response:
    return await _proxy("anthropic", path, request)


@app.api_route("/apify/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy_apify(path: str, request: Request) -> Response:
    return await _proxy("apify", path, request)
