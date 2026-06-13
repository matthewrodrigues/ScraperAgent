# Step 2 — Criteria Intake Form Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the criteria intake form — an HTMX page that takes a free-text "describe what you want" box, parses it into structured fields via a Claude Haiku call, lets the user edit, and persists a `searches` row on submit. After submit, the user lands on a stub `/searches/{id}` page that echoes the parsed criteria (real eBay search + reference price land in build steps 3+).

**Architecture:** New `agents/criteria_parser.py` wraps the Anthropic SDK using tool-use for structured output. A new `api/routes/searches.py` exposes `POST /searches/parse` (HTMX fragment swap), `POST /searches` (form submit + redirect), and `GET /searches/{id}` (stub detail page). The repo's existing `db/repo.create_search()` and `db/repo.get_search()` are reused as-is.

**Tech Stack:** FastAPI + Jinja2 + HTMX (already in `base.html`), Anthropic Python SDK (already in `requirements.txt`), Pydantic v2 (already in `requirements.txt`), pytest + httpx `TestClient` (pytest will be added).

**Spec:** `docs/superpowers/specs/2026-06-13-criteria-form-design.md`

---

## File Structure

**Create:**
- `agents/__init__.py` — empty marker
- `agents/criteria_parser.py` — `ParsedCriteria` Pydantic model + `parse_criteria(nl_text)` function
- `api/routes/searches.py` — three routes for the searches resource
- `templates/_criteria_form_fields.html` — HTMX-swappable fragment of the five structured fields
- `templates/search_detail.html` — stub `/searches/{id}` page
- `tests/test_criteria_parser.py` — unit tests with the Anthropic client monkeypatched
- `tests/test_searches_routes.py` — FastAPI TestClient tests
- `tests/conftest.py` — pytest fixture that points the app at a tmp SQLite DB

**Modify:**
- `api/main.py` — include the new searches router
- `api/routes/__init__.py` — export the new router
- `templates/index.html` — replace the placeholder block with the real form
- `requirements.txt` — add `pytest`
- `config.py` — drop dead back-compat aliases (`MAX_NEGOTIATION_ROUNDS`, `DEFAULT_STRATEGY`)

**Delete:**
- `graph/` (entire directory)
- `browser/` (entire directory)
- `browser_profile/` (entire directory)
- `screenshots/` (entire directory)
- `package.json`, `package-lock.json`

---

## Task 1: Cleanup — remove dead v0 code

**Files:**
- Delete: `graph/`, `browser/`, `browser_profile/`, `screenshots/`, `package.json`, `package-lock.json`
- Modify: `config.py:41-43` (remove three lines of back-compat aliases)

- [ ] **Step 1: Verify nothing live imports the doomed code**

Run:
```bash
grep -rn "from graph\|import graph\|from browser\|import browser" --include="*.py" . | grep -v ".venv\|__pycache__\|docs/"
```
Expected output: empty (no matches outside `.venv`).

- [ ] **Step 2: Delete the dead directories and files**

Run:
```bash
rm -rf graph browser browser_profile screenshots package.json package-lock.json
```

- [ ] **Step 3: Remove back-compat aliases from `config.py`**

Edit `config.py`, delete these three lines (currently 41-43):
```python
# Back-compat aliases used by legacy v0 code paths (negotiate.py); replaced in step 8.
MAX_NEGOTIATION_ROUNDS = MAX_ROUNDS
DEFAULT_STRATEGY = "anchor_low"
```

- [ ] **Step 4: Verify the app still boots**

Run:
```bash
python -c "from api.main import app; print('ok')"
```
Expected: `ok` (no ImportError).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: delete v0 graph/browser/playwright leftovers"
```

---

## Task 2: Add pytest and a shared test fixture for an isolated DB

**Files:**
- Modify: `requirements.txt`
- Create: `tests/conftest.py`

- [ ] **Step 1: Add pytest to `requirements.txt`**

Append this line to the end of `requirements.txt`:
```
pytest==8.3.4
```

- [ ] **Step 2: Install it**

Run:
```bash
pip install pytest==8.3.4
```
Expected: "Successfully installed pytest-8.3.4" (or already-installed).

- [ ] **Step 3: Write `tests/conftest.py`**

The whole point: every test should run against a fresh tmp DB, not the real `scraperagent.db`. We monkey-patch `config.DB_PATH` before any code reads it, then call `repo.init_db()` to materialize the schema.

```python
"""Shared pytest fixtures.

Every test gets a fresh SQLite file in a tmp dir so tests can't pollute
./scraperagent.db or each other.
"""

