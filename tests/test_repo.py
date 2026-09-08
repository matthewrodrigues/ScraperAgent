"""Tests for db.repo functions that aren't already exercised by route tests."""

import sqlite3

import pytest

import config
from db import repo


def _make_search(**overrides) -> int:
    defaults = dict(
        criteria_nl="test",
        criteria_structured={"title_keywords": "x"},
        max_price=100.0,
    )
    defaults.update(overrides)
    return repo.create_search(**defaults)


def _listing(**overrides) -> dict:
    base = {
        "ebay_item_id": "v1|111|0",
        "title": "Test item",
        "price": 50.0,
        "shipping_cost": 5.0,
        "condition": "Used",
        "seller_id": "seller1",
        "seller_rating": 99.5,
        "seller_feedback_count": 100,
        "url": "https://www.ebay.com/itm/111",
        "image_url": "https://example.test/img.jpg",
        "listed_at": "2026-06-01T00:00:00Z",
        "raw_data": {"original": "json"},
    }
    base.update(overrides)
    return base


def test_add_listings_inserts_all_and_returns_ids(tmp_db):
    search_id = _make_search()
    ids = repo.add_listings(search_id, [_listing(ebay_item_id=f"v1|{i}|0") for i in range(3)])

    assert len(ids) == 3
    assert all(isinstance(i, int) for i in ids)
    rows = repo.list_listings(search_id)
    assert len(rows) == 3


def test_add_listings_empty_is_noop(tmp_db):
    search_id = _make_search()
    assert repo.add_listings(search_id, []) == []


def test_add_listings_rolls_back_on_partial_failure(tmp_db):
    """If row N fails (e.g. missing search_id FK), rows 0..N-1 must not persist."""
    search_id = _make_search()
    good = _listing(ebay_item_id="v1|good|0")
    # Force a foreign-key violation: search_id 99999 doesn't exist. We do this
    # by patching the inserted search_id mid-call via a wrapper list.
    bad = _listing(ebay_item_id="v1|bad|0")
    bad_search_id = 99999

    with pytest.raises(sqlite3.IntegrityError):
        # We can't easily mix search_ids inside add_listings (it takes one arg),
        # so instead we trigger the failure via a bad price (NOT NULL violation).
        repo.add_listings(search_id, [good, {**bad, "price": None}])

    # Despite the good row being inserted first, the rollback should remove it.
    assert repo.list_listings(search_id) == []


def test_list_listings_orders_by_price_ascending(tmp_db):
    search_id = _make_search()
    repo.add_listings(search_id, [
        _listing(ebay_item_id="v1|a|0", price=50.0),
        _listing(ebay_item_id="v1|b|0", price=10.0),
        _listing(ebay_item_id="v1|c|0", price=30.0),
    ])
    rows = repo.list_listings(search_id)
    assert [r["price"] for r in rows] == [10.0, 30.0, 50.0]


def test_set_search_error_populates_column(tmp_db):
    search_id = _make_search()
    repo.set_search_error(search_id, "eBay returned 503")

    row = repo.get_search(search_id)
    assert row["error_message"] == "eBay returned 503"


def test_init_db_adds_error_message_column_to_existing_db(tmp_db):
    """Migration helper should be idempotent and tolerate a pre-existing column."""
    # tmp_db already initialized the schema with the column; re-running init must not error.
    repo.init_db()
    row = repo.get_search(_make_search())
    assert "error_message" in row


def test_mark_listing_selected_sets_selected_at(tmp_db):
    search_id = _make_search()
    ids = repo.add_listings(search_id, [_listing()])
    repo.mark_listing_selected(ids[0])

    row = repo.get_listing(ids[0])
    assert row["selected_at"] is not None


def test_mark_listing_selected_is_idempotent(tmp_db):
    """Selecting twice keeps the first timestamp — important for retried HTTP requests."""
    search_id = _make_search()
    ids = repo.add_listings(search_id, [_listing()])
    repo.mark_listing_selected(ids[0])
    first_ts = repo.get_listing(ids[0])["selected_at"]
    repo.mark_listing_selected(ids[0])
    second_ts = repo.get_listing(ids[0])["selected_at"]

    assert first_ts == second_ts


