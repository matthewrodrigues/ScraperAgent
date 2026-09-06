"""FastAPI app entry point.

Lifecycle:
    - On startup: init SQLite (creates ./scraperagent.db if missing) +
      spawn the background seller-reply poller (`agents.poller.poll_loop`).
    - On shutdown: cancel the poller task; asyncio waits for clean exit.
    - Mounts: /static for CSS, /templates rendered via Jinja2
    - Routes: /health (liveness), / (search list + new search form)
    - Auth: every route is closed by default; see `api/auth.py` for the
      exempt list and the reasoning behind each entry.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import config
from agents.poller import poll_loop
from api.auth import RequireAuthMiddleware
from api.auth import router as auth_router
from db import repo


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    config.STATIC_DIR.mkdir(parents=True, exist_ok=True)
    repo.init_db()
    # Spawn the background poller. The task reference is stored on the app so
    # we can cancel it cleanly on shutdown — without that the task survives
    # uvicorn reloads and leaks file descriptors.
    poller_task = asyncio.create_task(poll_loop(), name="seller-reply-poller")
    app.state.poller_task = poller_task
    try:
        yield
    finally:
        poller_task.cancel()
        try:
            await poller_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — shutdown errors shouldn't crash teardown
            log.warning("poller shutdown raised: %s", exc)


app = FastAPI(title="ScraperAgent", lifespan=lifespan)

# Middleware order is load-bearing. Starlette builds the stack so that the LAST
# registered middleware is the OUTERMOST, so SessionMiddleware must be added
# after RequireAuthMiddleware — otherwise `request.session` doesn't exist yet
# when the auth check runs and every request 500s.
app.add_middleware(RequireAuthMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=config.SESSION_SECRET,
    session_cookie="scraperagent_session",
    same_site="lax",
    https_only=False,  # served over plain HTTP on localhost; TLS is terminated by the tunnel
)

from api.routes import ebay_notifications_router, searches_router

app.include_router(auth_router)
app.include_router(searches_router)
app.include_router(ebay_notifications_router)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    recent = repo.list_recent_searches(limit=10)
    # Starlette 1.x signature: (request, name, context)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"recent_searches": recent},
    )
