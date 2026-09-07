# Key Broker Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a transparent reverse proxy that lets invited friends run their own local copy of ScraperAgent using the owner's Anthropic and Apify keys, with per-friend and global monthly spend caps.

**Architecture:** Both vendor SDKs accept a base-URL override, so the broker forwards native wire protocols with the owner's key substituted, meters spend from responses, and enforces quota before forwarding. The broker holds only `friends` and `spend` — no friend data. Client-side, a kwargs factory chooses between broker and direct-vendor credentials.

**Tech Stack:** Python 3.12, FastAPI, httpx 0.28.1, SQLite, pytest. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-07-key-broker-phase1-design.md`

## Global Constraints

- The existing 281 tests must pass unchanged at every commit. This is the regression check on spec goal 4.
- No new entries in `requirements.txt`. FastAPI, httpx, and pytest are already present.
- Broker code lives in `keybroker/` — never `broker/`, to avoid visual collision with the existing `browser/` package.
- SQLite conventions follow `db/schema.sql`: `INTEGER PRIMARY KEY AUTOINCREMENT`, ISO-8601 UTC text timestamps via `datetime('now')`, `cost_usd REAL`, `ON DELETE CASCADE`.
- Timestamp string format is `YYYY-MM-DD HH:MM:SS` (SQLite's `datetime('now')` output). All month-boundary comparisons must use this exact format.
- Quota rejections return **402**, never 429. The Anthropic SDK retries 429 twice and the Apify SDK four times.
- `BROKER_MAX_SINGLE_CALL_USD = 0.30`, `MAX_TOKENS_CEILING = 4096`, `MAX_BODY_BYTES = 262144`. These are module constants, not env vars. The 0.30 reservation is only valid while the other two hold.
- Tests never touch the live network. Upstream calls are stubbed.

## File Structure

| File | Responsibility |
|---|---|
| `integrations/clients.py` | Resolve vendor SDK constructor kwargs (broker vs direct) |
| `keybroker/__init__.py` | Package marker |
| `keybroker/db.py` | Schema, connection, friend and spend persistence |
| `keybroker/auth.py` | Token generation, hashing, per-vendor header extraction |
| `keybroker/quota.py` | Month-to-date sums, headroom reservation, cap checks |
| `keybroker/clamps.py` | `max_tokens` ceiling, body cap, `max_total_charge_usd` clamp, streaming detection |
| `keybroker/meter.py` | Parse vendor responses into `spend` rows |
| `keybroker/app.py` | FastAPI proxy: routing, key injection, error mapping |
| `keybroker/__main__.py` | uvicorn entry point |
| `scripts/add_friend.py` | Create a friend, print token once |
| `scripts/revoke_friend.py` | Set `revoked_at` |
| `scripts/spend_report.py` | Per-friend month-to-date report |

---

### Task 1: Client kwargs factory and config

Wires broker settings into `config.py` and routes all three vendor call sites through a single credential resolver. Returns kwargs rather than clients so existing SDK-class patches keep working.

**Files:**
- Modify: `config.py` (append after the Paths section, which ends at the `EBAY_BROWSER_SCREENSHOTS_DIR` assignment around line 205)
- Create: `integrations/clients.py`
- Modify: `agents/criteria_parser.py:100`
- Modify: `agents/negotiate.py:134`
- Modify: `pricing/google_shopping.py:50`
- Test: `tests/test_clients.py`

**Interfaces:**
- Consumes: `config.ANTHROPIC_API_KEY`, `config.APIFY_TOKEN`, `config.DATA_DIR`
- Produces: `config.BROKER_URL`, `config.BROKER_TOKEN`, `config.BROKER_DB_PATH`, `config.BROKER_PORT`, `config.BROKER_GLOBAL_MONTHLY_BUDGET_USD`; `integrations.clients.anthropic_kwargs() -> dict[str, Any]`, `integrations.clients.apify_kwargs() -> dict[str, Any]`

- [ ] **Step 1: Write the failing test**

Create `tests/test_clients.py`:

```python
"""Tests for integrations.clients — vendor credential resolution.

Returns constructor kwargs rather than clients so that call sites keep their
own SDK construction, which is what the rest of the suite patches.
"""

import pytest

import config
from integrations import clients


