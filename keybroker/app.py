"""The proxy.

Transparent by default: forwards native vendor wire protocols with the owner's
key substituted. Bodies are parsed only where keybroker.clamps requires it, and
nothing but friends and spend is stored.

Order matters: fail closed on missing owner keys, authenticate, refuse
off-allowlist paths and oversized bodies before reading them, clamp, check
quota, forward, then meter. Metering last because it must never gate delivery.
"""

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response

import config
from keybroker import auth, clamps, db, dialect, meter, quota


log = logging.getLogger(__name__)

UPSTREAMS = {
    "anthropic": "https://api.anthropic.com",
    "apify": "https://api.apify.com",
}

_OWNER_KEY_ATTR = {"anthropic": "ANTHROPIC_API_KEY", "apify": "APIFY_TOKEN"}

# Only the two Anthropic endpoints this app actually calls. An open /anthropic/*
# route also exposes /v1/messages/batches, whose bodies nest max_tokens under
# requests[].params (so the clamp misses them) and whose create response carries
# no usage (so the meter records nothing) — hundreds of unclamped, unmetered
# requests inside one 256 KB body. Everything else gets 404.
_ANTHROPIC_ALLOWED_PATHS = {"v1/messages", "v1/messages/count_tokens"}

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
    dialect.active().open_pool()
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
        dialect.active().close_pool()


app = FastAPI(title="ScraperAgent key broker", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _proxy(vendor: str, path: str, request: Request) -> Response:
    owner_key = getattr(config, _OWNER_KEY_ATTR[vendor], "")
    if not owner_key:
        # Fail closed, mirroring api/auth.py on an unset DASHBOARD_PASSWORD.
        return Response(f"Broker {vendor} key is not configured.", status_code=503)

    try:
        friend = auth.friend_for_token(auth.extract_token(request.headers, vendor))
    except Exception:
        # Fail CLOSED: the cap cannot be checked, so nothing may be spent.
        # Distinct from 401 (a known-bad token) and 402 (a real refusal) —
        # this is the broker being unable to answer, not a decision about
        # this caller.
        log.exception("broker: datastore unavailable during auth")
        return Response("Broker datastore unavailable.", status_code=503)
    if friend is None:
        client_host = request.client.host if request.client else "?"
        log.warning("broker: rejected %s request from %s", vendor, client_host)
        return Response("Unauthorized.", status_code=401)

    if vendor == "anthropic" and path.strip("/") not in _ANTHROPIC_ALLOWED_PATHS:
        log.warning("broker: refused anthropic path %r for %s", path, friend["name"])
        return Response("Not found.", status_code=404)

    # Consult Content-Length before reading: request.body() buffers the whole
    # payload, so without this an authenticated friend can exhaust broker memory
    # with a multi-GB POST. ensure_size below stays as the backstop for absent
    # or lying headers.
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > clamps.MAX_BODY_BYTES:
                return Response(
                    f"{declared} bytes exceeds the {clamps.MAX_BODY_BYTES} byte cap",
                    status_code=413,
                )
        except ValueError:
            pass  # unparseable header; the post-read check will catch it

    body = await request.body()
    try:
        clamps.ensure_size(body)
    except clamps.BodyTooLarge as exc:
        return Response(str(exc), status_code=413)
    try:
        clamps.ensure_not_streaming(body)
    except clamps.StreamingUnsupported as exc:
        return Response(str(exc), status_code=400)

    if vendor == "anthropic":
        # Before quota.check and before anything is forwarded: an unpriced model
        # meters at $0.00, which would leave both caps inert.
        try:
            clamps.ensure_priced_model(body)
        except clamps.UnpricedModel as exc:
            return Response(str(exc), status_code=400)

    try:
        quota.check(friend)
    except quota.QuotaExceeded as exc:
        # 402, never 429: the Anthropic SDK retries 429 twice and Apify's four
        # times, which would bury this behind a confusing delay.
        return Response(exc.detail, status_code=402)
    except Exception:
        log.exception("broker: datastore unavailable during quota check")
        return Response("Broker datastore unavailable.", status_code=503)

    params = dict(request.query_params)
    apify_ceiling_usd: float | None = None
    if vendor == "anthropic":
        body = clamps.clamp_anthropic(body)
    elif clamps.is_apify_run_creation(request.method, path):
        # The body is the actor's own input record and must survive untouched;
        # Apify reads the run's spend ceiling from the query string instead.
        params = clamps.clamp_apify_charge(params, quota.remaining_usd(friend))
        # Remember what the run was actually capped at: that is the worst case
        # this run can cost, and it is what the friend gets debited on creation.
        apify_ceiling_usd = float(params[clamps.APIFY_CHARGE_PARAM])

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
            params=params,
        )
    except httpx.HTTPError as exc:
        log.warning("broker: upstream %s error: %s", vendor, exc)
        return Response(f"Upstream {vendor} error.", status_code=502)

    if upstream.status_code < 400:
        try:
            if vendor == "anthropic":
                meter.record_anthropic(friend["id"], upstream.content)
            else:
                # A created run is debited at its clamped ceiling immediately, then
                # settled by scripts.reconcile_spend — Apify's usage figure is not
                # trustworthy at the moment the run first reports terminal. Anything
                # that is not a run creation (or carries no run id) falls through to
                # the defensive path.
                debited = apify_ceiling_usd is not None and meter.record_apify_provisional(
                    friend["id"], upstream.content, apify_ceiling_usd
                )
                if not debited:
                    meter.record_apify(friend["id"], upstream.content)
        except Exception:
            # Fail OPEN: the money is already spent upstream, so a bookkeeping
            # failure must not also cost the friend their result. meter.py has
            # internal exception handling too; this catches the case where that
            # mechanism fails or the metering function itself is broken.
            log.exception("failed to record spend for friend %s", friend["id"])

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


# Only GET (run-status polling, dataset item fetches) and POST (run creation)
# are ever issued by this app. PUT/PATCH/DELETE would let an authenticated
# friend reach destructive Apify management calls (deleting actors, tasks,
# schedules, webhooks) under the owner's token. A path allowlist (as used for
# /anthropic/*) was rejected here: the Apify SDK's .call() spans run creation,
# run-status polling, and dataset fetches, and may hit endpoints not enumerated
# in our code, so an allowlist risks silently breaking a friend's search.
# Narrowing methods= is enough to close the hole and cannot break anything the
# app does; FastAPI refuses other methods with 405 before _proxy ever runs.
@app.api_route("/apify/{path:path}", methods=["GET", "POST"])
async def proxy_apify(path: str, request: Request) -> Response:
    return await _proxy("apify", path, request)
