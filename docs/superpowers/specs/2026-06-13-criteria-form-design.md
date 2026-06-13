# Step 2 — Criteria Intake Form (Design Spec)

**Status:** approved 2026-06-13
**Scope:** PRD build step 2 only — criteria form + Haiku NL parsing + persist `searches` row + stub `/searches/{id}` page. No graph kickoff yet (that's step 3+).

---

## 1. Architecture

New/changed files:

```
agents/
  __init__.py               # NEW
  criteria_parser.py        # NEW — Haiku call, returns ParsedCriteria

api/
  routes/
    __init__.py             # MODIFY — export the searches router
    searches.py             # NEW — POST /searches/parse, POST /searches, GET /searches/{id}
  main.py                   # MODIFY — include searches router

db/
  repo.py                   # NO CHANGE — create_search() and get_search() already exist

templates/
  index.html                # MODIFY — replace placeholder with the form
  _criteria_form_fields.html# NEW — HTMX fragment of the structured fields
  search_detail.html        # NEW — stub /searches/{id} page

graph/                      # DELETE entire dir (per Q4-A — old pre-PRD stubs)
  graph.py
  state.py
  nodes/*.py

config.py                   # MODIFY — drop the back-compat aliases (MAX_NEGOTIATION_ROUNDS,
                            # DEFAULT_STRATEGY) since the legacy code that used them is gone
```

The new `agents/` directory houses direct Anthropic-SDK callers (Haiku parser now, future ad-hoc Sonnet utilities later) — separate from the LangGraph orchestration that will live in a freshly-rebuilt `graph/` starting at step 3.

---

## 2. Components

### `agents/criteria_parser.py`

One public function:

```python
def parse_criteria(nl_text: str) -> ParsedCriteria: ...
```

- Uses `anthropic.Anthropic()` client with `config.PARSER_MODEL` (`claude-haiku-4-5-20251001`).
- Calls Claude with a system prompt instructing it to extract only the five v1 fields and to leave fields it isn't confident about as `None`/empty.
- Uses Anthropic SDK tool-use with a single tool whose `input_schema` mirrors `ParsedCriteria` — that gives us structured output without parsing JSON out of free text.
- Returns a Pydantic `ParsedCriteria` model.

`ParsedCriteria` (Pydantic):

| Field | Type | Notes |
|---|---|---|
| `title_keywords` | `str` | Free-text query string passed to eBay Browse API `q` |
| `must_not_keywords` | `list[str]` | Rendered as `-word` tokens when we build the eBay query in step 3 |
| `condition_floor` | `Literal["new","refurbished","used","any"] \| None` | Maps to eBay `conditionIds` filter |
| `max_price` | `float \| None` | Currency assumed USD |
| `min_seller_rating` | `float \| None` | Percentage 0–100 |

Notes:
- `max_price` may come back `None` if the NL doesn't mention price — the form then forces the user to type it before submit (it's the only required field on the structured side).
- All other fields are advisory; missing is fine.

### `api/routes/searches.py`

Three routes:

| Method + path | Returns | Purpose |
|---|---|---|
| `POST /searches/parse` | HTML fragment (`_criteria_form_fields.html`) | HTMX swap target. Reads form field `criteria_nl`, calls `parse_criteria`, renders the structured fields pre-filled with the result. |
| `POST /searches` | 303 redirect to `/searches/{id}` | Reads full form (NL + structured), validates with Pydantic, calls `repo.create_search()`, redirects. |
| `GET /searches/{id}` | HTML page (`search_detail.html`) | Renders the persisted criteria back. Placeholder copy: "Reference-price discovery & eBay search land in build steps 3–4." 404 if not found. |

### Templates

**`templates/index.html`** — replace the placeholder block with:
- A `<form>` whose NL `<textarea>` posts to `/searches/parse` via `hx-post`, with `hx-target="#structured-fields"` and `hx-swap="outerHTML"`.
- Below the textarea, a `<div id="structured-fields">` that on first render includes the empty version of `_criteria_form_fields.html`. After parsing, the fragment replaces it pre-filled.
- A separate "Start search" submit button that posts the whole form to `POST /searches` (standard form submit, not HTMX — we want a real redirect).
- Keep the existing "Recent searches" table.