@pytest.fixture
def direct(monkeypatch):
    monkeypatch.setattr(config, "BROKER_URL", "")
    monkeypatch.setattr(config, "BROKER_TOKEN", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-real-anthropic")
    monkeypatch.setattr(config, "APIFY_TOKEN", "apify-real-token")


@pytest.fixture
def brokered(monkeypatch):
    monkeypatch.setattr(config, "BROKER_URL", "https://broker.example.ts.net")
    monkeypatch.setattr(config, "BROKER_TOKEN", "sa_friendtoken")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-real-anthropic")
    monkeypatch.setattr(config, "APIFY_TOKEN", "apify-real-token")


def test_anthropic_kwargs_direct_uses_real_key(direct):
    assert clients.anthropic_kwargs() == {"api_key": "sk-real-anthropic"}


def test_apify_kwargs_direct_uses_real_token(direct):
    assert clients.apify_kwargs() == {"token": "apify-real-token"}


def test_anthropic_kwargs_brokered_points_at_broker(brokered):
    kwargs = clients.anthropic_kwargs()
    assert kwargs["api_key"] == "sa_friendtoken"
    assert kwargs["base_url"] == "https://broker.example.ts.net/anthropic"


def test_apify_kwargs_brokered_points_at_broker(brokered):
    kwargs = clients.apify_kwargs()
    assert kwargs["token"] == "sa_friendtoken"
    assert kwargs["api_url"] == "https://broker.example.ts.net/apify"


def test_brokered_kwargs_never_leak_the_real_vendor_key(brokered):
    assert "sk-real-anthropic" not in clients.anthropic_kwargs().values()
    assert "apify-real-token" not in clients.apify_kwargs().values()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clients.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'integrations.clients'`

- [ ] **Step 3: Add broker config**

Append to `config.py`, after the Paths section:

```python
# ---- Key broker ----
# Client side. When BROKER_URL is set the app sends Anthropic and Apify traffic
# through a broker holding someone else's keys (see
# docs/superpowers/specs/2026-09-07-key-broker-phase1-design.md). Unset means
# direct vendor calls with local keys — the owner's machine, and the escape
# hatch for any friend whose broker is down.
BROKER_URL = os.getenv("SCRAPERAGENT_BROKER_URL", "").rstrip("/")
BROKER_TOKEN = os.getenv("SCRAPERAGENT_BROKER_TOKEN", "")

if BROKER_URL and not BROKER_TOKEN:
    # Fail fast rather than silently falling back to vendor keys a friend does
    # not have — that failure would surface as a confusing 401 from Anthropic.
    raise RuntimeError(
        "SCRAPERAGENT_BROKER_URL is set but SCRAPERAGENT_BROKER_TOKEN is empty. "
        "Set both, or neither (to use your own ANTHROPIC_API_KEY / APIFY_TOKEN)."
    )

# Server side. Only the broker host reads these.
BROKER_DB_PATH = Path(os.getenv("BROKER_DB_PATH", str(DATA_DIR / "broker.db")))
BROKER_PORT = int(os.getenv("BROKER_PORT", "8001"))
BROKER_GLOBAL_MONTHLY_BUDGET_USD = float(
    os.getenv("BROKER_GLOBAL_MONTHLY_BUDGET_USD", "25.00")
)
```

- [ ] **Step 4: Create the kwargs factory**

Create `integrations/clients.py`:

```python
"""Vendor SDK credential resolution.

Returns constructor *kwargs*, not constructed clients. Call sites keep their own
`Anthropic(...)` / `ApifyClient(...)` construction because the test suite patches
those classes where they are used; moving construction here would break ~20
existing patches. See the Phase 1 design spec, section 10.
"""

from typing import Any

import config


def anthropic_kwargs() -> dict[str, Any]:
    """Constructor kwargs for `anthropic.Anthropic`."""
    if config.BROKER_URL:
        return {
            "api_key": config.BROKER_TOKEN,
            "base_url": f"{config.BROKER_URL}/anthropic",
        }
    return {"api_key": config.ANTHROPIC_API_KEY}


def apify_kwargs() -> dict[str, Any]:
    """Constructor kwargs for `apify_client.ApifyClient`."""
    if config.BROKER_URL:
        return {
            "token": config.BROKER_TOKEN,
            "api_url": f"{config.BROKER_URL}/apify",
        }
    return {"token": config.APIFY_TOKEN}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_clients.py -v`
Expected: PASS (5 tests)

- [ ] **Step 6: Wire the three call sites**

In `agents/criteria_parser.py`, add `from integrations import clients` to the imports and change line 100:

```python
    client = Anthropic(**clients.anthropic_kwargs())
```

In `agents/negotiate.py`, add `from integrations import clients` to the imports and change line 134:

```python
    client = Anthropic(**clients.anthropic_kwargs())
```

In `pricing/google_shopping.py`, add `from integrations import clients` to the imports and change line 50:

```python
        _client = ApifyClient(**clients.apify_kwargs())
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 286 tests (281 existing, unchanged, plus 5 new). If any existing test fails, the kwargs indirection has leaked into a patch target; fix that rather than editing the existing test.

- [ ] **Step 8: Commit**

```bash
git add config.py integrations/clients.py agents/criteria_parser.py agents/negotiate.py pricing/google_shopping.py tests/test_clients.py
git commit -m "feat(broker): resolve vendor credentials through a kwargs factory"
```

---

### Task 2: Broker persistence

The two-table store. Spend writes are idempotent on `(vendor, upstream_ref)` because Apify's `.call()` polls and returns the same terminal run object repeatedly.

**Files:**
- Create: `keybroker/__init__.py` (empty)
- Create: `keybroker/db.py`
- Test: `tests/test_keybroker_db.py`

**Interfaces:**
- Consumes: `config.BROKER_DB_PATH`
- Produces: `keybroker.db.init_db()`, `get_conn()`, `month_bounds(month: str | None) -> tuple[str, str]`, `create_friend(name: str, token_sha256: str, monthly_budget_usd: float) -> int`, `get_friend_by_token_hash(token_sha256: str) -> dict | None`, `get_friend_by_name(name: str) -> dict | None`, `revoke_friend(name: str) -> bool`, `list_friends() -> list[dict]`, `record_spend(friend_id: int, vendor: str, cost_usd: float, model_or_actor: str | None = None, input_tokens: int | None = None, output_tokens: int | None = None, cache_read_tokens: int | None = None, cache_creation_tokens: int | None = None, upstream_ref: str | None = None) -> bool`, `friend_month_spend(friend_id: int, month: str | None = None) -> float`, `global_month_spend(month: str | None = None) -> float`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_db.py`:

```python
"""Tests for keybroker.db — friends and spend persistence."""

import pytest

import config
from keybroker import db


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()
    return tmp_path / "broker.db"


def test_create_and_lookup_friend_by_token_hash(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    found = db.get_friend_by_token_hash("hash-alice")
    assert found["id"] == friend_id
    assert found["name"] == "alice"
    assert found["monthly_budget_usd"] == 5.0


def test_unknown_token_hash_returns_none(broker_db):
    db.create_friend("alice", "hash-alice", 5.0)
    assert db.get_friend_by_token_hash("hash-nobody") is None


def test_revoked_friend_is_not_returned_by_token_lookup(broker_db):
    db.create_friend("alice", "hash-alice", 5.0)
    assert db.revoke_friend("alice") is True
    assert db.get_friend_by_token_hash("hash-alice") is None


def test_revoke_preserves_the_row_and_its_spend(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.02, upstream_ref="msg_1")
    db.revoke_friend("alice")
    assert db.get_friend_by_name("alice")["revoked_at"] is not None
    assert db.friend_month_spend(friend_id) == pytest.approx(0.02)


def test_revoking_an_unknown_friend_returns_false(broker_db):
    assert db.revoke_friend("nobody") is False


def test_duplicate_name_is_rejected(broker_db):
    db.create_friend("alice", "hash-a", 5.0)
    with pytest.raises(Exception):
        db.create_friend("alice", "hash-b", 5.0)


def test_spend_accumulates_per_friend(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.02, upstream_ref="msg_1")
    db.record_spend(friend_id, "anthropic", 0.03, upstream_ref="msg_2")
    assert db.friend_month_spend(friend_id) == pytest.approx(0.05)


def test_repeated_upstream_ref_is_recorded_once(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    assert db.record_spend(friend_id, "apify", 0.04, upstream_ref="run_1") is True
    assert db.record_spend(friend_id, "apify", 0.04, upstream_ref="run_1") is False
    assert db.friend_month_spend(friend_id) == pytest.approx(0.04)


def test_same_ref_across_vendors_is_not_deduplicated(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.01, upstream_ref="shared")
    db.record_spend(friend_id, "apify", 0.02, upstream_ref="shared")
    assert db.friend_month_spend(friend_id) == pytest.approx(0.03)


def test_spend_without_upstream_ref_is_never_deduplicated(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    db.record_spend(friend_id, "anthropic", 0.01)
    db.record_spend(friend_id, "anthropic", 0.01)
    assert db.friend_month_spend(friend_id) == pytest.approx(0.02)


def test_global_spend_sums_across_friends(broker_db):
    a = db.create_friend("alice", "hash-a", 5.0)
    b = db.create_friend("bob", "hash-b", 5.0)
    db.record_spend(a, "anthropic", 0.02, upstream_ref="m1")
    db.record_spend(b, "anthropic", 0.03, upstream_ref="m2")
    assert db.global_month_spend() == pytest.approx(0.05)


def test_prior_month_spend_is_excluded(broker_db):
    friend_id = db.create_friend("alice", "hash-alice", 5.0)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO spend (friend_id, vendor, cost_usd, created_at) "
            "VALUES (?, 'anthropic', 1.50, '2026-08-15 12:00:00')",
            (friend_id,),
        )
    assert db.friend_month_spend(friend_id, month="2026-09") == pytest.approx(0.0)
    assert db.friend_month_spend(friend_id, month="2026-08") == pytest.approx(1.50)


def test_month_bounds_wraps_december():
    assert db.month_bounds("2026-12") == ("2026-12-01 00:00:00", "2027-01-01 00:00:00")


def test_list_friends_includes_revoked(broker_db):
    db.create_friend("alice", "hash-a", 5.0)
    db.create_friend("bob", "hash-b", 5.0)
    db.revoke_friend("bob")
    assert {f["name"] for f in db.list_friends()} == {"alice", "bob"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_db.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'keybroker'`

- [ ] **Step 3: Create the package and persistence module**

Create empty `keybroker/__init__.py`, then `keybroker/db.py`:

```python
"""Broker persistence: friends and spend.

Deliberately tiny. The broker stores no domain data — no searches, listings,
negotiations, or eBay credentials. Two tables is the whole model.

Conventions mirror db/schema.sql: integer PKs, ISO-8601 UTC text timestamps
written by SQLite's datetime('now'), cost_usd REAL, cascading FKs.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import config


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS friends (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    name               TEXT NOT NULL UNIQUE,
    token_sha256       TEXT NOT NULL UNIQUE,
    monthly_budget_usd REAL NOT NULL DEFAULT 5.0,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at         TEXT
);

CREATE TABLE IF NOT EXISTS spend (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    friend_id             INTEGER NOT NULL REFERENCES friends(id) ON DELETE CASCADE,
    vendor                TEXT NOT NULL,
    model_or_actor        TEXT,
    cost_usd              REAL NOT NULL,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cache_read_tokens     INTEGER,
    cache_creation_tokens INTEGER,
    upstream_ref          TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_spend_friend_time ON spend(friend_id, created_at);

-- Apify's .call() polls, so the same terminal run object arrives repeatedly.
-- A partial unique index makes INSERT OR IGNORE the dedupe mechanism, mirroring
-- the idx_messages_pending idiom in db/schema.sql.
CREATE UNIQUE INDEX IF NOT EXISTS idx_spend_upstream
    ON spend(vendor, upstream_ref) WHERE upstream_ref IS NOT NULL;
"""


def init_db() -> None:
    with sqlite3.connect(config.BROKER_DB_PATH) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(config.BROKER_DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        yield conn
    finally:
        conn.close()


def month_bounds(month: str | None = None) -> tuple[str, str]:
    """Half-open [start, end) for a YYYY-MM month, in SQLite's datetime('now')
    format. Defaults to the current UTC month."""
    if month is None:
        now = datetime.now(timezone.utc)
        year, mon = now.year, now.month
    else:
        year, mon = (int(part) for part in month.split("-"))
    next_year, next_mon = (year + 1, 1) if mon == 12 else (year, mon + 1)
    return (
        f"{year:04d}-{mon:02d}-01 00:00:00",
        f"{next_year:04d}-{next_mon:02d}-01 00:00:00",
    )


def create_friend(name: str, token_sha256: str, monthly_budget_usd: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO friends (name, token_sha256, monthly_budget_usd) VALUES (?, ?, ?)",
            (name, token_sha256, monthly_budget_usd),
        )
        return int(cur.lastrowid)


def get_friend_by_token_hash(token_sha256: str) -> dict[str, Any] | None:
    """Active friends only — a revoked token is indistinguishable from unknown."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM friends WHERE token_sha256 = ? AND revoked_at IS NULL",
            (token_sha256,),
        ).fetchone()
        return dict(row) if row else None


def get_friend_by_name(name: str) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM friends WHERE name = ?", (name,)).fetchone()
        return dict(row) if row else None


def revoke_friend(name: str) -> bool:
    """Set revoked_at. Never deletes — spend history outlives the friendship."""
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE friends SET revoked_at = datetime('now') "
            "WHERE name = ? AND revoked_at IS NULL",
            (name,),
        )
        return cur.rowcount > 0


def list_friends() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM friends ORDER BY name").fetchall()
        return [dict(r) for r in rows]


def record_spend(
    friend_id: int,
    vendor: str,
    cost_usd: float,
    model_or_actor: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cache_read_tokens: int | None = None,
    cache_creation_tokens: int | None = None,
    upstream_ref: str | None = None,
) -> bool:
    """Record one charge. Returns False when a row for this (vendor,
    upstream_ref) already existed, which is the normal case for Apify polls."""
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO spend (
                friend_id, vendor, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, upstream_ref
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                friend_id, vendor, model_or_actor, cost_usd,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens, upstream_ref,
            ),
        )
        return cur.rowcount > 0


def friend_month_spend(friend_id: int, month: str | None = None) -> float:
    start, end = month_bounds(month)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
            "WHERE friend_id = ? AND created_at >= ? AND created_at < ?",
            (friend_id, start, end),
        ).fetchone()
        return float(row["total"])


def global_month_spend(month: str | None = None) -> float:
    start, end = month_bounds(month)
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM spend "
            "WHERE created_at >= ? AND created_at < ?",
            (start, end),
        ).fetchone()
        return float(row["total"])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_db.py -v`
Expected: PASS (14 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 300 tests

- [ ] **Step 6: Commit**

```bash
git add keybroker/__init__.py keybroker/db.py tests/test_keybroker_db.py
git commit -m "feat(broker): friends and spend persistence"
```

---

### Task 3: Token authentication

Tokens are high-entropy, so SHA-256 plus an indexed lookup replaces the constant-time compare `api/auth.py` needs for its low-entropy password. The two SDKs send different auth headers, so extraction is per-vendor.

**Files:**
- Create: `keybroker/auth.py`
- Test: `tests/test_keybroker_auth.py`

**Interfaces:**
- Consumes: `keybroker.db.get_friend_by_token_hash`, `keybroker.db.create_friend`
- Produces: `keybroker.auth.TOKEN_PREFIX`, `generate_token() -> str`, `hash_token(token: str) -> str`, `extract_token(headers: Mapping[str, str], vendor: str) -> str | None`, `friend_for_token(token: str | None) -> dict | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_auth.py`:

```python
"""Tests for keybroker.auth — token issuance, hashing, and header extraction."""

import pytest

import config
from keybroker import auth, db


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()


def test_generated_tokens_are_prefixed_and_unique():
    a, b = auth.generate_token(), auth.generate_token()
    assert a.startswith(auth.TOKEN_PREFIX)
    assert a != b
    assert len(a) > 40


def test_hash_is_stable_and_hex():
    h = auth.hash_token("sa_example")
    assert h == auth.hash_token("sa_example")
    assert len(h) == 64
    assert "sa_example" not in h


def test_extract_anthropic_token_from_x_api_key():
    headers = {"x-api-key": "sa_tok", "authorization": "Bearer wrong"}
    assert auth.extract_token(headers, "anthropic") == "sa_tok"


def test_extract_apify_token_from_bearer():
    headers = {"authorization": "Bearer sa_tok", "x-api-key": "wrong"}
    assert auth.extract_token(headers, "apify") == "sa_tok"


def test_bearer_scheme_is_case_insensitive():
    assert auth.extract_token({"authorization": "bearer sa_tok"}, "apify") == "sa_tok"


def test_missing_headers_yield_none():
    assert auth.extract_token({}, "anthropic") is None
    assert auth.extract_token({}, "apify") is None


def test_non_bearer_authorization_yields_none():
    assert auth.extract_token({"authorization": "Basic abc"}, "apify") is None


def test_friend_for_token_resolves_a_live_token(broker_db):
    token = auth.generate_token()
    friend_id = db.create_friend("alice", auth.hash_token(token), 5.0)
    assert auth.friend_for_token(token)["id"] == friend_id


def test_friend_for_token_rejects_unknown_and_empty(broker_db):
    assert auth.friend_for_token("sa_nope") is None
    assert auth.friend_for_token(None) is None
    assert auth.friend_for_token("") is None


def test_friend_for_token_rejects_a_revoked_token(broker_db):
    token = auth.generate_token()
    db.create_friend("alice", auth.hash_token(token), 5.0)
    db.revoke_friend("alice")
    assert auth.friend_for_token(token) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_auth.py -v`
Expected: FAIL — `ImportError: cannot import name 'auth' from 'keybroker'`

- [ ] **Step 3: Write the implementation**

Create `keybroker/auth.py`:

```python
"""Broker token authentication.

Tokens carry 256 bits of entropy, so SHA-256 plus an indexed lookup is both
faster and safer than the constant-time compare api/auth.py needs: hashing
destroys any prefix relationship, so there is no timing oracle to exploit.
Storing only the hash also means a leaked broker.db holds nothing spendable.
"""

import hashlib
import secrets
from typing import Any, Mapping

from keybroker import db


TOKEN_PREFIX = "sa_"


def generate_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def extract_token(headers: Mapping[str, str], vendor: str) -> str | None:
    """Pull the friend's token from whichever header that vendor's SDK uses.

    The Anthropic SDK sends `X-Api-Key`; the Apify SDK sends
    `Authorization: Bearer`. Reading the wrong one would let a caller
    authenticate with a header the real client never sets.
    """
    if vendor == "anthropic":
        return headers.get("x-api-key") or None
    authorization = headers.get("authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return value
    return None


def friend_for_token(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    return db.get_friend_by_token_hash(hash_token(token))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_auth.py -v`
Expected: PASS (10 tests)

- [ ] **Step 5: Commit**

```bash
git add keybroker/auth.py tests/test_keybroker_auth.py
git commit -m "feat(broker): token authentication"
```

---

### Task 4: Quota with headroom reservation

The check reserves the worst-case single call rather than comparing against raw spend, which makes the budget a hard ceiling despite metering happening after the response arrives.

**Files:**
- Create: `keybroker/quota.py`
- Test: `tests/test_keybroker_quota.py`

**Interfaces:**
- Consumes: `keybroker.db.friend_month_spend`, `keybroker.db.global_month_spend`, `config.BROKER_GLOBAL_MONTHLY_BUDGET_USD`
- Produces: `keybroker.quota.MAX_SINGLE_CALL_USD` (0.30), `QuotaExceeded` (exception with `.detail: str`), `remaining_usd(friend: dict) -> float`, `check(friend: dict) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_quota.py`:

```python
"""Tests for keybroker.quota — per-friend and global caps with headroom."""

import pytest

import config
from keybroker import db, quota


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    monkeypatch.setattr(config, "BROKER_GLOBAL_MONTHLY_BUDGET_USD", 25.0)
    db.init_db()


def _friend(name="alice", budget=5.0):
    db.create_friend(name, f"hash-{name}", budget)
    return db.get_friend_by_name(name)


def test_fresh_friend_passes(broker_db):
    quota.check(_friend())  # does not raise


def test_friend_well_under_budget_passes(broker_db):
    friend = _friend()
    db.record_spend(friend["id"], "anthropic", 1.00, upstream_ref="m1")
    quota.check(friend)


def test_friend_within_one_call_of_budget_is_rejected(broker_db):
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 4.85, upstream_ref="m1")
    with pytest.raises(quota.QuotaExceeded) as excinfo:
        quota.check(friend)
    assert "alice" in excinfo.value.detail


def test_just_inside_the_headroom_passes(broker_db):
    """Spec section 8 permits the last call to land at or under the budget, so
    anything with more than MAX_SINGLE_CALL_USD of room must be allowed.
    Margins avoid asserting on exact float equality at the boundary."""
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 5.0 - quota.MAX_SINGLE_CALL_USD - 0.01,
                    upstream_ref="m1")
    quota.check(friend)


def test_just_past_the_headroom_is_refused(broker_db):
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 5.0 - quota.MAX_SINGLE_CALL_USD + 0.01,
                    upstream_ref="m1")
    with pytest.raises(quota.QuotaExceeded):
        quota.check(friend)


def test_global_cap_rejects_a_friend_under_their_own_budget(broker_db, monkeypatch):
    monkeypatch.setattr(config, "BROKER_GLOBAL_MONTHLY_BUDGET_USD", 2.0)
    alice = _friend("alice", budget=5.0)
    bob = _friend("bob", budget=5.0)
    db.record_spend(bob["id"], "anthropic", 1.90, upstream_ref="m1")
    with pytest.raises(quota.QuotaExceeded) as excinfo:
        quota.check(alice)
    assert "global" in excinfo.value.detail.lower()


def test_remaining_reflects_spend(broker_db):
    friend = _friend(budget=5.0)
    db.record_spend(friend["id"], "anthropic", 1.25, upstream_ref="m1")
    assert quota.remaining_usd(friend) == pytest.approx(3.75)


def test_remaining_never_goes_negative(broker_db):
    friend = _friend(budget=1.0)
    db.record_spend(friend["id"], "anthropic", 2.50, upstream_ref="m1")
    assert quota.remaining_usd(friend) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_quota.py -v`
Expected: FAIL — `ImportError: cannot import name 'quota' from 'keybroker'`

- [ ] **Step 3: Write the implementation**

Create `keybroker/quota.py`:

```python
"""Spend caps.

Metering happens after a response arrives, so a naive "spent < budget" check
would let the final call cross the line. Reserving the worst-case single call
closes that: the last permitted call lands at or under the budget, so the owner
sets the true ceiling and this module does the subtraction.

MAX_SINGLE_CALL_USD is only valid while keybroker.clamps holds max_tokens to
4096 and the request body to 256 KB. Change either and recompute this.
"""

from typing import Any

import config
from keybroker import db


MAX_SINGLE_CALL_USD = 0.30


class QuotaExceeded(Exception):
    """Raised when a request would exceed a per-friend or global cap."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def remaining_usd(friend: dict[str, Any]) -> float:
    """Budget left this month, floored at zero. Used to clamp Apify runs."""
    spent = db.friend_month_spend(friend["id"])
    return max(0.0, float(friend["monthly_budget_usd"]) - spent)


def check(friend: dict[str, Any]) -> None:
    """Raise QuotaExceeded when either cap lacks room for one more call."""
    budget = float(friend["monthly_budget_usd"])
    spent = db.friend_month_spend(friend["id"])
    if spent + MAX_SINGLE_CALL_USD > budget:
        raise QuotaExceeded(
            f"{friend['name']} has spent ${spent:.2f} of a ${budget:.2f} "
            f"monthly budget; no headroom for another call."
        )

    global_budget = float(config.BROKER_GLOBAL_MONTHLY_BUDGET_USD)
    global_spent = db.global_month_spend()
    if global_spent + MAX_SINGLE_CALL_USD > global_budget:
        raise QuotaExceeded(
            f"Global monthly cap reached: ${global_spent:.2f} of "
            f"${global_budget:.2f} spent across all friends."
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_quota.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add keybroker/quota.py tests/test_keybroker_quota.py
git commit -m "feat(broker): per-friend and global caps with headroom reservation"
```

---

### Task 5: Request clamps

The only three places the broker inspects a request body. Each exists because the client runs on someone else's machine.

**Files:**
- Create: `keybroker/clamps.py`
- Test: `tests/test_keybroker_clamps.py`

**Interfaces:**
- Consumes: nothing
- Produces: `keybroker.clamps.MAX_TOKENS_CEILING` (4096), `MAX_BODY_BYTES` (262144), `BodyTooLarge`, `StreamingUnsupported`, `ensure_size(body: bytes) -> None`, `ensure_not_streaming(body: bytes) -> None`, `clamp_anthropic(body: bytes) -> bytes`, `is_apify_run_creation(method: str, path: str) -> bool`, `clamp_apify_run(body: bytes, remaining_usd: float) -> bytes`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_clamps.py`:

```python
"""Tests for keybroker.clamps — the three trusted-input rewrites."""

import json

import pytest

from keybroker import clamps


def test_body_within_cap_passes():
    clamps.ensure_size(b"x" * 100)


def test_oversized_body_is_rejected():
    with pytest.raises(clamps.BodyTooLarge):
        clamps.ensure_size(b"x" * (clamps.MAX_BODY_BYTES + 1))


def test_streaming_request_is_rejected():
    with pytest.raises(clamps.StreamingUnsupported):
        clamps.ensure_not_streaming(json.dumps({"stream": True}).encode())


def test_non_streaming_request_passes():
    clamps.ensure_not_streaming(json.dumps({"stream": False}).encode())
    clamps.ensure_not_streaming(json.dumps({}).encode())


def test_unparseable_body_is_not_treated_as_streaming():
    clamps.ensure_not_streaming(b"not json")


def test_max_tokens_above_ceiling_is_rewritten_down():
    body = json.dumps({"model": "m", "max_tokens": 64000}).encode()
    assert json.loads(clamps.clamp_anthropic(body))["max_tokens"] == clamps.MAX_TOKENS_CEILING


def test_max_tokens_below_ceiling_is_untouched():
    body = json.dumps({"model": "m", "max_tokens": 1024}).encode()
    assert clamps.clamp_anthropic(body) == body


def test_clamping_preserves_other_fields():
    body = json.dumps({"model": "m", "max_tokens": 99999, "tools": [{"name": "t"}]}).encode()
    out = json.loads(clamps.clamp_anthropic(body))
    assert out["tools"] == [{"name": "t"}]
    assert out["model"] == "m"


def test_unparseable_anthropic_body_passes_through_unchanged():
    assert clamps.clamp_anthropic(b"not json") == b"not json"


def test_apify_run_creation_is_detected():
    assert clamps.is_apify_run_creation("POST", "/v2/acts/abc~actor/runs") is True
    assert clamps.is_apify_run_creation("GET", "/v2/acts/abc~actor/runs") is False
    assert clamps.is_apify_run_creation("POST", "/v2/actor-runs/xyz") is False


def test_apify_charge_above_remaining_is_clamped_down():
    body = json.dumps({"maxTotalChargeUsd": 5.00}).encode()
    out = json.loads(clamps.clamp_apify_run(body, remaining_usd=0.40))
    assert out["maxTotalChargeUsd"] == pytest.approx(0.40)


def test_apify_charge_below_remaining_is_untouched():
    body = json.dumps({"maxTotalChargeUsd": 0.10}).encode()
    out = json.loads(clamps.clamp_apify_run(body, remaining_usd=5.00))
    assert out["maxTotalChargeUsd"] == pytest.approx(0.10)


def test_apify_body_without_a_charge_field_gets_one():
    out = json.loads(clamps.clamp_apify_run(json.dumps({}).encode(), remaining_usd=0.40))
    assert out["maxTotalChargeUsd"] == pytest.approx(0.40)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_clamps.py -v`
Expected: FAIL — `ImportError: cannot import name 'clamps' from 'keybroker'`

- [ ] **Step 3: Write the implementation**

Create `keybroker/clamps.py`:

```python
"""Request rewrites for untrusted clients.

The broker forwards bodies verbatim except here. Each clamp exists because the
client runs on someone else's machine, so its values are advisory at best.
Together, the max_tokens ceiling and the body cap are what make
quota.MAX_SINGLE_CALL_USD a valid reservation — change them and recompute it.
"""

import json
import logging
from typing import Any


log = logging.getLogger(__name__)

MAX_TOKENS_CEILING = 4096
MAX_BODY_BYTES = 256 * 1024


class BodyTooLarge(Exception):
    """Request body exceeds MAX_BODY_BYTES."""


class StreamingUnsupported(Exception):
    """Streaming would under-meter silently, so it is refused outright."""


def _load(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def ensure_size(body: bytes) -> None:
    if len(body) > MAX_BODY_BYTES:
        raise BodyTooLarge(f"{len(body)} bytes exceeds the {MAX_BODY_BYTES} byte cap")


def ensure_not_streaming(body: bytes) -> None:
    payload = _load(body)
    if payload is not None and payload.get("stream") is True:
        raise StreamingUnsupported("streaming responses are not supported by the broker")


def clamp_anthropic(body: bytes) -> bytes:
    """Rewrite max_tokens down to the ceiling. Unparseable bodies pass through —
    upstream is a better judge of malformed JSON than we are."""
    payload = _load(body)
    if payload is None:
        return body
    if payload.get("max_tokens", 0) > MAX_TOKENS_CEILING:
        log.info("clamping max_tokens %s -> %s", payload["max_tokens"], MAX_TOKENS_CEILING)
        payload["max_tokens"] = MAX_TOKENS_CEILING
        return json.dumps(payload).encode("utf-8")
    return body


def is_apify_run_creation(method: str, path: str) -> bool:
    """POST /v2/acts/{actorId}/runs — the only Apify call that starts spending."""
    return method.upper() == "POST" and path.rstrip("/").endswith("/runs")


def clamp_apify_run(body: bytes, remaining_usd: float) -> bytes:
    """Cap the run's own spend ceiling at the friend's remaining budget.

    Apify is the only vendor exposing a pre-spend limit, so this is the one
    place the broker prevents spend rather than measuring it afterwards.
    """
    payload = _load(body) or {}
    requested = payload.get("maxTotalChargeUsd")
    if requested is None or float(requested) > remaining_usd:
        payload["maxTotalChargeUsd"] = round(remaining_usd, 4)
    return json.dumps(payload).encode("utf-8")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_clamps.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add keybroker/clamps.py tests/test_keybroker_clamps.py
git commit -m "feat(broker): max_tokens, body size, and Apify charge clamps"
```

---

### Task 6: Response metering

Turns vendor responses into `spend` rows. Anthropic prices through the existing `config.price_usage()`; Apify reads `usageTotalUsd` off terminal run objects.

**Files:**
- Create: `keybroker/meter.py`
- Test: `tests/test_keybroker_meter.py`

**Interfaces:**
- Consumes: `config.price_usage`, `keybroker.db.record_spend`
- Produces: `keybroker.meter.record_anthropic(friend_id: int, body: bytes) -> None`, `record_apify(friend_id: int, body: bytes) -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_meter.py`:

```python
"""Tests for keybroker.meter — response bodies to spend rows."""

import json

import pytest

import config
from keybroker import db, meter


@pytest.fixture
def friend(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()
    return db.create_friend("alice", "hash-alice", 5.0)


def _anthropic_body(input_tokens=1000, output_tokens=500):
    return json.dumps({
        "id": "msg_01",
        "model": config.NEGOTIATOR_MODEL,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }).encode()


def _apify_body(status="SUCCEEDED", usage=0.04, run_id="run_1"):
    return json.dumps({
        "data": {"id": run_id, "actId": "actor~x", "status": status,
                 "usageTotalUsd": usage}
    }).encode()


def test_anthropic_spend_is_priced_through_config(friend):
    meter.record_anthropic(friend, _anthropic_body())
    expected = config.price_usage(config.NEGOTIATOR_MODEL, 1000, 500)
    assert db.friend_month_spend(friend) == pytest.approx(expected)


def test_anthropic_token_counts_are_recorded(friend):
    meter.record_anthropic(friend, _anthropic_body())
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM spend").fetchone()
    assert row["input_tokens"] == 1000
    assert row["output_tokens"] == 500
    assert row["cache_read_tokens"] == 0
    assert row["upstream_ref"] == "msg_01"
    assert row["vendor"] == "anthropic"


def test_anthropic_response_without_usage_is_ignored(friend):
    meter.record_anthropic(friend, json.dumps({"id": "msg_02"}).encode())
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_unparseable_body_does_not_raise(friend):
    meter.record_anthropic(friend, b"not json")
    meter.record_apify(friend, b"not json")
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_apify_terminal_run_is_recorded(friend):
    meter.record_apify(friend, _apify_body())
    assert db.friend_month_spend(friend) == pytest.approx(0.04)


def test_apify_running_status_is_not_recorded(friend):
    meter.record_apify(friend, _apify_body(status="RUNNING"))
    assert db.friend_month_spend(friend) == pytest.approx(0.0)


def test_repeated_apify_polls_record_once(friend):
    for _ in range(4):
        meter.record_apify(friend, _apify_body())
    assert db.friend_month_spend(friend) == pytest.approx(0.04)


def test_failed_runs_still_cost_money_and_are_recorded(friend):
    meter.record_apify(friend, _apify_body(status="FAILED", usage=0.01))
    assert db.friend_month_spend(friend) == pytest.approx(0.01)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_meter.py -v`
Expected: FAIL — `ImportError: cannot import name 'meter' from 'keybroker'`

- [ ] **Step 3: Write the implementation**

Create `keybroker/meter.py`:

```python
"""Response-driven spend metering.

The broker is the authoritative ledger — it sees SDK retries the client never
attributes to a search. Metering never raises: the money is already spent
upstream, so a bookkeeping failure must not also cost the friend their result.
"""

import json
import logging
from typing import Any

import config
from keybroker import db


log = logging.getLogger(__name__)

# A run that reached any of these has finished charging.
TERMINAL_RUN_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


def _load(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def record_anthropic(friend_id: int, body: bytes) -> None:
    payload = _load(body)
    if payload is None:
        return
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return  # error responses carry no usage

    model = payload.get("model", "")
    input_tokens = int(usage.get("input_tokens", 0))
    output_tokens = int(usage.get("output_tokens", 0))
    try:
        db.record_spend(
            friend_id=friend_id,
            vendor="anthropic",
            model_or_actor=model,
            cost_usd=config.price_usage(model, input_tokens, output_tokens),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_creation_tokens=usage.get("cache_creation_input_tokens"),
            upstream_ref=payload.get("id"),
        )
    except Exception:
        log.exception("failed to record anthropic spend for friend %s", friend_id)


def record_apify(friend_id: int, body: bytes) -> None:
    """Record when a response carries a terminal run with a usage figure.

    Matching on shape rather than route means this covers both the run-creation
    response and every poll, without the broker having to parse URLs.
    """
    payload = _load(body)
    if payload is None:
        return
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    if data.get("status") not in TERMINAL_RUN_STATUSES:
        return
    usage = data.get("usageTotalUsd")
    if usage is None:
        return

    try:
        db.record_spend(
            friend_id=friend_id,
            vendor="apify",
            model_or_actor=data.get("actId"),
            cost_usd=float(usage),
            upstream_ref=data.get("id"),
        )
    except Exception:
        log.exception("failed to record apify spend for friend %s", friend_id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_meter.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add keybroker/meter.py tests/test_keybroker_meter.py
git commit -m "feat(broker): response metering for Anthropic and Apify"
```

---

### Task 7: The proxy

Wires everything together. This is the only task that touches the network, and the only one where ordering matters: fail-closed, then auth, then clamps, then quota, then forward, then meter.

**Files:**
- Create: `keybroker/app.py`
- Create: `keybroker/__main__.py`
- Test: `tests/test_keybroker_proxy.py`

**Interfaces:**
- Consumes: `keybroker.auth`, `keybroker.clamps`, `keybroker.db`, `keybroker.meter`, `keybroker.quota`, `config.ANTHROPIC_API_KEY`, `config.APIFY_TOKEN`, `config.BROKER_PORT`
- Produces: `keybroker.app.app` (FastAPI instance), `keybroker.app.UPSTREAMS`

- [ ] **Step 1: Write the failing test**

Create `tests/test_keybroker_proxy.py`:

```python
"""Tests for keybroker.app — the proxy end to end, with upstreams stubbed."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

import config
from keybroker import app as app_module
from keybroker import auth, db, quota


@pytest.fixture
def broker(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-owner-key")
    monkeypatch.setattr(config, "APIFY_TOKEN", "apify-owner-token")
    monkeypatch.setattr(config, "BROKER_GLOBAL_MONTHLY_BUDGET_USD", 25.0)
    with TestClient(app_module.app) as client:
        token = auth.generate_token()
        db.create_friend("alice", auth.hash_token(token), 5.0)
        yield client, token


class _FakeUpstream:
    """Stands in for the module's httpx.AsyncClient and records the call."""

    def __init__(self, response: httpx.Response):
        self.response = response
        self.calls: list[dict] = []

    async def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


def _install(monkeypatch, response):
    fake = _FakeUpstream(response)
    monkeypatch.setattr(app_module, "_client", fake)
    return fake


def _anthropic_response(input_tokens=100, output_tokens=50):
    return httpx.Response(200, json={
        "id": "msg_01", "model": config.NEGOTIATOR_MODEL,
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })


def test_health_needs_no_token(broker):
    client, _ = broker
    assert client.get("/health").status_code == 200


def test_missing_token_is_401(broker):
    client, _ = broker
    assert client.post("/anthropic/v1/messages", json={}).status_code == 401


def test_unknown_token_is_401(broker):
    client, _ = broker
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": "sa_nope"})
    assert r.status_code == 401


def test_revoked_token_is_401(broker, monkeypatch):
    client, token = broker
    db.revoke_friend("alice")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 401


def test_valid_request_is_forwarded_with_the_owner_key(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"model": "m", "max_tokens": 1024},
                    headers={"x-api-key": token, "anthropic-version": "2023-06-01"})
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    assert call["headers"]["x-api-key"] == "sk-owner-key"
    assert call["headers"]["anthropic-version"] == "2023-06-01"


def test_friend_token_never_reaches_upstream(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert token not in json.dumps(dict(fake.calls[0]["headers"]))


def test_apify_uses_bearer_and_its_own_upstream(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"items": []}}))
    r = client.get("/apify/v2/datasets/ds1/items",
                   headers={"authorization": f"Bearer {token}"})
    assert r.status_code == 200
    call = fake.calls[0]
    assert call["url"] == "https://api.apify.com/v2/datasets/ds1/items"
    assert call["headers"]["authorization"] == "Bearer apify-owner-token"


def test_successful_call_is_metered(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response(input_tokens=1000, output_tokens=500))
    client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    friend = db.get_friend_by_name("alice")
    expected = config.price_usage(config.NEGOTIATOR_MODEL, 1000, 500)
    assert db.friend_month_spend(friend["id"]) == pytest.approx(expected)


def test_upstream_error_is_forwarded_and_not_metered(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, httpx.Response(400, json={"error": "bad"}))
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 400
    friend = db.get_friend_by_name("alice")
    assert db.friend_month_spend(friend["id"]) == pytest.approx(0.0)


def test_upstream_connection_failure_is_502(broker, monkeypatch):
    client, token = broker

    class _Broken:
        async def request(self, *a, **k):
            raise httpx.ConnectError("boom")

    monkeypatch.setattr(app_module, "_client", _Broken())
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 502


def test_exhausted_budget_returns_402_not_429(broker, monkeypatch):
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.90, upstream_ref="m1")
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 402


def test_streaming_request_is_400(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"stream": True}, headers={"x-api-key": token})
    assert r.status_code == 400


def test_oversized_body_is_413(broker, monkeypatch):
    client, token = broker
    _install(monkeypatch, _anthropic_response())
    r = client.post("/anthropic/v1/messages",
                    json={"pad": "x" * 300_000}, headers={"x-api-key": token})
    assert r.status_code == 413


def test_max_tokens_is_clamped_before_forwarding(broker, monkeypatch):
    client, token = broker
    fake = _install(monkeypatch, _anthropic_response())
    client.post("/anthropic/v1/messages",
                json={"model": "m", "max_tokens": 64000},
                headers={"x-api-key": token})
    sent = json.loads(fake.calls[0]["content"])
    assert sent["max_tokens"] == 4096


def test_apify_run_charge_is_clamped_to_remaining_budget(broker, monkeypatch):
    client, token = broker
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 4.60, upstream_ref="m1")
    fake = _install(monkeypatch, httpx.Response(200, json={"data": {"id": "r1"}}))
    client.post("/apify/v2/acts/actor~x/runs", json={"maxTotalChargeUsd": 5.0},
                headers={"authorization": f"Bearer {token}"})
    sent = json.loads(fake.calls[0]["content"])
    assert sent["maxTotalChargeUsd"] == pytest.approx(0.40)


def test_unset_owner_key_fails_closed_with_503(broker, monkeypatch):
    client, token = broker
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    r = client.post("/anthropic/v1/messages", json={}, headers={"x-api-key": token})
    assert r.status_code == 503
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_proxy.py -v`
Expected: FAIL — `ImportError: cannot import name 'app' from 'keybroker'`

- [ ] **Step 3: Write the proxy**

Create `keybroker/app.py`:

```python
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
```

Create `keybroker/__main__.py`:

```python
"""Run the broker: python -m keybroker

Binds loopback only. Tailscale Funnel runs on this machine and forwards to
127.0.0.1, so there is never a reason to listen on all interfaces.
"""

import uvicorn

import config


if __name__ == "__main__":
    uvicorn.run(
        "keybroker.app:app",
        host="127.0.0.1",
        port=config.BROKER_PORT,
        log_level="info",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_keybroker_proxy.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 355 tests (281 original + 74 added across Tasks 1-7)

- [ ] **Step 6: Commit**

```bash
git add keybroker/app.py keybroker/__main__.py tests/test_keybroker_proxy.py
git commit -m "feat(broker): proxy routing, key injection, and error mapping"
```

---

### Task 8: Operator scripts and documentation

The owner's whole control surface: issue a token, revoke it, see what it cost. Follows the `scripts/backup_profile.py` conventions — `argparse`, `main() -> int`, non-zero exit on failure.

**Files:**
- Create: `scripts/add_friend.py`
- Create: `scripts/revoke_friend.py`
- Create: `scripts/spend_report.py`
- Modify: `README.md` (add a "Key broker" section after "Deployment")
- Modify: `.env.example`
- Test: `tests/test_friend_scripts.py`

**Interfaces:**
- Consumes: `keybroker.auth.generate_token`, `keybroker.auth.hash_token`, `keybroker.db`
- Produces: `scripts.add_friend.main(argv: list[str] | None = None) -> int`, `scripts.revoke_friend.main(...) -> int`, `scripts.spend_report.main(...) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_friend_scripts.py`:

```python
"""Tests for the broker operator scripts."""

import pytest

import config
from keybroker import auth, db
from scripts import add_friend, revoke_friend, spend_report


@pytest.fixture
def broker_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "BROKER_DB_PATH", tmp_path / "broker.db")
    db.init_db()


def test_add_friend_creates_a_friend_and_prints_the_token(broker_db, capsys):
    assert add_friend.main(["alice", "--budget", "7.5"]) == 0
    printed = capsys.readouterr().out
    friend = db.get_friend_by_name("alice")
    assert friend["monthly_budget_usd"] == 7.5

    token = next(w for w in printed.split() if w.startswith(auth.TOKEN_PREFIX))
    assert auth.friend_for_token(token)["id"] == friend["id"]


def test_the_raw_token_is_not_stored(broker_db, capsys):
    add_friend.main(["alice"])
    printed = capsys.readouterr().out
    token = next(w for w in printed.split() if w.startswith(auth.TOKEN_PREFIX))
    assert db.get_friend_by_name("alice")["token_sha256"] != token


def test_adding_a_duplicate_name_fails_without_a_traceback(broker_db, capsys):
    add_friend.main(["alice"])
    assert add_friend.main(["alice"]) == 1


def test_revoke_friend_revokes(broker_db, capsys):
    add_friend.main(["alice"])
    assert revoke_friend.main(["alice"]) == 0
    assert db.get_friend_by_name("alice")["revoked_at"] is not None


def test_revoking_an_unknown_friend_exits_nonzero(broker_db):
    assert revoke_friend.main(["nobody"]) == 1


def test_spend_report_lists_each_friend_with_totals(broker_db, capsys):
    add_friend.main(["alice", "--budget", "5"])
    add_friend.main(["bob", "--budget", "5"])
    friend = db.get_friend_by_name("alice")
    db.record_spend(friend["id"], "anthropic", 1.25, upstream_ref="m1")
    capsys.readouterr()

    assert spend_report.main([]) == 0
    out = capsys.readouterr().out
    assert "alice" in out and "bob" in out
    assert "1.25" in out


def test_spend_report_honours_an_explicit_month(broker_db, capsys):
    add_friend.main(["alice"])
    friend = db.get_friend_by_name("alice")
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO spend (friend_id, vendor, cost_usd, created_at) "
            "VALUES (?, 'anthropic', 3.00, '2026-08-10 09:00:00')",
            (friend["id"],),
        )
    capsys.readouterr()
    spend_report.main(["--month", "2026-08"])
    assert "3.00" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_friend_scripts.py -v`
Expected: FAIL — `ImportError: cannot import name 'add_friend' from 'scripts'`

- [ ] **Step 3: Write `scripts/add_friend.py`**

```python
"""Issue a broker token to a friend.

The token is shown once and never stored in recoverable form — only its SHA-256
lands in broker.db, so a leaked database holds nothing spendable. Lose the
token and the fix is to revoke and reissue.

Usage:
    python -m scripts.add_friend alice
    python -m scripts.add_friend alice --budget 7.50
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from keybroker import auth, db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Issue a broker token")
    parser.add_argument("name", help="short handle for the friend, e.g. alice")
    parser.add_argument("--budget", type=float, default=5.0,
                        help="monthly cap in USD (default: 5.00)")
    args = parser.parse_args(argv)

    db.init_db()
    token = auth.generate_token()
    try:
        db.create_friend(args.name, auth.hash_token(token), args.budget)
    except sqlite3.IntegrityError:
        print(f"A friend named {args.name!r} already exists.", file=sys.stderr)
        return 1

    print(f"Friend:  {args.name}")
    print(f"Budget:  ${args.budget:.2f}/month")
    print(f"Token:   {token}")
    print()
    print("Shown once. Have them add to their .env:")
    print(f"  SCRAPERAGENT_BROKER_TOKEN={token}")
    print("  SCRAPERAGENT_BROKER_URL=<your broker URL>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Write `scripts/revoke_friend.py`**

```python
"""Revoke a friend's broker token.

Sets revoked_at rather than deleting, so their spend history survives for your
own accounting. Revocation takes effect on the next request.

Usage:
    python -m scripts.revoke_friend alice
"""

from __future__ import annotations

import argparse
import sys

from keybroker import db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Revoke a broker token")
    parser.add_argument("name")
    args = parser.parse_args(argv)

    db.init_db()
    if not db.revoke_friend(args.name):
        print(f"No active friend named {args.name!r}.", file=sys.stderr)
        return 1
    print(f"Revoked {args.name}. Their spend history is retained.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: Write `scripts/spend_report.py`**

```python
"""Month-to-date broker spend, per friend.

Usage:
    python -m scripts.spend_report
    python -m scripts.spend_report --month 2026-08
"""

from __future__ import annotations

import argparse
import sys

import config
from keybroker import db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Broker spend report")
    parser.add_argument("--month", help="YYYY-MM (default: current UTC month)")
    args = parser.parse_args(argv)

    db.init_db()
    friends = db.list_friends()
    if not friends:
        print("No friends yet. Issue a token with: python -m scripts.add_friend <name>")
        return 0

    start, _ = db.month_bounds(args.month)
    print(f"Spend for {start[:7]}")
    print(f"{'friend':<16}{'spent':>10}{'budget':>10}  status")
    for friend in friends:
        spent = db.friend_month_spend(friend["id"], args.month)
        status = "revoked" if friend["revoked_at"] else "active"
        print(f"{friend['name']:<16}{spent:>10.2f}{friend['monthly_budget_usd']:>10.2f}  {status}")

    total = db.global_month_spend(args.month)
    cap = float(config.BROKER_GLOBAL_MONTHLY_BUDGET_USD)
    print(f"{'TOTAL':<16}{total:>10.2f}{cap:>10.2f}  global cap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_friend_scripts.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Document it**

Add to `.env.example`:

```
# ---- Key broker (friends only) ----
# Set both to route Anthropic and Apify through someone else's broker.
# Leave both empty to use your own ANTHROPIC_API_KEY / APIFY_TOKEN.
SCRAPERAGENT_BROKER_URL=
SCRAPERAGENT_BROKER_TOKEN=

# ---- Key broker (broker host only) ----
BROKER_GLOBAL_MONTHLY_BUDGET_USD=25.00
BROKER_PORT=8001
```

Add a "Key broker" section to `README.md` after "Deployment" covering: what it is (friends run the full app locally and reach vendors through your keys), running it (`python -m keybroker`, then `tailscale funnel 8001`), issuing and revoking tokens, reading the spend report, and the two caps. State plainly that friends' prompts transit your machine in memory, and that a friend can bypass the broker at any time by setting their own vendor keys.

- [ ] **Step 8: Run the full suite**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: PASS — 362 tests (281 original + 81 added)

- [ ] **Step 9: Commit**

```bash
git add scripts/add_friend.py scripts/revoke_friend.py scripts/spend_report.py tests/test_friend_scripts.py README.md .env.example
git commit -m "feat(broker): operator scripts and documentation"
```

---

## Verification

After Task 8, confirm the spec's success criteria hold:

- [ ] `.venv/Scripts/python.exe -m pytest -q` — 362 passing, including the original 281 unchanged.
- [ ] With `SCRAPERAGENT_BROKER_URL` empty, `python -c "from integrations import clients; print(clients.anthropic_kwargs())"` shows the real key and no `base_url`.
- [ ] `python -m scripts.add_friend testuser --budget 1` prints a token; `python -m scripts.spend_report` lists them at $0.00.
- [ ] `python -m keybroker` starts and `curl http://127.0.0.1:8001/health` returns `{"status":"ok"}`.
- [ ] `python -m scripts.revoke_friend testuser` exits 0; the report shows them as revoked.

## Notes for the executor

- **Phase 0 is not in this plan.** Until the eBay API removal lands, a friend still needs `EBAY_USER_TOKEN` and cannot actually run the app. Everything here is independently testable regardless.
- **Do not weaken the clamps in Task 5 without recomputing `quota.MAX_SINGLE_CALL_USD`.** The 0.30 reservation is derived from `MAX_TOKENS_CEILING` and `MAX_BODY_BYTES`; they are a unit.
- **Never change 402 to 429** in Task 7. Both SDKs auto-retry 429.
- **If an existing test breaks in Task 1**, the fix is in the production code, not the test. The whole point of returning kwargs is that the ~20 existing SDK-class patches keep working.
