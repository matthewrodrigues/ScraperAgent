"""Thin SQLite wrapper.

Deliberately thin: no ORM, no migrations framework. The schema is small and
the access patterns are simple. When that stops being true, swap to SQLAlchemy.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import config


SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def init_db() -> None:
    """Create tables if they don't exist. Safe to call on every boot."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(config.DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        _apply_column_migrations(conn)


# Lightweight "add a column if it doesn't already exist" pattern. Avoids pulling
# in a migration framework while still letting us evolve the schema without
# asking the user to nuke their local db. Keep entries here ordered oldest-first.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("searches", "error_message", "TEXT"),
    # Best Offer plumbing — added when PlaceOffer became the primary send path
    # for BO-enabled listings. `buying_options` is the comma-joined Browse API
    # value (e.g. "FIXED_PRICE,BEST_OFFER"); we store it as TEXT rather than
    # JSON because the only query is a substring check for "BEST_OFFER".
    ("listings", "buying_options", "TEXT"),
    # eBay's PlaceOffer returns a BestOfferID we need later for GetBestOffers
    # polling. Nullable: AAQ-only negotiations never populate it.
    ("negotiations", "ebay_best_offer_id", "TEXT"),
    # Per-message Anthropic usage + cost. Populated by the negotiate graph
    # when an agent message is drafted; legacy rows + seller messages stay NULL.
    # `cost_usd` is computed at write time via `config.price_usage` — storing
    # the materialized cost (not just tokens) keeps the dashboard sum cheap
    # and means a future pricing change doesn't retroactively rewrite history.
    ("messages", "input_tokens", "INTEGER"),
    ("messages", "output_tokens", "INTEGER"),
    ("messages", "cost_usd", "REAL"),
    # Discovery moved from the free Browse API to a paid Apify actor, so a
    # search now has a cost of its own that is not a reference price.
    ("searches", "search_cost_usd", "REAL NOT NULL DEFAULT 0"),
    # Non-fatal degradation notice shown as a banner. Distinct from
    # error_message, which means the search failed.
    ("searches", "warning_message", "TEXT"),
]


def _apply_column_migrations(conn: sqlite3.Connection) -> None:
    for table, col, coltype in _COLUMN_MIGRATIONS:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")


@contextmanager
def get_conn() -> Iterator[sqlite3.Connection]:
    """Connection context with row factory + foreign keys on."""
    conn = sqlite3.connect(config.DB_PATH, isolation_level=None)  # autocommit; we manage txns explicitly when needed
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ---- Search CRUD ---------------------------------------------------------

def create_search(criteria_nl: str, criteria_structured: dict[str, Any], max_price: float) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO searches (criteria_nl, criteria_structured_json, max_price) VALUES (?, ?, ?)",
            (criteria_nl, json.dumps(criteria_structured), max_price),
        )
        return cur.lastrowid


def get_search(search_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM searches WHERE id = ?", (search_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["criteria_structured"] = json.loads(d.pop("criteria_structured_json"))
        return d


def update_search_status(search_id: int, status: str, thread_id: str | None = None) -> None:
    with get_conn() as conn:
        if thread_id is not None:
            conn.execute(
                "UPDATE searches SET status = ?, thread_id = ? WHERE id = ?",
                (status, thread_id, search_id),
            )
        else:
            conn.execute("UPDATE searches SET status = ? WHERE id = ?", (status, search_id))


def list_recent_searches(limit: int = 20) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, criteria_nl, max_price, status, created_at FROM searches "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_search_error(search_id: int, error_message: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE searches SET error_message = ? WHERE id = ?",
            (error_message, search_id),
        )


def set_search_cost(search_id: int, cost_usd: float) -> None:
    """Record what discovery cost for this search."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE searches SET search_cost_usd = ? WHERE id = ?",
            (round(cost_usd, 6), search_id),
        )


def set_search_warning(search_id: int, message: str) -> None:
    """Attach a non-fatal notice to a search. Rendered as a banner; unlike
    error_message it does not mean the search failed."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE searches SET warning_message = ? WHERE id = ?",
            (message, search_id),
        )


# ---- Listing CRUD --------------------------------------------------------