def test_get_selected_listing_returns_none_when_none_selected(tmp_db):
    search_id = _make_search()
    repo.add_listings(search_id, [_listing(), _listing(ebay_item_id="v1|222|0")])
    assert repo.get_selected_listing(search_id) is None


def test_get_selected_listing_returns_the_first_selected(tmp_db):
    search_id = _make_search()
    ids = repo.add_listings(search_id, [
        _listing(ebay_item_id="v1|a|0"),
        _listing(ebay_item_id="v1|b|0"),
    ])
    repo.mark_listing_selected(ids[1])

    selected = repo.get_selected_listing(search_id)
    assert selected["id"] == ids[1]


def test_get_listing_unknown_id_returns_none(tmp_db):
    assert repo.get_listing(999_999) is None


# ---- Reference prices ----

def test_add_reference_price_and_list(tmp_db):
    search_id = _make_search()
    rid = repo.add_reference_price(
        search_id=search_id,
        source="google_shopping",
        condition="new",
        median=250.0, p25=220.0, p75=280.0,
        raw_data=[{"price": 250.0, "url": "u"}],
        cost_usd=0.07,
    )
    assert isinstance(rid, int)
    rows = repo.list_reference_prices(search_id)
    assert len(rows) == 1
    assert rows[0]["source"] == "google_shopping"
    assert rows[0]["median"] == 250.0
    assert rows[0]["cost_usd"] == 0.07
    assert rows[0]["raw_data"] == [{"price": 250.0, "url": "u"}]


def test_sum_apify_cost_returns_zero_when_no_rows(tmp_db):
    search_id = _make_search()
    assert repo.sum_apify_cost(search_id) == 0.0


def test_sum_apify_cost_aggregates(tmp_db):
    search_id = _make_search()
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.05)
    repo.add_reference_price(search_id, "amazon",          "new", 110.0, 100.0, 120.0, [], 0.12)
    assert repo.sum_apify_cost(search_id) == pytest.approx(0.17)


def test_sum_apify_cost_is_scoped_to_search(tmp_db):
    a = _make_search()
    b = _make_search()
    repo.add_reference_price(a, "google_shopping", "new", 1.0, 1.0, 1.0, [], 0.10)
    repo.add_reference_price(b, "google_shopping", "new", 1.0, 1.0, 1.0, [], 0.20)
    assert repo.sum_apify_cost(a) == pytest.approx(0.10)
    assert repo.sum_apify_cost(b) == pytest.approx(0.20)


# ---- Negotiation + message CRUD ----

def _seeded_listing(tmp_db) -> tuple[int, int]:
    """Helper: create a search + one listing; return (search_id, listing_id)."""
    search_id = _make_search()
    ids = repo.add_listings(search_id, [_listing()])
    return search_id, ids[0]


def test_create_and_get_negotiation(tmp_db):
    search_id, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(
        listing_id=listing_id,
        strategy="anchor_low",
        strategy_inputs={"gap": 0.2, "asking": 100.0},
        current_offer=85.0,
    )
    n = repo.get_negotiation(nid)
    assert n["strategy"] == "anchor_low"
    assert n["status"] == "open"
    assert n["current_offer"] == 85.0
    assert n["strategy_inputs"] == {"gap": 0.2, "asking": 100.0}


def test_update_negotiation_status_terminal_records_timestamp(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "time_pressure", {}, 50.0)
    repo.update_negotiation_status(nid, "walked_away")

    n = repo.get_negotiation(nid)
    assert n["status"] == "walked_away"
    assert n["walked_away_at"] is not None


def test_get_active_negotiation_for_listing_returns_open(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 50.0)
    assert repo.get_active_negotiation_for_listing(listing_id)["id"] == nid

    repo.update_negotiation_status(nid, "walked_away")
    assert repo.get_active_negotiation_for_listing(listing_id) is None


def test_add_and_get_message(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 85.0)
    mid = repo.add_message(nid, role="agent", body="Would you accept $85?", offer_amount=85.0)

    m = repo.get_message(mid)
    assert m["body"] == "Would you accept $85?"
    assert m["status"] == "pending"
    assert m["offer_amount"] == 85.0