import pytest

import config
from db import repo


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    repo.init_db()
    return db_path
```

- [ ] **Step 4: Verify pytest discovers tests dir**

Run:
```bash
pytest tests/ --collect-only -q
```
Expected: no errors (zero tests collected is fine).

- [ ] **Step 5: Commit**

```bash
git add requirements.txt tests/conftest.py
git commit -m "test: add pytest and tmp-db fixture"
```

---

## Task 3: `ParsedCriteria` Pydantic model

**Files:**
- Create: `agents/__init__.py`
- Create: `agents/criteria_parser.py`
- Create: `tests/test_criteria_parser.py`

- [ ] **Step 1: Write the failing model test**

Create `tests/test_criteria_parser.py`:
```python
"""Tests for agents.criteria_parser."""

import pytest

from agents.criteria_parser import ParsedCriteria


def test_parsed_criteria_all_fields_optional():
    """Every field has a safe default so a missing-info parse still validates."""
    pc = ParsedCriteria()
    assert pc.title_keywords == ""
    assert pc.must_not_keywords == []
    assert pc.condition_floor is None
    assert pc.max_price is None
    assert pc.min_seller_rating is None


def test_parsed_criteria_condition_floor_constrained():
    """condition_floor only accepts the documented literals."""
    with pytest.raises(ValueError):
        ParsedCriteria(condition_floor="brand-new")  # not in the Literal set
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_criteria_parser.py -v
```
Expected: FAIL with `ModuleNotFoundError: No module named 'agents'`.

- [ ] **Step 3: Create the `agents/` package**

Create `agents/__init__.py` as an empty file (zero bytes).

- [ ] **Step 4: Write `agents/criteria_parser.py` with just the model**

Create `agents/criteria_parser.py`:
```python
"""Claude Haiku-backed natural-language parser for the criteria intake form.

The form sends a free-text "describe what you want" string; we ask Haiku to
extract the five structured fields the eBay Browse API can filter on. We use
the Anthropic SDK's tool-use feature so the model's output is validated against
a JSON schema server-side — we never parse free-form JSON out of a text reply.
"""

from typing import Literal

from pydantic import BaseModel, Field


ConditionFloor = Literal["new", "refurbished", "used", "any"]


class ParsedCriteria(BaseModel):
    """The five v1 structured fields. Every field is optional so a parse with
    low-confidence extraction still validates — the form lets the user fill in
    anything Haiku missed."""

    title_keywords: str = Field(
        default="",
        description="Free-text query passed to eBay Browse API `q`.",
    )
    must_not_keywords: list[str] = Field(
        default_factory=list,
        description="Terms to exclude; rendered as `-word` tokens in the eBay query.",
    )
    condition_floor: ConditionFloor | None = Field(
        default=None,
        description="Minimum acceptable condition; maps to eBay conditionIds filter.",
    )
    max_price: float | None = Field(
        default=None,
        description="USD ceiling. Required at form-submit time but may be None at parse time.",
    )
    min_seller_rating: float | None = Field(
        default=None,
        ge=0,
        le=100,
        description="Percentage 0-100.",
    )
