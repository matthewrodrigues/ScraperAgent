"""Routes for the searches resource.

  POST /searches/parse — HTMX fragment swap; calls Haiku and re-renders the
                         structured-fields fragment with extracted values.
  POST /searches        — Form submit; validates, persists, redirects.
  GET  /searches/{id}   — Stub detail page; echoes the persisted criteria.
"""

import logging

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

import config
from agents.criteria_parser import SearchSubmission, parse_criteria
from db import repo

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


def _none_if_blank(v: str) -> str | None:
    """Form fields submit empty strings for unset; the Pydantic model wants None."""
    v = (v or "").strip()
    return v or None


@router.post("/searches")
def submit_search(
    criteria_nl: str = Form(""),
    title_keywords: str = Form(""),
    must_not_keywords_csv: str = Form(""),
    condition_floor: str = Form(""),
    max_price: str = Form(""),
    min_seller_rating: str = Form(""),
) -> RedirectResponse:
    try:
        submission = SearchSubmission(
            criteria_nl=criteria_nl,
            title_keywords=title_keywords,
            must_not_keywords_csv=must_not_keywords_csv,
            condition_floor=_none_if_blank(condition_floor),  # type: ignore[arg-type]
            max_price=float(max_price) if max_price.strip() else None,  # type: ignore[arg-type]
            min_seller_rating=float(min_seller_rating) if min_seller_rating.strip() else None,
        )
    except (ValidationError, ValueError) as exc:
        # In v1 we keep this terse — the form's `required` attribute on max_price
        # blocks 99% of empty submits before they reach us. A 400 is fine for
        # the rare case where someone disables JS / bypasses the attribute.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    search_id = repo.create_search(
        criteria_nl=submission.criteria_nl,
        criteria_structured=submission.to_structured_dict(),
        max_price=submission.max_price,
    )
    return RedirectResponse(url=f"/searches/{search_id}", status_code=303)
