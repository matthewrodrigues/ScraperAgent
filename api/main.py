"""FastAPI app entry point.

Lifecycle:
    - On startup: init SQLite (creates ./scraperagent.db if missing) +
      spawn the background seller-reply poller (`agents.poller.poll_loop`).
    - On shutdown: cancel the poller task; asyncio waits for clean exit.
    - Mounts: /static for CSS, /templates rendered via Jinja2
    - Routes: /health (liveness), / (search list + new search form)
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from agents.poller import poll_loop
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

from api.routes import ebay_notifications_router, searches_router

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