```

- [ ] **Step 5: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_criteria_parser.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Commit**

```bash
git add agents/ tests/test_criteria_parser.py
git commit -m "feat(agents): add ParsedCriteria pydantic model"
```

---

## Task 4: Implement `parse_criteria()` against the Anthropic SDK

**Files:**
- Modify: `agents/criteria_parser.py`
- Modify: `tests/test_criteria_parser.py`

The plan: define a single tool `record_criteria` whose `input_schema` matches `ParsedCriteria`. Call `client.messages.create(...)` with `tool_choice={"type": "tool", "name": "record_criteria"}` to force the model to call the tool. Pull the tool input dict out of the response and feed it to `ParsedCriteria.model_validate(...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_criteria_parser.py`:
```python
from unittest.mock import MagicMock, patch


def _fake_anthropic_response(tool_input: dict):
    """Build a minimal stub of the Anthropic Messages response shape."""
    response = MagicMock()
    block = MagicMock()
    block.type = "tool_use"
    block.name = "record_criteria"
    block.input = tool_input
    response.content = [block]
    return response


@patch("agents.criteria_parser.Anthropic")
def test_parse_criteria_happy_path(anthropic_cls):
    from agents.criteria_parser import parse_criteria

    client = anthropic_cls.return_value
    client.messages.create.return_value = _fake_anthropic_response({
        "title_keywords": "iPhone 13 mini red 128GB unlocked",
        "must_not_keywords": ["cracked", "broken"],
        "condition_floor": "used",
        "max_price": 300.0,
        "min_seller_rating": 98.0,
    })

    result = parse_criteria("red iPhone 13 mini, 128GB+, unlocked, no cracks, under $300, rep 98+")

    assert result.title_keywords == "iPhone 13 mini red 128GB unlocked"
    assert result.must_not_keywords == ["cracked", "broken"]
    assert result.condition_floor == "used"
    assert result.max_price == 300.0
    assert result.min_seller_rating == 98.0


@patch("agents.criteria_parser.Anthropic")
def test_parse_criteria_partial_extraction(anthropic_cls):
    """Haiku may only fill some fields — those left out get defaults."""
    from agents.criteria_parser import parse_criteria

    client = anthropic_cls.return_value
    client.messages.create.return_value = _fake_anthropic_response({
        "title_keywords": "vintage Polaroid camera",
    })

    result = parse_criteria("vintage Polaroid camera")
    assert result.title_keywords == "vintage Polaroid camera"
    assert result.must_not_keywords == []
    assert result.max_price is None


@patch("agents.criteria_parser.Anthropic")
def test_parse_criteria_no_tool_use_block_raises(anthropic_cls):
    """If Haiku ignores the tool (shouldn't happen with tool_choice=tool), surface it."""
    from agents.criteria_parser import parse_criteria, CriteriaParseError

    client = anthropic_cls.return_value
    response = MagicMock()
    text_block = MagicMock()
    text_block.type = "text"
    response.content = [text_block]
    client.messages.create.return_value = response

    with pytest.raises(CriteriaParseError):
        parse_criteria("anything")
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_criteria_parser.py -v
```
Expected: 3 new tests FAIL with `ImportError: cannot import name 'parse_criteria'`.

- [ ] **Step 3: Implement `parse_criteria()` and `CriteriaParseError`**

Append to `agents/criteria_parser.py`:
```python
from anthropic import Anthropic

import config


class CriteriaParseError(RuntimeError):
    """Raised when Haiku's response doesn't contain a usable tool-use block."""


_SYSTEM_PROMPT = (
    "You extract structured shopping criteria from a buyer's natural-language "
    "description of an item they want to find on eBay. Call the `record_criteria` "
    "tool exactly once. Leave any field you can't confidently infer at its default "
    "(empty string, empty list, or null). Never guess a max price — only fill it "
    "when the buyer states one explicitly."
)


_RECORD_CRITERIA_TOOL = {
    "name": "record_criteria",
    "description": "Record the structured shopping criteria extracted from the buyer's description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title_keywords": {
                "type": "string",
                "description": "Concise eBay search query — the words you'd type into the search bar.",
            },
            "must_not_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Terms to exclude (e.g. 'broken', 'parts only').",
            },
            "condition_floor": {
                "type": ["string", "null"],
                "enum": ["new", "refurbished", "used", "any", None],
                "description": "Minimum acceptable condition, or null if unspecified.",
            },
            "max_price": {
                "type": ["number", "null"],
                "description": "USD price ceiling if the buyer stated one; otherwise null.",
            },
            "min_seller_rating": {
                "type": ["number", "null"],
                "minimum": 0,
                "maximum": 100,
                "description": "Minimum seller feedback percentage if specified.",
            },
        },
        "required": [],
    },
}