def test_get_pending_message_for_search_joins_negotiation(tmp_db):
    search_id, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "split_the_difference", {}, 90.0)
    repo.add_message(nid, "agent", "hello", 90.0)

    pending = repo.get_pending_message_for_search(search_id)
    assert pending is not None
    assert pending["body"] == "hello"
    assert pending["negotiation_strategy"] == "split_the_difference"
    assert pending["negotiation_offer"] == 90.0


def test_get_pending_message_returns_none_when_approved(tmp_db):
    search_id, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "x", 80.0)
    repo.approve_message(mid)
    assert repo.get_pending_message_for_search(search_id) is None


def test_approve_message_with_edited_body(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "original", 80.0)
    repo.approve_message(mid, new_body="edited")

    m = repo.get_message(mid)
    assert m["status"] == "approved"
    assert m["body"] == "edited"
    assert m["approved_at"] is not None


def test_reject_message_sets_status(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "x", 80.0)
    repo.reject_message(mid)
    assert repo.get_message(mid)["status"] == "rejected"


def test_set_message_sent_records_sent_state_and_bumps_rounds(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "Hi, would you accept $80?", 80.0, status="approved")

    repo.set_message_sent(mid)

    m = repo.get_message(mid)
    assert m["status"] == "sent"
    assert m["sent_at"] is not None
    # Round counter bumped + status flipped to awaiting_seller
    n = repo.get_negotiation(nid)
    assert n["rounds"] == 1
    assert n["status"] == "awaiting_seller"


def test_set_message_sent_bumps_per_round(tmp_db):
    """Calling set_message_sent multiple times (one per round) increments correctly."""
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    for i in range(3):
        mid = repo.add_message(nid, "agent", f"Round {i+1} message body that is long enough", 80.0, status="approved")
        repo.set_message_sent(mid)
    assert repo.get_negotiation(nid)["rounds"] == 3


def test_get_messages_by_negotiation_returns_chronological(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    repo.add_message(nid, "agent", "first agent message", 80.0)
    repo.add_message(nid, "seller", "seller reply", None)
    repo.add_message(nid, "agent", "second agent message", 85.0)

    msgs = repo.get_messages_by_negotiation(nid)
    assert [m["role"] for m in msgs] == ["agent", "seller", "agent"]


def test_mark_negotiation_deal_sets_terminal_state(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    repo.mark_negotiation_deal(nid, final_price=75.0)

    n = repo.get_negotiation(nid)
    assert n["status"] == "deal"
    assert n["final_price"] == 75.0
    assert n["deal_at"] is not None


# ---- Best Offer plumbing (added with PlaceOffer integration) ----

def test_approve_message_can_update_offer_amount(tmp_db):
    """User edits the offer price on the approval card → must overwrite the
    LLM's original number so PlaceOffer sends the user-authoritative amount."""
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "would you take $80?", 80.0)
    repo.approve_message(mid, new_body="would you take $75?", new_offer_amount=75.0)

    m = repo.get_message(mid)
    assert m["status"] == "approved"
    assert m["body"] == "would you take $75?"
    assert m["offer_amount"] == 75.0


def test_set_best_offer_id_persists(tmp_db):
    """Best-offer id must round-trip on the negotiation row so polling has a key."""
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    repo.set_best_offer_id(nid, "1234567890")
    assert repo.get_negotiation(nid)["ebay_best_offer_id"] == "1234567890"


def test_listings_persist_buying_options(tmp_db):
    """BEST_OFFER / FIXED_PRICE / AUCTION must survive the round-trip so the
    route can branch the send button on it without re-parsing raw_data."""
    search_id = _make_search()
    ids = repo.add_listings(search_id, [
        _listing(ebay_item_id="v1|bo|0", buying_options=["FIXED_PRICE", "BEST_OFFER"]),
        _listing(ebay_item_id="v1|fp|0", buying_options=["FIXED_PRICE"]),
    ])
    rows = {r["ebay_item_id"]: r for r in repo.list_listings(search_id)}
    # Stored as a comma-joined string for simple SQL LIKE checks; deserialize on read.
    assert "BEST_OFFER" in (rows["v1|bo|0"]["buying_options"] or "")
    assert "BEST_OFFER" not in (rows["v1|fp|0"]["buying_options"] or "")


# ---- Per-search cost dashboard ----

def test_add_message_persists_usage_kwargs(tmp_db):
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(
        nid, "agent", "Would you accept $80?", 80.0,
        input_tokens=1500, output_tokens=80, cost_usd=0.0057,
    )
    m = repo.get_message(mid)
    assert m["input_tokens"] == 1500
    assert m["output_tokens"] == 80
    assert m["cost_usd"] == pytest.approx(0.0057)


def test_add_message_without_usage_stores_null(tmp_db):
    """Legacy callers + seller messages must work unchanged."""
    _, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "seller", "I can do $90.", None)
    m = repo.get_message(mid)
    assert m["input_tokens"] is None
    assert m["output_tokens"] is None
    assert m["cost_usd"] is None