**`templates/_criteria_form_fields.html`** — the five structured fields as a single block:
- `title_keywords` (text input)
- `must_not_keywords` (text input, comma-separated; we split on render)
- `condition_floor` (select: any / used / refurbished / new)
- `max_price` (number input, `required`, `step=0.01`)
- `min_seller_rating` (number input 0–100)

The template receives a `criteria` dict-or-None; when None, all inputs render empty.

**`templates/search_detail.html`** — extends `base.html`. Shows the parsed criteria as a read-only summary plus a "next steps" placeholder line.

### `api/main.py` change

Add `from api.routes import searches as searches_routes` and `app.include_router(searches_routes.router)`.

---

## 3. Data flow

```
User loads /
  → GET /  → templates/index.html (empty form)

User types into NL box, clicks "Parse"
  → HTMX POST /searches/parse with form field criteria_nl
  → criteria_parser.parse_criteria(nl_text) → ParsedCriteria
  → render _criteria_form_fields.html with pre-filled values
  → HTMX swaps #structured-fields outerHTML

User edits fields, clicks "Start search"
  → standard form POST /searches
  → validate with a SearchSubmission Pydantic model (rejects empty/invalid max_price)
  → repo.create_search(nl_text, structured_dict, max_price) → search_id
  → 303 redirect to /searches/{search_id}

User lands on /searches/{id}
  → GET /searches/{id}
  → repo.get_search(id) → row
  → templates/search_detail.html renders parsed criteria
```

---

## 4. Error handling

| Failure | Behavior |
|---|---|
| Haiku API error during `/parse` | Render `_criteria_form_fields.html` empty with an inline error banner ("Couldn't parse — please fill in manually"). Return 200, not 5xx, so HTMX still swaps the fragment cleanly. Log the exception. |
| Haiku returns invalid tool-use payload (Pydantic validation fails) | Same as above — degrade to empty form + banner. |
| User submits `POST /searches` with missing/invalid `max_price` | 400 with the form re-rendered and a field-level error. (FastAPI `RequestValidationError` handler customized for this route, or simpler: validate by hand and re-render.) |
| User opens `/searches/{id}` for an unknown id | 404 with a small "no such search" page. |

No retries on Haiku failure in v1 — the user can just hit Parse again or fill in by hand.

---

## 5. Testing

Three tests, kept lightweight (no live Anthropic calls in CI):

1. **`tests/test_criteria_parser.py`** — monkeypatch `anthropic.Anthropic` to return a canned tool-use response; assert `parse_criteria` returns the expected `ParsedCriteria`. One happy-path test + one "missing fields" test.
2. **`tests/test_searches_routes.py`** — FastAPI `TestClient`:
   - `POST /searches/parse` with a stubbed parser returns the fragment with expected values inlined.
   - `POST /searches` with valid form persists a row and redirects to `/searches/{id}`.
   - `POST /searches` with missing `max_price` returns 400.
   - `GET /searches/{id}` renders the criteria; unknown id returns 404.
3. **Manual smoke test** (documented in the plan, not automated): start `uvicorn`, type "red iPhone 13 mini, 128GB+, unlocked, under $300" into the NL box, click Parse, verify the structured fields populate sanely.

---

## 6. Cleanup actions bundled with this step

- Delete `graph/` (entire directory) — old stubs predate the PRD.
- Remove `MAX_NEGOTIATION_ROUNDS` and `DEFAULT_STRATEGY` back-compat aliases from `config.py` (no live code references them once `graph/` is gone).
- `python-multipart` (required by FastAPI form parsing) is already in `requirements.txt` — no dep change needed.
- `must_not_keywords` arrives from the form as a single comma-separated string; splitting into `list[str]` happens server-side in the `POST /searches` handler before it's passed to `repo.create_search()`.

---

## 7. Out of scope (explicitly deferred)

- Triggering any LangGraph run on submit — that's step 3+.
- Reference-price display on `/searches/{id}` — step 5.
- Listing selection UI — step 5.
- SSE/live updates — step 7+.
- Edit-after-create or re-run-search — backlog.
- Server-side rate limiting on the parse endpoint — personal-use, single user.