def parse_criteria(nl_text: str) -> ParsedCriteria:
    """Ask Haiku to extract structured criteria from a free-text description."""
    client = Anthropic(api_key=config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=config.PARSER_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPT,
        tools=[_RECORD_CRITERIA_TOOL],
        tool_choice={"type": "tool", "name": "record_criteria"},
        messages=[{"role": "user", "content": nl_text}],
    )

    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "record_criteria":
            return ParsedCriteria.model_validate(block.input)

    raise CriteriaParseError("Haiku response did not contain a record_criteria tool_use block")
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_criteria_parser.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/criteria_parser.py tests/test_criteria_parser.py
git commit -m "feat(agents): parse_criteria via Anthropic tool-use"
```

---

## Task 5: `SearchSubmission` validation model

**Files:**
- Modify: `agents/criteria_parser.py` (add `SearchSubmission` next to `ParsedCriteria`)
- Modify: `tests/test_criteria_parser.py`

Why this lives next to `ParsedCriteria`: same domain (criteria shapes), tiny model, no need for a new file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_criteria_parser.py`:
```python
def test_search_submission_requires_max_price():
    from agents.criteria_parser import SearchSubmission

    with pytest.raises(ValueError):
        SearchSubmission(criteria_nl="anything", max_price=None)


def test_search_submission_splits_must_not_keywords():
    from agents.criteria_parser import SearchSubmission

    sub = SearchSubmission(
        criteria_nl="red iPhone",
        title_keywords="iPhone 13 mini red",
        must_not_keywords_csv=" cracked , broken ,  ,water damage",
        condition_floor="used",
        max_price=300.0,
        min_seller_rating=98.0,
    )

    assert sub.must_not_keywords == ["cracked", "broken", "water damage"]


def test_search_submission_to_structured_dict():
    from agents.criteria_parser import SearchSubmission

    sub = SearchSubmission(
        criteria_nl="x",
        title_keywords="thing",
        must_not_keywords_csv="",
        condition_floor=None,
        max_price=50.0,
        min_seller_rating=None,
    )

    d = sub.to_structured_dict()
    assert d == {
        "title_keywords": "thing",
        "must_not_keywords": [],
        "condition_floor": None,
        "min_seller_rating": None,
    }
    # max_price is stored in its own column, not the JSON blob
    assert "max_price" not in d
    assert "criteria_nl" not in d
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_criteria_parser.py::test_search_submission_requires_max_price -v
```
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Add `SearchSubmission` to `agents/criteria_parser.py`**