def test_sum_claude_cost_aggregates_per_search(tmp_db):
    search_a, listing_a = _seeded_listing(tmp_db)
    nid_a = repo.create_negotiation(listing_a, "anchor_low", {}, 80.0)
    repo.add_message(nid_a, "agent", "long enough body for round 1", 80.0, cost_usd=0.10)
    repo.add_message(nid_a, "agent", "long enough body for round 2", 75.0, cost_usd=0.05)
    # Seller message + a NULL-cost agent message both excluded.
    repo.add_message(nid_a, "seller", "long enough seller reply", None)
    repo.add_message(nid_a, "agent", "legacy agent with no usage tracked", 70.0)

    # Different search should not contaminate.
    search_b = _make_search()
    ids = repo.add_listings(search_b, [_listing(ebay_item_id="v1|other|0")])
    nid_b = repo.create_negotiation(ids[0], "anchor_low", {}, 50.0)
    repo.add_message(nid_b, "agent", "another search's costs stay isolated", 45.0, cost_usd=0.99)

    assert repo.sum_claude_cost(search_a) == pytest.approx(0.15)
    assert repo.sum_claude_cost(search_b) == pytest.approx(0.99)


def test_sum_claude_cost_returns_zero_when_empty(tmp_db):
    search_id = _make_search()
    assert repo.sum_claude_cost(search_id) == 0.0


def test_sum_total_cost_shape_and_arithmetic(tmp_db):
    search_id, listing_id = _seeded_listing(tmp_db)
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.07)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    repo.add_message(nid, "agent", "long enough body to clear validator", 80.0, cost_usd=0.0123)

    costs = repo.sum_total_cost(search_id)
    assert set(costs.keys()) == {"search", "apify", "claude", "total"}
    assert costs["search"] == pytest.approx(0.0)
    assert costs["apify"] == pytest.approx(0.07)
    assert costs["claude"] == pytest.approx(0.0123)
    assert costs["total"] == pytest.approx(0.07 + 0.0123)