def add_listings(search_id: int, listings: list[dict[str, Any]]) -> list[int]:
    """Insert all listings for a search atomically. Returns new row ids in order.

    `listings` are plain dicts matching the listings-table columns. We accept
    dicts (not the Pydantic Listing model) to keep this layer free of dependencies
    on browser.ebay — the caller does the model_dump.
    """
    if not listings:
        return []
    ids: list[int] = []
    with get_conn() as conn:
        # Explicit transaction since the connection is in autocommit mode.
        conn.execute("BEGIN")
        try:
            for l in listings:
                # buying_options arrives as a list from Browse API; flatten to
                # a comma-joined string for the TEXT column. Empty list / None
                # both map to NULL — only present-and-nonempty stores anything.
                bo = l.get("buying_options")
                buying_options_str = ",".join(bo) if bo else None
                cur = conn.execute(
                    """
                    INSERT INTO listings (
                        search_id, ebay_item_id, title, price, shipping_cost,
                        condition, seller_id, seller_rating, seller_feedback_count,
                        url, image_url, listed_at, raw_data_json, buying_options
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        search_id,
                        l["ebay_item_id"],
                        l["title"],
                        l["price"],
                        l.get("shipping_cost"),
                        l.get("condition"),
                        l.get("seller_id"),
                        l.get("seller_rating"),
                        l.get("seller_feedback_count"),
                        l["url"],
                        l.get("image_url"),
                        l.get("listed_at"),
                        json.dumps(l.get("raw_data") or {}),
                        buying_options_str,
                    ),
                )
                ids.append(cur.lastrowid)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return ids


def list_listings(search_id: int) -> list[dict[str, Any]]:
    """All listings for a search, cheapest first (matches search-time sort)."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM listings WHERE search_id = ? ORDER BY price ASC, id ASC",
            (search_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_listing(listing_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM listings WHERE id = ?", (listing_id,)).fetchone()
        return dict(row) if row else None


def mark_listing_selected(listing_id: int) -> None:
    """Set selected_at = now(). Idempotent — running twice keeps the first timestamp."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE listings SET selected_at = COALESCE(selected_at, datetime('now')) WHERE id = ?",
            (listing_id,),
        )


def unmark_listing_selected(listing_id: int) -> None:
    """Clear selected_at. Idempotent — clearing an already-NULL row is a no-op."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE listings SET selected_at = NULL WHERE id = ?",
            (listing_id,),
        )


def get_selected_listing(search_id: int) -> dict[str, Any] | None:
    """The single listing already marked for negotiation, if any. Used to enforce
    one-selection-per-search and to render the post-selection state."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM listings WHERE search_id = ? AND selected_at IS NOT NULL "
            "ORDER BY selected_at ASC LIMIT 1",
            (search_id,),
        ).fetchone()
        return dict(row) if row else None


# ---- Reference price CRUD -----------------------------------------------

def add_reference_price(
    search_id: int,
    source: str,
    condition: str | None,
    median: float | None,
    p25: float | None,
    p75: float | None,
    raw_data: Any,
    cost_usd: float,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO reference_prices (
                search_id, source, raw_data_json, median, p25, p75, condition, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (search_id, source, json.dumps(raw_data), median, p25, p75, condition, cost_usd),
        )
        return cur.lastrowid


def list_reference_prices(search_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM reference_prices WHERE search_id = ? ORDER BY id ASC",
            (search_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            d["raw_data"] = json.loads(d.pop("raw_data_json") or "null")
            out.append(d)
        return out


def sum_search_cost(search_id: int) -> float:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(search_cost_usd, 0.0) AS c FROM searches WHERE id = ?",
            (search_id,),
        ).fetchone()
        return float(row["c"]) if row else 0.0


def sum_apify_cost(search_id: int) -> float:
    """Sum of cost_usd across all reference_prices rows for a search.
    Used by the cost guard to enforce APIFY_BUDGET_USD."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS total FROM reference_prices WHERE search_id = ?",
            (search_id,),
        ).fetchone()
        return float(row["total"])


def sum_claude_cost(search_id: int) -> float:
    """Sum of cost_usd across all agent messages drafted for a search.

    Joins through the schema chain (messages → negotiations → listings →
    searches) because messages have no direct search_id. NULL costs are
    excluded — agent messages without usage data (legacy rows, or any seller
    messages that ever inadvertently carry a cost) don't contribute."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(m.cost_usd), 0.0) AS total
              FROM messages m
              JOIN negotiations n ON n.id = m.negotiation_id
              JOIN listings l ON l.id = n.listing_id
             WHERE l.search_id = ? AND m.cost_usd IS NOT NULL
            """,
            (search_id,),
        ).fetchone()
        return float(row["total"])


