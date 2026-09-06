"""Dashboard authentication.

Why the dashboard needs a password at all: its routes spend real money
(Anthropic tokens, Apify actor runs) and drive a Chrome session already logged
into the owner's eBay account. An unauthenticated dashboard is not a data leak,
it's a spending and impersonation hole.

This is the *inner* layer. In the deployed setup Cloudflare Access sits in
front as the outer gate. Keeping an app-level password behind it means a
misconfigured tunnel — or an accidental `--host 0.0.0.0` — degrades to "asks
for a password" rather than "wide open".

Design notes:

  * Cookie signing is delegated to Starlette's `SessionMiddleware`
    (itsdangerous under the hood). We decide *who* gets a session; we do not
    hand-roll the crypto that keeps it unforgeable.
  * `config.DASHBOARD_PASSWORD` is read at request time, not import time, so
    tests can patch it and so a restart is enough to rotate it.
  * The exempt list is the entire security boundary — see EXEMPT_PATHS.
"""

from __future__ import annotations

import hmac
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

import config


log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))

SESSION_KEY = "authed"

# Paths reachable with no session. This set is the security boundary, so each
# entry earns its place:
#   /health                 — liveness probe; returns a constant.
#   /login                  — the door itself; gating it would lock everyone out.
#   /ebay/account-deletion  — eBay's servers call this and cannot authenticate.
#                             Safe to expose because it is genuinely inert: the
#                             GET returns sha256(challenge + token + endpoint),
#                             which reveals nothing without the token, and the
#                             POST only logs and returns 204.
EXEMPT_PATHS = frozenset({"/health", "/login", "/ebay/account-deletion"})

# Static assets are public by nature (CSS and the HTMX bundle) and are needed to
# render the login page itself.
EXEMPT_PREFIXES = ("/static/",)


def _is_exempt(path: str) -> bool:
    return path in EXEMPT_PATHS or path.startswith(EXEMPT_PREFIXES)


def _safe_next(target: str | None) -> str:
    """Reduce a caller-supplied `next` to a same-origin path, else "/".

    `next` arrives from a query string or form field, both attacker-controllable
    via a crafted link. Without this, the login form is an open redirect: a
    phishing page could send a victim through a genuine ScraperAgent login and
    land them somewhere hostile with the trust of a real login behind it.

    Rejects anything not starting with "/" (absolute URLs, scheme-relative) and
    anything starting with "//" (protocol-relative, which browsers treat as
    cross-origin despite the leading slash).
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


class RequireAuthMiddleware(BaseHTTPMiddleware):
    """Close every non-exempt route to callers without a valid session."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _is_exempt(path):
            return await call_next(request)

        if not config.DASHBOARD_PASSWORD:
            # Fail closed. An empty password must never mean "no password
            # required" — that turns a misconfigured deploy into an open
            # dashboard, which is the exact failure this module exists to
            # prevent. 503 rather than 500: the app is fine, it's unconfigured.
            log.error("DASHBOARD_PASSWORD is not set; refusing to serve %s", path)
            return PlainTextResponse(
                "DASHBOARD_PASSWORD is not set. The dashboard is locked until it is "
                "configured in .env (see .env.example) and the server restarted.",
                status_code=503,
            )

        if request.session.get(SESSION_KEY):
            return await call_next(request)

        return _unauthenticated_response(request, path)


def _unauthenticated_response(request: Request, path: str) -> Response:
    """Turn away an anonymous caller in whichever way its client understands.

    The dashboard polls itself with HTMX. If an expired session answered a poll
    with a 303, HTMX would follow it and swap the entire login page into a
    fragment slot — the user would see a login form nested inside a stale
    dashboard. `HX-Redirect` tells HTMX to navigate the whole window instead.
    """
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse(f"/login?{urlencode({'next': path})}", status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/") -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"next": _safe_next(next), "error": None},
    )


@router.post("/login")
def login_submit(
    request: Request,
    password: str = Form(""),
    next: str = Form("/"),
) -> Response:
    target = _safe_next(next)
    expected = config.DASHBOARD_PASSWORD or ""

    # compare_digest rather than `==`: string equality short-circuits on the
    # first differing byte, which leaks the length of the matching prefix to
    # anyone who can time the response.
    if not expected or not hmac.compare_digest(password, expected):
        log.warning("failed dashboard login attempt from %s", request.client.host if request.client else "?")
        return templates.TemplateResponse(
            request,
            "login.html",
            {"next": target, "error": "Incorrect password."},
            status_code=401,
        )

    request.session[SESSION_KEY] = True
    return RedirectResponse(target, status_code=303)


@router.post("/logout")
def logout(request: Request) -> Response:
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