def test_set_search_cost_and_read_back(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_cost(search_id, 0.0512)
    assert repo.get_search(search_id)["search_cost_usd"] == pytest.approx(0.0512)


def test_search_cost_defaults_to_zero(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    assert repo.get_search(search_id)["search_cost_usd"] == 0.0


def test_set_search_warning_and_read_back(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_warning(search_id, "Seller feedback unavailable.")
    assert repo.get_search(search_id)["warning_message"] == "Seller feedback unavailable."


def test_warning_message_defaults_to_none(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    assert repo.get_search(search_id)["warning_message"] is None


def test_migration_adds_columns_to_a_preexisting_database(tmp_path, monkeypatch):
    """The tmp_db fixture builds a fresh schema, so it never exercises the
    ALTER TABLE path. A real user's broker.db predates these columns."""
    import sqlite3
    db_path = tmp_path / "legacy.db"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE searches ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " criteria_nl TEXT NOT NULL,"
        " criteria_structured_json TEXT NOT NULL,"
        " max_price REAL NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending',"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    conn.close()

    repo.init_db()

    with repo.get_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(searches)").fetchall()}
    assert "search_cost_usd" in cols
    assert "warning_message" in cols
    assert "costs_are_estimates" in cols


def test_set_search_costs_are_estimates_and_read_back(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_costs_are_estimates(search_id)
    assert repo.get_search(search_id)["costs_are_estimates"] == 1


def test_costs_are_estimates_defaults_to_zero(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    assert repo.get_search(search_id)["costs_are_estimates"] == 0


def test_sum_total_cost_includes_search_cost(tmp_db):
    search_id = repo.create_search("headphones", {"title_keywords": "headphones"}, 250.0)
    repo.set_search_cost(search_id, 0.05)
    repo.add_reference_price(
        search_id, source="google_shopping", raw_data={}, median=100.0,
        p25=90.0, p75=110.0, condition="used", cost_usd=0.49,
    )
    totals = repo.sum_total_cost(search_id)
    assert totals["search"] == pytest.approx(0.05)
    assert totals["apify"] == pytest.approx(0.49)
    assert totals["total"] == pytest.approx(0.54)


# ---- Reference-price aggregation for listing ranking ----

def test_get_aggregated_ref_median_returns_none_when_no_rows(tmp_db):
    search_id = _make_search()
    assert repo.get_aggregated_ref_median(search_id) is None


def test_get_aggregated_ref_median_picks_first_non_none(tmp_db):
    """First non-None median wins. Mirrors strategies/__init__.py logic so
    both call sites are semantically aligned."""
    search_id = _make_search()
    # First row has NULL median (e.g. actor failed to compute one); the
    # aggregator should skip and return the next available.
    repo.add_reference_price(search_id, "amazon", "new", None, None, None, [], 0.05)
    repo.add_reference_price(search_id, "google_shopping", "new", 120.0, 100.0, 140.0, [], 0.07)
    assert repo.get_aggregated_ref_median(search_id) == 120.0


def test_get_aggregated_ref_median_scoped_to_search(tmp_db):
    a = _make_search()
    b = _make_search()
    repo.add_reference_price(a, "google_shopping", "new", 50.0, 40.0, 60.0, [], 0.05)
    repo.add_reference_price(b, "google_shopping", "new", 999.0, 800.0, 1100.0, [], 0.05)
    assert repo.get_aggregated_ref_median(a) == 50.0
    assert repo.get_aggregated_ref_median(b) == 999.0


# ---- list_active_negotiations (poller candidates) ----

def test_list_active_negotiations_only_returns_active_statuses(tmp_db):
    """Terminal states (deal / walked_away / timed_out) must NOT be returned —
    the poller would burn eBay API calls on closed negotiations otherwise."""
    _, listing_id_a = _seeded_listing(tmp_db)
    _, listing_id_b = _seeded_listing(tmp_db)
    _, listing_id_c = _seeded_listing(tmp_db)
    _, listing_id_d = _seeded_listing(tmp_db)

    nid_open = repo.create_negotiation(listing_id_a, "anchor_low", {}, 80.0)
    nid_awaiting = repo.create_negotiation(listing_id_b, "anchor_low", {}, 80.0)
    repo.update_negotiation_status(nid_awaiting, "awaiting_seller")
    nid_deal = repo.create_negotiation(listing_id_c, "anchor_low", {}, 80.0)
    repo.mark_negotiation_deal(nid_deal, 75.0)
    nid_walked = repo.create_negotiation(listing_id_d, "anchor_low", {}, 80.0)
    repo.update_negotiation_status(nid_walked, "walked_away")

    active = repo.list_active_negotiations()
    active_ids = {a["negotiation_id"] for a in active}
    assert nid_open in active_ids
    assert nid_awaiting in active_ids
    assert nid_deal not in active_ids
    assert nid_walked not in active_ids


def test_list_active_negotiations_includes_ebay_item_id_and_search_id(tmp_db):
    """The poller needs `ebay_item_id` and `search_id` per row so it can fetch
    messages and invoke the counter graph without a follow-up DB lookup."""
    search_id, listing_id = _seeded_listing(tmp_db)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)

    active = repo.list_active_negotiations()
    assert len(active) == 1
    row = active[0]
    assert row["negotiation_id"] == nid
    assert row["listing_id"] == listing_id
    assert row["search_id"] == search_id
    assert row["ebay_item_id"]  # truthy — comes from listings.ebay_item_id
    assert "rounds" in row