def sum_total_cost(search_id: int) -> dict[str, float]:
    """Cost breakdown for a search: discovery + Apify pricing + Claude drafting.

    Returns `{"search": S, "apify": A, "claude": C, "total": S+A+C}` rounded to
    4 decimals. Single source of truth for the dashboard cost line."""
    search = round(sum_search_cost(search_id), 4)
    apify = round(sum_apify_cost(search_id), 4)
    claude = round(sum_claude_cost(search_id), 4)
    return {
        "search": search,
        "apify": apify,
        "claude": claude,
        "total": round(search + apify + claude, 4),
    }


def get_aggregated_ref_median(search_id: int) -> float | None:
    """Single representative ref_median for a search, used to rank listings
    by market-relative gap.

    Strategy today: first non-None median across all reference_prices rows
    (matches what `strategies/__init__.py` already does to pick a single
    number out of multiple sources). Returns None when no median is available
    — the route layer then falls back to raw price-asc ordering.

    TODO: weight by source quality (eBay sold > Amazon > Walmart > Target >
    Best Buy > Google Shopping) once additional Apify actors land. Today
    only `google_shopping` populates ref_prices so weighting is academic."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT median FROM reference_prices
             WHERE search_id = ? AND median IS NOT NULL
             ORDER BY id ASC LIMIT 1
            """,
            (search_id,),
        ).fetchone()
        if row is None:
            return None
        return float(row["median"])


# ---- Negotiation CRUD ----------------------------------------------------

def create_negotiation(
    listing_id: int,
    strategy: str,
    strategy_inputs: dict[str, Any],
    current_offer: float,
) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO negotiations (listing_id, strategy, strategy_inputs_json, current_offer)
            VALUES (?, ?, ?, ?)
            """,
            (listing_id, strategy, json.dumps(strategy_inputs), current_offer),
        )
        return cur.lastrowid


def get_negotiation(negotiation_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM negotiations WHERE id = ?", (negotiation_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["strategy_inputs"] = json.loads(d.pop("strategy_inputs_json") or "null")
        return d


def list_active_negotiations() -> list[dict[str, Any]]:
    """All negotiations the background poller should check for new seller
    replies. Returns rows joined with their listing's ebay_item_id and
    search_id so the poller doesn't need a follow-up lookup per negotiation.

    "Active" = the negotiation is either waiting for a seller reply
    (status='awaiting_seller') or actively mid-counter-draft (status='open').
    Terminal states (deal / walked_away / timed_out) are excluded.

    Rounds-cap filtering happens downstream (the poller checks the cap before
    firing a counter draft) so we still surface seller messages even when no
    counter is possible — the user may want to manually mark a deal."""
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT n.id AS negotiation_id, n.status, n.rounds,
                   n.listing_id, l.ebay_item_id, l.search_id
              FROM negotiations n
              JOIN listings l ON l.id = n.listing_id
             WHERE n.status IN ('awaiting_seller', 'open')
             ORDER BY n.id ASC
            """
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_negotiation_for_listing(listing_id: int) -> dict[str, Any] | None:
    """Latest non-terminal negotiation for a listing. Used to find a
    negotiation by listing_id when the route only knows the listing."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM negotiations WHERE listing_id = ? "
            "AND status NOT IN ('deal', 'walked_away', 'timed_out') "
            "ORDER BY id DESC LIMIT 1",
            (listing_id,),
        ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["strategy_inputs"] = json.loads(d.pop("strategy_inputs_json") or "null")
        return d


def update_negotiation_status(negotiation_id: int, status: str) -> None:
    """Set status; if terminal (walked_away / deal / timed_out) also set the
    matching timestamp column so we have an audit trail."""
    timestamp_col = {
        "walked_away": "walked_away_at",
        "deal": "deal_at",
    }.get(status)
    with get_conn() as conn:
        if timestamp_col:
            conn.execute(
                f"UPDATE negotiations SET status = ?, {timestamp_col} = datetime('now') WHERE id = ?",
                (status, negotiation_id),
            )
        else:
            conn.execute("UPDATE negotiations SET status = ? WHERE id = ?", (status, negotiation_id))


# ---- Message CRUD --------------------------------------------------------

def add_message(
    negotiation_id: int,
    role: str,                # 'agent' | 'seller'
    body: str,
    offer_amount: float | None,
    status: str = "pending",
    *,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cost_usd: float | None = None,
) -> int:
    """Insert a message row.

    `input_tokens`, `output_tokens`, `cost_usd` are populated when the
    drafter (Sonnet) creates the message. Seller messages and legacy callers
    pass nothing and the columns stay NULL — `sum_claude_cost` filters them
    out, so an agent message without usage data simply doesn't contribute
    to the dashboard total.
    """
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO messages (
                negotiation_id, role, body, offer_amount, status,
                input_tokens, output_tokens, cost_usd
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (negotiation_id, role, body, offer_amount, status,
             input_tokens, output_tokens, cost_usd),
        )
        return cur.lastrowid


def get_message(message_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        return dict(row) if row else None


def get_pending_message_for_search(search_id: int) -> dict[str, Any] | None:
    """The single pending agent message for this search's active negotiation.
    Returns the message joined with its negotiation's strategy + current_offer
    so the template can render everything from one query."""
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT m.*, n.strategy AS negotiation_strategy, n.current_offer AS negotiation_offer,
                   n.id AS negotiation_id_join, l.id AS listing_id_join
              FROM messages m
              JOIN negotiations n ON n.id = m.negotiation_id
              JOIN listings l ON l.id = n.listing_id
             WHERE l.search_id = ?
               AND m.status = 'pending'
               AND m.role = 'agent'
             ORDER BY m.id DESC LIMIT 1
            """,
            (search_id,),
        ).fetchone()
        return dict(row) if row else None