Append to `agents/criteria_parser.py`:
```python
class SearchSubmission(BaseModel):
    """Validated form payload for `POST /searches`.

    Separate from `ParsedCriteria` because:
      - `max_price` is required here (we won't persist a search without one)
      - the form sends `must_not_keywords` as a single CSV string; we split here
      - `criteria_nl` is part of the persisted row but not part of the parsed shape
    """

    criteria_nl: str
    title_keywords: str = ""
    must_not_keywords_csv: str = ""
    condition_floor: ConditionFloor | None = None
    max_price: float = Field(gt=0)
    min_seller_rating: float | None = Field(default=None, ge=0, le=100)

    @property
    def must_not_keywords(self) -> list[str]:
        return [kw.strip() for kw in self.must_not_keywords_csv.split(",") if kw.strip()]

    def to_structured_dict(self) -> dict:
        """Shape stored in `searches.criteria_structured_json`. Excludes `criteria_nl`
        and `max_price` because those have dedicated columns."""
        return {
            "title_keywords": self.title_keywords,
            "must_not_keywords": self.must_not_keywords,
            "condition_floor": self.condition_floor,
            "min_seller_rating": self.min_seller_rating,
        }
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_criteria_parser.py -v
```
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add agents/criteria_parser.py tests/test_criteria_parser.py
git commit -m "feat(agents): add SearchSubmission form-validation model"
```

---

## Task 6: Templates — `_criteria_form_fields.html` and `search_detail.html`

**Files:**
- Create: `templates/_criteria_form_fields.html`
- Create: `templates/search_detail.html`

No test for templates in isolation — they're exercised by the route tests in the next task.

- [ ] **Step 1: Write `templates/_criteria_form_fields.html`**

This fragment is rendered on initial page load (empty) and re-rendered by the `POST /searches/parse` endpoint (pre-filled). It's intentionally a `<div>` with an id so HTMX `outerHTML` swap replaces the whole fragment cleanly.

```html
{# Structured criteria fields. Rendered standalone by POST /searches/parse,
   and inlined into the form on GET /. The form submit (POST /searches)
   reads these field names directly. #}
<div id="structured-fields" class="structured-fields">
    {% if parse_error %}
    <p class="error">Couldn't parse — please fill in manually. ({{ parse_error }})</p>
    {% endif %}

    <label>
        Title keywords
        <input type="text" name="title_keywords" value="{{ criteria.title_keywords if criteria else '' }}">
    </label>

    <label>
        Exclude (comma-separated)
        <input type="text" name="must_not_keywords_csv"
               value="{{ (criteria.must_not_keywords | join(', ')) if criteria and criteria.must_not_keywords else '' }}"
               placeholder="broken, parts only, water damage">
    </label>

    <label>
        Condition floor
        <select name="condition_floor">
            <option value="">— any —</option>
            {% set sel = (criteria.condition_floor if criteria else None) %}
            <option value="used"        {% if sel == 'used' %}selected{% endif %}>Used or better</option>
            <option value="refurbished" {% if sel == 'refurbished' %}selected{% endif %}>Refurbished or better</option>
            <option value="new"         {% if sel == 'new' %}selected{% endif %}>New only</option>
        </select>
    </label>

    <label>
        Max price (USD) <span class="req">*</span>
        <input type="number" name="max_price" step="0.01" min="0" required
               value="{{ criteria.max_price if criteria and criteria.max_price is not none else '' }}">
    </label>

    <label>
        Min seller rating (%)
        <input type="number" name="min_seller_rating" step="0.1" min="0" max="100"
               value="{{ criteria.min_seller_rating if criteria and criteria.min_seller_rating is not none else '' }}">
    </label>
</div>
```

- [ ] **Step 2: Write `templates/search_detail.html`**

```html
{% extends "base.html" %}
{% block title %}Search #{{ search.id }} — ScraperAgent{% endblock %}
{% block content %}
<section class="search-detail">
    <h1>Search #{{ search.id }}</h1>
    <p class="muted">Submitted {{ search.created_at }} · status: <strong>{{ search.status }}</strong></p>

    <h2>What you asked for</h2>
    <blockquote>{{ search.criteria_nl }}</blockquote>

    <h2>Parsed criteria</h2>
    <dl class="criteria">
        <dt>Title keywords</dt>
        <dd>{{ search.criteria_structured.title_keywords or "—" }}</dd>

        <dt>Exclude</dt>
        <dd>
            {% set excl = search.criteria_structured.must_not_keywords %}
            {{ excl | join(", ") if excl else "—" }}
        </dd>

        <dt>Condition floor</dt>
        <dd>{{ search.criteria_structured.condition_floor or "any" }}</dd>

        <dt>Max price</dt>
        <dd>${{ "%.2f"|format(search.max_price) }}</dd>

        <dt>Min seller rating</dt>
        <dd>
            {% set msr = search.criteria_structured.min_seller_rating %}
            {{ "%.1f%%"|format(msr) if msr is not none else "—" }}
        </dd>
    </dl>

    <p class="placeholder">Reference-price discovery & eBay search land in build steps 3–4.</p>
</section>
{% endblock %}
```

- [ ] **Step 3: Commit**

```bash
git add templates/_criteria_form_fields.html templates/search_detail.html
git commit -m "feat(templates): criteria form fragment + search detail stub"
```

---

## Task 7: `searches` router — `POST /searches/parse`

**Files:**
- Create: `api/routes/searches.py`
- Modify: `api/routes/__init__.py`
- Create: `tests/test_searches_routes.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_searches_routes.py`:
```python
"""End-to-end tests for the searches router using FastAPI's TestClient."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agents.criteria_parser import ParsedCriteria
from api.main import app


@pytest.fixture
def client(tmp_db):
    return TestClient(app)


@patch("api.routes.searches.parse_criteria")
def test_post_searches_parse_returns_fragment_with_values(mock_parse, client):
    mock_parse.return_value = ParsedCriteria(
        title_keywords="iPhone 13 mini red 128GB",
        must_not_keywords=["cracked", "broken"],
        condition_floor="used",
        max_price=300.0,
        min_seller_rating=98.0,
    )

    resp = client.post("/searches/parse", data={"criteria_nl": "red iPhone 13 mini"})

    assert resp.status_code == 200
    body = resp.text
    assert 'name="title_keywords"' in body
    assert 'value="iPhone 13 mini red 128GB"' in body
    assert "cracked, broken" in body
    assert 'value="300.0"' in body
    # selected option for used
    assert 'value="used"        selected' in body or 'value="used" selected' in body


@patch("api.routes.searches.parse_criteria", side_effect=RuntimeError("api down"))
def test_post_searches_parse_haiku_failure_renders_empty_with_banner(mock_parse, client):
    resp = client.post("/searches/parse", data={"criteria_nl": "anything"})
    assert resp.status_code == 200
    assert "Couldn't parse" in resp.text
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_searches_routes.py -v
```
Expected: FAIL with `404 Not Found` (the route doesn't exist yet).

- [ ] **Step 3: Create `api/routes/searches.py` with just the parse route**

```python
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
```

- [ ] **Step 4: Wire the router into `api/routes/__init__.py`**

Replace the contents of `api/routes/__init__.py` with:
```python
from api.routes.searches import router as searches_router

__all__ = ["searches_router"]
```

- [ ] **Step 5: Include the router in `api/main.py`**

Edit `api/main.py`. After the `app = FastAPI(...)` line (around line 28), add:
```python
from api.routes import searches_router

app.include_router(searches_router)
```

- [ ] **Step 6: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_searches_routes.py -v
```
Expected: 2 passed.

- [ ] **Step 7: Commit**

```bash
git add api/routes/__init__.py api/routes/searches.py api/main.py tests/test_searches_routes.py
git commit -m "feat(routes): POST /searches/parse with Haiku-backed HTMX fragment"
```

---

## Task 8: `POST /searches` submit handler

**Files:**
- Modify: `api/routes/searches.py`
- Modify: `tests/test_searches_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_searches_routes.py`:
```python
from db import repo


def test_post_searches_persists_and_redirects(client):
    resp = client.post(
        "/searches",
        data={
            "criteria_nl": "red iPhone 13 mini under $300",
            "title_keywords": "iPhone 13 mini red 128GB",
            "must_not_keywords_csv": "cracked, broken",
            "condition_floor": "used",
            "max_price": "300.00",
            "min_seller_rating": "98",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/searches/")

    search_id = int(resp.headers["location"].rsplit("/", 1)[1])
    row = repo.get_search(search_id)
    assert row is not None
    assert row["criteria_nl"] == "red iPhone 13 mini under $300"
    assert row["max_price"] == 300.00
    assert row["criteria_structured"]["must_not_keywords"] == ["cracked", "broken"]
    assert row["criteria_structured"]["title_keywords"] == "iPhone 13 mini red 128GB"
    assert row["criteria_structured"]["condition_floor"] == "used"


def test_post_searches_missing_max_price_returns_400(client):
    resp = client.post(
        "/searches",
        data={
            "criteria_nl": "anything",
            "title_keywords": "thing",
            "must_not_keywords_csv": "",
            "condition_floor": "",
            "max_price": "",
            "min_seller_rating": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_post_searches_empty_condition_floor_treated_as_null(client):
    """Form sends `condition_floor=""` for "any" — we must coerce to None."""
    resp = client.post(
        "/searches",
        data={
            "criteria_nl": "thing",
            "title_keywords": "thing",
            "must_not_keywords_csv": "",
            "condition_floor": "",
            "max_price": "50",
            "min_seller_rating": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    search_id = int(resp.headers["location"].rsplit("/", 1)[1])
    row = repo.get_search(search_id)
    assert row["criteria_structured"]["condition_floor"] is None
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_searches_routes.py -v
```
Expected: 3 new tests FAIL with 405 or 404.

- [ ] **Step 3: Add the submit handler**

Append to `api/routes/searches.py`:
```python
from fastapi import HTTPException
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from agents.criteria_parser import SearchSubmission
from db import repo


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
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_searches_routes.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add api/routes/searches.py tests/test_searches_routes.py
git commit -m "feat(routes): POST /searches persists row + 303 redirect"
```

---

## Task 9: `GET /searches/{id}` detail stub

**Files:**
- Modify: `api/routes/searches.py`
- Modify: `tests/test_searches_routes.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_searches_routes.py`:
```python
def test_get_search_detail_renders_persisted_row(client):
    search_id = repo.create_search(
        criteria_nl="red iPhone 13 mini under $300",
        criteria_structured={
            "title_keywords": "iPhone 13 mini red 128GB",
            "must_not_keywords": ["cracked"],
            "condition_floor": "used",
            "min_seller_rating": 98.0,
        },
        max_price=300.0,
    )

    resp = client.get(f"/searches/{search_id}")
    assert resp.status_code == 200
    assert "red iPhone 13 mini under $300" in resp.text
    assert "iPhone 13 mini red 128GB" in resp.text
    assert "cracked" in resp.text
    assert "$300.00" in resp.text


def test_get_search_detail_unknown_id_returns_404(client):
    resp = client.get("/searches/999999")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_searches_routes.py -v
```
Expected: 2 new tests FAIL with 405 or wrong content.

- [ ] **Step 3: Add the detail route**

Append to `api/routes/searches.py`:
```python
@router.get("/searches/{search_id}", response_class=HTMLResponse)
def search_detail(request: Request, search_id: int) -> HTMLResponse:
    row = repo.get_search(search_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"search {search_id} not found")
    return templates.TemplateResponse(request, "search_detail.html", {"search": row})
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_searches_routes.py -v
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add api/routes/searches.py tests/test_searches_routes.py
git commit -m "feat(routes): GET /searches/{id} stub detail page"
```

---

## Task 10: Wire the form into `templates/index.html`

**Files:**
- Modify: `templates/index.html`

- [ ] **Step 1: Verify HTMX is loaded by `base.html`**

Run:
```bash
grep -n "htmx" templates/base.html
```
Expected: a line referencing `/static/htmx.min.js`.

- [ ] **Step 2: Self-host HTMX and wire the `<script>` tag**

`base.html` already comments that HTMX is intended to be served from `/static/htmx.min.js`, but the file isn't actually present and there's no `<script>` tag rendering it. Download it once (no CDN, no SRI worries) and add the tag.

Run:
```bash
curl -fL --create-dirs -o static/htmx.min.js https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js
ls -l static/htmx.min.js
```
Expected: file exists, ~48KB.

Then edit `templates/base.html`. After the `{# HTMX is self-hosted... #}` comment, insert this `<script>` tag (inside `<head>`, before `{% block head_extra %}`):
```html
<script src="/static/htmx.min.js" defer></script>
```

No `integrity=` / `crossorigin=` needed — same-origin assets aren't subject to CDN-compromise risk. Commit the change.

```bash
git add static/htmx.min.js templates/base.html
git commit -m "feat(ui): self-host HTMX (no CDN dependency)"
```

- [ ] **Step 3: Rewrite `templates/index.html`**

Replace the file's contents with:
```html
{% extends "base.html" %}
{% block title %}ScraperAgent — Home{% endblock %}
{% block content %}
<section class="hero">
    <h1>ScraperAgent</h1>
    <p>Tell me what you want. I'll find it, price-check it, and negotiate on eBay.</p>
</section>

<section class="new-search">
    <h2>New search</h2>
    <form action="/searches" method="post" class="criteria-form">
        <label class="nl">
            Describe what you want
            <textarea name="criteria_nl" rows="3"
                      placeholder="red iPhone 13 mini, 128GB+, unlocked, under $300, seller rating 98%+"></textarea>
        </label>

        <button type="button"
                hx-post="/searches/parse"
                hx-include="[name='criteria_nl']"
                hx-target="#structured-fields"
                hx-swap="outerHTML">
            Parse with Claude
        </button>

        {% include "_criteria_form_fields.html" %}

        <button type="submit" class="primary">Start search</button>
    </form>
</section>

<section>
    <h2>Recent searches</h2>
    {% if recent_searches %}
        <table>
            <thead><tr><th>#</th><th>Query</th><th>Max</th><th>Status</th><th>Started</th></tr></thead>
            <tbody>
                {% for s in recent_searches %}
                <tr>
                    <td><a href="/searches/{{ s.id }}">{{ s.id }}</a></td>
                    <td>{{ s.criteria_nl }}</td>
                    <td>${{ "%.2f"|format(s.max_price) }}</td>
                    <td>{{ s.status }}</td>
                    <td>{{ s.created_at }}</td>
                </tr>
                {% endfor %}
            </tbody>
        </table>
    {% else %}
        <p>No searches yet.</p>
    {% endif %}
</section>
{% endblock %}
```

- [ ] **Step 4: Run the full test suite**

Run:
```bash
pytest tests/ -v
```
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add templates/
git commit -m "feat(ui): criteria intake form with HTMX parse + submit"
```

---

## Task 11: Manual smoke test

**Files:** none

- [ ] **Step 1: Start the dev server**

Run:
```bash
python main.py --reload
```
Expected: `Uvicorn running on http://127.0.0.1:8000`.

- [ ] **Step 2: Open the page in a browser**

Visit `http://127.0.0.1:8000/`. You should see the hero, the NL textarea, the empty structured fields, "Parse with Claude" and "Start search" buttons, and the Recent-searches table.

- [ ] **Step 3: Test the parse flow**

Type into the NL box:
```
red iPhone 13 mini, 128GB+, unlocked, no cracks, under $300, seller rep 98+
```
Click **Parse with Claude**. After a beat, the structured fields should populate:
- Title keywords: something like `iPhone 13 mini red 128GB unlocked`
- Exclude: `cracked` (and possibly `broken`)
- Condition floor: `Used or better`
- Max price: `300`
- Min seller rating: `98`

- [ ] **Step 4: Test the submit flow**

Optionally edit a field, then click **Start search**. You should land on `/searches/{id}` and see the parsed criteria echoed back plus the placeholder line about steps 3–4.

- [ ] **Step 5: Test the Recent searches link**

Go back to `/`. The new search should appear in the Recent-searches table with a clickable id linking to `/searches/{id}`.

- [ ] **Step 6: If everything looks right, commit a smoke-test note (optional)**

No code change needed — this task is a sign-off, not an artifact.

---

## Self-review notes

- **Spec coverage:** Tasks 3–4 cover §2 (criteria parser). Tasks 6, 7, 8, 9, 10 cover §2 (routes & templates). Task 1 covers §6 (cleanup). Tasks 7 and 8 cover §4 (error handling: Haiku failure renders empty fragment with banner; missing max_price returns 400). Tests in Tasks 3, 4, 5, 7, 8, 9 cover §5. Task 11 covers the manual smoke test from §5.
- **Type consistency:** `ParsedCriteria.must_not_keywords` is `list[str]` everywhere; `SearchSubmission.must_not_keywords_csv` is the form-input field and `.must_not_keywords` is the split property. `condition_floor` is the `ConditionFloor` Literal in both models. `max_price` is `float | None` in `ParsedCriteria` (Haiku might omit it) and `float` (required, >0) in `SearchSubmission` (form submit requires it). These map cleanly.
- **Placeholder scan:** No TBDs. Every code step shows complete code; every command step shows expected output.
- **Pre-req fixed in plan:** The repo's `base.html` references `/static/htmx.min.js` but neither the file nor an actual `<script>` tag exist. Task 10 Step 2 downloads HTMX into `static/` and adds the tag — same-origin, no CDN/SRI concerns.
