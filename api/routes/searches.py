"""Routes for the searches resource.

  POST /searches/parse — HTMX fragment swap; calls Haiku and re-renders the
                         structured-fields fragment with extracted values.
  POST /searches        — Form submit; validates, persists, redirects.
  GET  /searches/{id}   — Stub detail page; echoes the persisted criteria.
"""

import logging

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import config
from agents.criteria_parser import parse_criteria

log = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))


@router.post("/searches/parse", response_class=HTMLResponse)
def parse_criteria_endpoint(request: Request, criteria_nl: str = Form("")) -> HTMLResponse:
    """Take the NL box, call Haiku, return the pre-filled structured-fields fragment."""
    criteria = None
    parse_error = None
    if criteria_nl.strip():
        try:
            criteria = parse_criteria(criteria_nl).model_dump()
        except Exception as exc:  # noqa: BLE001 — we deliberately degrade on any failure
            log.warning("criteria parse failed", exc_info=exc)
            parse_error = type(exc).__name__

    return templates.TemplateResponse(
        request,
        "_criteria_form_fields.html",
        {"criteria": criteria, "parse_error": parse_error},
    )