def approve_message(
    message_id: int,
    new_body: str | None = None,
    new_offer_amount: float | None = None,
) -> None:
    """Set status='approved' + approved_at; optionally replace body and/or
    offer_amount (when the user edited either before approving). The offer_amount
    is the value PlaceOffer will use, so user edits here are authoritative."""
    sets = ["status = 'approved'", "approved_at = datetime('now')"]
    params: list[Any] = []
    if new_body is not None:
        sets.append("body = ?")
        params.append(new_body)
    if new_offer_amount is not None:
        sets.append("offer_amount = ?")
        params.append(new_offer_amount)
    params.append(message_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id = ?", params)


def set_best_offer_id(negotiation_id: int, offer_id: str) -> None:
    """Record the BestOfferID returned by PlaceOffer so GetBestOffers polling
    can locate the structured offer state later."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE negotiations SET ebay_best_offer_id = ? WHERE id = ?",
            (offer_id, negotiation_id),
        )


def reject_message(message_id: int) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE messages SET status = 'rejected' WHERE id = ?", (message_id,))


def set_message_sent(message_id: int) -> None:
    """Record successful transmission to eBay. Bumps negotiation rounds because
    each 'sent' represents one agent-originated round we're committing to."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET status = 'sent', sent_at = datetime('now') WHERE id = ?",
            (message_id,),
        )
        # Bump the round counter on the parent negotiation in the same transaction —
        # rounds is what the 3-round cap consults, so it must move atomically with sent.
        conn.execute(
            "UPDATE negotiations SET rounds = rounds + 1, status = 'awaiting_seller' "
            "WHERE id = (SELECT negotiation_id FROM messages WHERE id = ?)",
            (message_id,),
        )


def get_messages_by_negotiation(negotiation_id: int) -> list[dict[str, Any]]:
    """Full ordered conversation history. Used by counter-drafting to give Claude
    full context, and by the template to render the message thread."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE negotiation_id = ? ORDER BY id ASC",
            (negotiation_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def mark_negotiation_deal(negotiation_id: int, final_price: float) -> None:
    """Terminal state: buyer accepted seller's final price (or vice versa).
    Records the agreed price + deal_at timestamp for later analysis."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE negotiations SET status = 'deal', final_price = ?, deal_at = datetime('now') WHERE id = ?",
            (final_price, negotiation_id),
        )
