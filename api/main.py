"""FastAPI app entry point.

Lifecycle:
    - On startup: init SQLite (creates ./scraperagent.db if missing)
    - Mounts: /static for CSS, /templates rendered via Jinja2
    - Routes: /health (liveness), / (search list + new search form — added in next build step)
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from db import repo


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    config.STATIC_DIR.mkdir(parents=True, exist_ok=True)
    repo.init_db()
    yield


app = FastAPI(title="ScraperAgent", lifespan=lifespan)

from api.routes import searches_router

app.include_router(searches_router)
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
