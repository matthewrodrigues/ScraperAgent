"""End-to-end tests for the searches router using FastAPI's TestClient."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from agents.criteria_parser import ParsedCriteria
from api.main import app
from integrations.ebay_trading import EbayTradingError, NoPartnerRelationshipError
from tests.conftest import TEST_DASHBOARD_PASSWORD


@pytest.fixture
def client(tmp_db):
    """A logged-in client.

    Every route below is behind `RequireAuthMiddleware`, so the fixture signs in
    once and lets TestClient carry the session cookie. Deliberately a real login
    rather than a test-only auth bypass: these are the app's most sensitive
    routes, and they should be exercised through the same middleware a browser
    hits. `tests/test_auth.py` covers the anonymous side.
    """
    c = TestClient(app)
    resp = c.post("/login", data={"password": TEST_DASHBOARD_PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303, f"test login failed: {resp.status_code}"
    return c


@pytest.fixture(autouse=True)
def _mock_negotiate_graph():
    """Selection triggers a BackgroundTask that runs the negotiate graph (which
    hits Claude). Mock it out of every route test by default; tests that
    specifically want to exercise the negotiate flow can opt back in by
    overriding within the test body."""
    with patch("api.routes.searches._run_negotiate_graph") as m:
        yield m


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


from db import repo


@patch("agents.graph.google_shopping.fetch", return_value=([], 0.0))
@patch("agents.graph.search_ebay", return_value=[])
def test_post_searches_persists_and_redirects(_mock_ebay, _mock_gshop, client):
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
    # Graph ran end-to-end on submit; with zero listings we still flip status.
    assert row["status"] == "awaiting_selection"


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


@patch("agents.graph.google_shopping.fetch", return_value=([], 0.0))
@patch("agents.graph.search_ebay", return_value=[])
def test_post_searches_empty_condition_floor_treated_as_null(_mock_ebay, _mock_gshop, client):
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


# ---------- Polling-target content endpoint ----------

def test_get_searches_content_returns_partial_only(client):
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=10.0
    )
    resp = client.get(f"/searches/{search_id}/content")
    assert resp.status_code == 200
    # Returns the inner div but NOT the outer page chrome
    assert 'id="search-content"' in resp.text
    assert "<!doctype" not in resp.text.lower()
    assert "<html" not in resp.text.lower()


def test_get_searches_content_unknown_id_returns_404(client):
    resp = client.get("/searches/999999/content")
    assert resp.status_code == 404


def test_get_searches_content_includes_polling_during_discovering(client):
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=10.0
    )
    repo.update_search_status(search_id, "discovering")
    resp = client.get(f"/searches/{search_id}/content")
    assert "hx-trigger" in resp.text
    assert f"/searches/{search_id}/content" in resp.text


def test_get_searches_content_stops_polling_when_done(client):
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=10.0
    )
    repo.update_search_status(search_id, "awaiting_selection")
    resp = client.get(f"/searches/{search_id}/content")
    assert "hx-trigger" not in resp.text


# ---------- Listing selection ----------

def _seed_search_with_listings(num: int = 3) -> tuple[int, list[int]]:
    """Helper: create a search row and N listings, return (search_id, listing_ids)."""
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=100.0
    )
    repo.update_search_status(search_id, "awaiting_selection")
    listings = [
        {
            "ebay_item_id": f"v1|{i}|0",
            "title": f"Item {i}",
            "price": float(10 + i),
            "url": f"https://example.test/{i}",
            "seller_id": f"seller_{i}",  # required for /send route to construct AAQToPartner
        }
        for i in range(num)
    ]
    ids = repo.add_listings(search_id, listings)
    return search_id, ids


def test_select_listing_marks_selected_and_flips_status(client):
    search_id, ids = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_id}/listings/{ids[1]}/select")

    assert resp.status_code == 200
    assert repo.get_listing(ids[1])["selected_at"] is not None
    assert repo.get_search(search_id)["status"] == "negotiating"
    # Returned fragment should be the swap target div
    assert 'id="search-content"' in resp.text


def test_select_listing_same_listing_twice_is_idempotent(client):
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    first_ts = repo.get_listing(ids[0])["selected_at"]

    resp = client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    assert resp.status_code == 200
    assert repo.get_listing(ids[0])["selected_at"] == first_ts


def test_select_different_listing_after_one_selected_returns_409(client):
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")

    resp = client.post(f"/searches/{search_id}/listings/{ids[1]}/select")
    assert resp.status_code == 409
    # ids[1] must not have been marked
    assert repo.get_listing(ids[1])["selected_at"] is None


def test_select_unknown_listing_returns_404(client):
    search_id, _ = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_id}/listings/999999/select")
    assert resp.status_code == 404


def test_select_listing_belonging_to_other_search_returns_404(client):
    """A listing that exists but is associated with a different search must not
    be selectable from the wrong URL — prevents cross-search write paths."""
    search_a, ids_a = _seed_search_with_listings()
    search_b, _ = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_b}/listings/{ids_a[0]}/select")
    assert resp.status_code == 404
    assert repo.get_listing(ids_a[0])["selected_at"] is None


def test_deselect_clears_selection_and_reverts_status(client):
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    assert repo.get_search(search_id)["status"] == "negotiating"

    resp = client.post(f"/searches/{search_id}/listings/{ids[0]}/deselect")
    assert resp.status_code == 200
    assert repo.get_listing(ids[0])["selected_at"] is None
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


def test_deselect_then_select_different_listing_succeeds(client):
    """After deselect, the conflict guard must let a different listing be picked."""
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    client.post(f"/searches/{search_id}/listings/{ids[0]}/deselect")

    resp = client.post(f"/searches/{search_id}/listings/{ids[1]}/select")
    assert resp.status_code == 200
    assert repo.get_listing(ids[1])["selected_at"] is not None


def test_deselect_unselected_listing_is_idempotent(client):
    """Deselecting something that was never selected must not error."""
    search_id, ids = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_id}/listings/{ids[0]}/deselect")
    assert resp.status_code == 200
    assert repo.get_listing(ids[0])["selected_at"] is None
    # Status was 'awaiting_selection' to start; must not have changed.
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


def test_deselect_listing_belonging_to_other_search_returns_404(client):
    search_a, ids_a = _seed_search_with_listings()
    search_b, _ = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_b}/listings/{ids_a[0]}/deselect")
    assert resp.status_code == 404


# ---------- Negotiate trigger on selection ----------

def test_select_fires_negotiate_background_task(client, _mock_negotiate_graph):
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    _mock_negotiate_graph.assert_called_once_with(search_id, ids[0])


def test_select_does_not_refire_on_idempotent_re_select(client, _mock_negotiate_graph):
    """Re-selecting the same listing must NOT spawn a duplicate Claude call."""
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    assert _mock_negotiate_graph.call_count == 1


def test_deselect_cascades_negotiation_to_walked_away(client):
    """Deselect marks any active negotiation as walked_away and rejects its pending message."""
    search_id, ids = _seed_search_with_listings()
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")
    # Simulate that the negotiate graph completed and persisted a pending message:
    nid = repo.create_negotiation(ids[0], "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "Hi, would you accept $85?", 85.0)

    client.post(f"/searches/{search_id}/listings/{ids[0]}/deselect")

    assert repo.get_negotiation(nid)["status"] == "walked_away"
    assert repo.get_message(mid)["status"] == "rejected"
    assert repo.get_search(search_id)["status"] == "awaiting_selection"


# ---------- Approve message ----------

def _seed_pending_message(num_listings: int = 1) -> tuple[int, int, int]:
    """Create a search + listing + selected + negotiation + pending message.
    Returns (search_id, listing_id, message_id)."""
    search_id, ids = _seed_search_with_listings(num=num_listings)
    listing_id = ids[0]
    repo.mark_listing_selected(listing_id)
    repo.update_search_status(search_id, "negotiating")
    nid = repo.create_negotiation(listing_id, "anchor_low", {"asking": 100.0}, 85.0)
    mid = repo.add_message(nid, "agent", "Hi, would you accept $85 for this item?", 85.0)
    return search_id, listing_id, mid


def test_approve_message_without_edit_marks_approved_and_flips_status(client):
    search_id, _, mid = _seed_pending_message()
    resp = client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})
    assert resp.status_code == 200
    assert repo.get_message(mid)["status"] == "approved"
    assert repo.get_search(search_id)["status"] == "awaiting_send"


def test_approve_message_with_edited_body_persists_edit(client):
    search_id, _, mid = _seed_pending_message()
    edited = "Hello, I would like to offer $85 for the headphones. Please let me know."
    resp = client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": edited})
    assert resp.status_code == 200
    m = repo.get_message(mid)
    assert m["status"] == "approved"
    assert m["body"] == edited


def test_approve_unknown_message_returns_404(client):
    search_id, _ = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_id}/messages/999999/approve", data={"body": ""})
    assert resp.status_code == 404


def test_approve_message_belonging_to_other_search_returns_404(client):
    """Defense-in-depth: stale URL can't approve a message from a different search."""
    search_a, _, mid_a = _seed_pending_message()
    search_b, _ = _seed_search_with_listings()
    resp = client.post(f"/searches/{search_b}/messages/{mid_a}/approve", data={"body": ""})
    assert resp.status_code == 404
    assert repo.get_message(mid_a)["status"] == "pending"


def test_approve_rejects_too_short_edited_body(client):
    search_id, _, mid = _seed_pending_message()
    resp = client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": "too short"})
    assert resp.status_code == 400
    assert repo.get_message(mid)["status"] == "pending"


# ---------- Send via eBay ----------

def _approved_message(client) -> tuple[int, int, int]:
    """Seed search + listing + selected + negotiation + approved (not yet sent) message."""
    search_id, listing_id, mid = _seed_pending_message()
    client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})
    return search_id, listing_id, mid


@patch("api.routes.searches.ebay_trading.send_member_message")
def test_send_message_calls_ebay_and_marks_sent(mock_send, client):
    search_id, listing_id, mid = _approved_message(client)
    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")

    assert resp.status_code == 200
    listing = repo.get_listing(listing_id)
    mock_send.assert_called_once_with(listing["ebay_item_id"], listing["seller_id"], repo.get_message(mid)["body"])
    m = repo.get_message(mid)
    assert m["status"] == "sent"
    assert m["sent_at"] is not None
    # Search reverts to negotiating; rounds counter bumped on negotiation.
    assert repo.get_search(search_id)["status"] == "negotiating"
    n = repo.get_negotiation(m["negotiation_id"])
    assert n["rounds"] == 1
    assert n["status"] == "awaiting_seller"


@patch("api.routes.searches.ebay_trading.send_member_message")
def test_send_message_is_idempotent_on_already_sent(mock_send, client):
    """Hitting Send twice (browser retry, double-click) must not re-call eBay."""
    search_id, _, mid = _approved_message(client)
    client.post(f"/searches/{search_id}/messages/{mid}/send")
    mock_send.reset_mock()

    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 200
    mock_send.assert_not_called()


@patch("api.routes.searches.ebay_trading.send_member_message")
def test_send_message_refuses_unapproved(mock_send, client):
    """Can't send a message that's still pending approval."""
    search_id, _, mid = _seed_pending_message()
    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 409
    mock_send.assert_not_called()


@patch("api.routes.searches.ebay_trading.send_member_message")
def test_send_message_refuses_when_listing_has_no_seller_id(mock_send, client):
    """eBay's Trading API requires the seller username; if we never captured
    it (rare race in Browse responses), surface a clear 409 instead of letting
    eBay reject the call with a generic message."""
    # Build a search + listing without seller_id, then approve a draft on it
    search_id = repo.create_search(criteria_nl="x", criteria_structured={}, max_price=100.0)
    repo.update_search_status(search_id, "awaiting_selection")
    ids = repo.add_listings(search_id, [{
        "ebay_item_id": "v1|noseller|0", "title": "x", "price": 10.0,
        "url": "https://x", "seller_id": None,
    }])
    listing_id = ids[0]
    repo.mark_listing_selected(listing_id)
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 80.0)
    mid = repo.add_message(nid, "agent", "this is a long enough body to test", 80.0)
    client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})

    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 409
    mock_send.assert_not_called()


@patch("api.routes.searches.ebay_trading.send_member_message",
       side_effect=NoPartnerRelationshipError("not the partner"))
def test_send_message_no_partner_renders_manual_notice_with_200(_mock_send, client):
    """Pre-purchase listings reject AAQToPartner with a 'not the partner' error.
    The route must NOT return 502 — it should re-render the page with a notice
    and leave the message in 'approved' state so the user can copy-paste."""
    search_id, _, mid = _approved_message(client)
    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 200
    assert "Manual contact required" in resp.text
    # Message stays approved (not sent), so the mark-sent button is available next
    assert repo.get_message(mid)["status"] == "approved"


@patch("api.routes.searches.ebay_trading.send_member_message", side_effect=EbayTradingError("network down"))
def test_send_message_surfaces_ebay_failure_as_502(_mock_send, client):
    search_id, _, mid = _approved_message(client)
    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 502
    # Message stays in approved state — user can retry or copy-paste manually.
    assert repo.get_message(mid)["status"] == "approved"


def test_mark_sent_advances_state_without_calling_ebay(client):
    """User confirms manual send via UI — bumps rounds + flips to awaiting_seller
    exactly like a successful API send, but no eBay call is made."""
    search_id, _, mid = _approved_message(client)
    resp = client.post(f"/searches/{search_id}/messages/{mid}/mark-sent")
    assert resp.status_code == 200
    m = repo.get_message(mid)
    assert m["status"] == "sent"
    assert m["sent_at"] is not None
    n = repo.get_negotiation(m["negotiation_id"])
    assert n["rounds"] == 1
    assert n["status"] == "awaiting_seller"


def test_mark_sent_refuses_unapproved(client):
    search_id, _, mid = _seed_pending_message()
    resp = client.post(f"/searches/{search_id}/messages/{mid}/mark-sent")
    assert resp.status_code == 409


def test_mark_sent_idempotent_on_already_sent(client):
    search_id, _, mid = _approved_message(client)
    client.post(f"/searches/{search_id}/messages/{mid}/mark-sent")
    first_sent_at = repo.get_message(mid)["sent_at"]

    resp = client.post(f"/searches/{search_id}/messages/{mid}/mark-sent")
    assert resp.status_code == 200
    assert repo.get_message(mid)["sent_at"] == first_sent_at


# ---------- Check replies ----------

def _negotiation_with_sent_message(client) -> tuple[int, int, int]:
    """Seed: approved + sent (so we're awaiting_seller and ready to check replies)."""
    with patch("api.routes.searches.ebay_trading.send_member_message"):
        search_id, listing_id, mid = _approved_message(client)
        client.post(f"/searches/{search_id}/messages/{mid}/send")
    return search_id, listing_id, repo.get_message(mid)["negotiation_id"]


@patch("api.routes.searches._run_counter_graph")
@patch("api.routes.searches.ebay_trading.get_messages_for_item")
def test_check_replies_persists_new_messages_and_fires_counter(mock_get, mock_counter, client):
    search_id, listing_id, nid = _negotiation_with_sent_message(client)
    mock_get.return_value = [{
        "message_id": "1",
        "sender": "seller_x",
        "body": "I can do $90 firm.",
        "received_at": "2026-06-17T10:00:00Z",
        "item_id": "v1|0|0",
    }]

    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-replies")
    assert resp.status_code == 200

    # Seller message persisted
    msgs = repo.get_messages_by_negotiation(nid)
    seller_msgs = [m for m in msgs if m["role"] == "seller"]
    assert len(seller_msgs) == 1
    assert seller_msgs[0]["body"] == "I can do $90 firm."
    # Counter draft fires in the background
    mock_counter.assert_called_once_with(search_id, listing_id, nid)
    # Negotiation reset from awaiting_seller back to open (drafting next round)
    assert repo.get_negotiation(nid)["status"] == "open"


@patch("api.routes.searches._run_counter_graph")
@patch("api.routes.searches.ebay_trading.get_messages_for_item")
def test_check_replies_dedups_known_messages(mock_get, mock_counter, client):
    search_id, listing_id, nid = _negotiation_with_sent_message(client)
    # Pre-populate the same seller message
    repo.add_message(nid, "seller", "Already saw this one.", None, status="received")
    mock_get.return_value = [{
        "message_id": "1", "sender": "x", "body": "Already saw this one.",
        "received_at": "2026-06-17T10:00:00Z", "item_id": "x",
    }]

    client.post(f"/searches/{search_id}/listings/{listing_id}/check-replies")

    seller_msgs = [m for m in repo.get_messages_by_negotiation(nid) if m["role"] == "seller"]
    assert len(seller_msgs) == 1  # still just the original; no duplicate persisted
    mock_counter.assert_not_called()  # no new messages = no counter draft


@patch("api.routes.searches._run_counter_graph")
@patch("api.routes.searches.ebay_trading.get_messages_for_item", return_value=[])
def test_check_replies_no_new_messages_does_nothing_meaningful(_mock_get, mock_counter, client):
    search_id, listing_id, _ = _negotiation_with_sent_message(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-replies")
    assert resp.status_code == 200
    mock_counter.assert_not_called()


@patch("api.routes.searches._run_counter_graph")
@patch("api.routes.searches.ebay_trading.get_messages_for_item")
def test_check_replies_skips_counter_at_round_cap(mock_get, mock_counter, client):
    """Even with a fresh seller reply, no counter is fired once we've already
    used our 3 rounds. UI must offer walk-away or deal instead."""
    search_id, listing_id, nid = _negotiation_with_sent_message(client)
    # Manually bump rounds to the cap
    with repo.get_conn() as conn:
        conn.execute("UPDATE negotiations SET rounds = 3 WHERE id = ?", (nid,))
    mock_get.return_value = [{"message_id": "1", "sender": "x", "body": "Final $85.",
                             "received_at": "2026-06-17", "item_id": "x"}]

    client.post(f"/searches/{search_id}/listings/{listing_id}/check-replies")
    mock_counter.assert_not_called()
    # Seller message still gets recorded so the user can read it
    assert any(m["role"] == "seller" and "Final $85" in m["body"]
               for m in repo.get_messages_by_negotiation(nid))


# ---------- Paste-reply fallback ----------

@patch("api.routes.searches._run_counter_graph")
def test_paste_reply_persists_and_fires_counter(mock_counter, client):
    search_id, listing_id, nid = _negotiation_with_sent_message(client)
    resp = client.post(
        f"/searches/{search_id}/listings/{listing_id}/paste-reply",
        data={"body": "Sure, I can do $90."},
    )
    assert resp.status_code == 200
    seller_msgs = [m for m in repo.get_messages_by_negotiation(nid) if m["role"] == "seller"]
    assert len(seller_msgs) == 1
    mock_counter.assert_called_once_with(search_id, listing_id, nid)


def test_paste_reply_rejects_blank_text(client):
    search_id, listing_id, _ = _negotiation_with_sent_message(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/paste-reply", data={"body": "   "})
    assert resp.status_code == 400


# ---------- Walk-away ----------

def test_walk_away_marks_negotiation_terminal(client):
    search_id, listing_id, nid = _negotiation_with_sent_message(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/walk-away")
    assert resp.status_code == 200
    n = repo.get_negotiation(nid)
    assert n["status"] == "walked_away"
    assert n["walked_away_at"] is not None
    assert repo.get_search(search_id)["status"] == "done"


# ---------- Mark deal ----------

def test_mark_deal_records_final_price_and_flips_search_to_done(client):
    search_id, listing_id, nid = _negotiation_with_sent_message(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/deal", data={"final_price": "92.50"})
    assert resp.status_code == 200
    n = repo.get_negotiation(nid)
    assert n["status"] == "deal"
    assert n["final_price"] == 92.50
    assert n["deal_at"] is not None
    assert repo.get_search(search_id)["status"] == "done"


def test_mark_deal_rejects_price_above_max(client):
    """PRD §181: never close a deal above buyer's max_price even on manual entry."""
    search_id, listing_id, _ = _negotiation_with_sent_message(client)
    # Search max_price is 100.0 from _seed_search_with_listings
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/deal", data={"final_price": "150"})
    assert resp.status_code == 400


def test_mark_deal_rejects_non_numeric(client):
    search_id, listing_id, _ = _negotiation_with_sent_message(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/deal", data={"final_price": "not a number"})
    assert resp.status_code == 400


def test_mark_deal_rejects_zero_or_negative(client):
    search_id, listing_id, _ = _negotiation_with_sent_message(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/deal", data={"final_price": "0"})
    assert resp.status_code == 400


# ---------- Best Offer send path ----------

def _seed_pending_message_bo() -> tuple[int, int, int]:
    """Seed search + BO-enabled listing + pending agent draft. The single
    difference from `_seed_pending_message` is buying_options."""
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=400.0
    )
    repo.update_search_status(search_id, "awaiting_selection")
    ids = repo.add_listings(search_id, [{
        "ebay_item_id": "v1|bo_item|0",
        "title": "BO Item",
        "price": 300.0,
        "url": "https://example.test/bo",
        "seller_id": "seller_bo",
        "buying_options": ["FIXED_PRICE", "BEST_OFFER"],
    }])
    listing_id = ids[0]
    repo.mark_listing_selected(listing_id)
    repo.update_search_status(search_id, "negotiating")
    nid = repo.create_negotiation(listing_id, "anchor_low", {}, 250.0)
    mid = repo.add_message(nid, "agent", "Hi, would you accept $250 for this item?", 250.0)
    return search_id, listing_id, mid


@patch("api.routes.searches._run_browser_send")
@patch("api.routes.searches.ebay_trading.send_member_message")
def test_send_message_bo_fires_browser_task_and_marks_sending(mock_aaq, mock_browser, client):
    """BO-eligible listings now go through Playwright browser automation.
    The route flips message status to 'sending' and fires _run_browser_send
    as a BackgroundTask. No eBay API call from the route itself."""
    search_id, listing_id, mid = _seed_pending_message_bo()
    client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})

    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 200
    # No AAQ call ever fires for BO listings.
    mock_aaq.assert_not_called()
    # The background task is queued with the message id.
    mock_browser.assert_called_once_with(mid)
    # Message status flips to 'sending' so polling shows the browser-sending UI.
    assert repo.get_message(mid)["status"] == "sending"


@patch("api.routes.searches._run_browser_send")
def test_send_message_bo_renders_browser_sending_ui(_mock_browser, client):
    """After Send is clicked on a BO listing, the rendered fragment must show
    the 'browser opened' UI block + carry HTMX polling attributes so the
    dashboard auto-refreshes when the task lands."""
    search_id, _, mid = _seed_pending_message_bo()
    client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})

    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 200
    assert "Browser opened — finish on eBay" in resp.text
    # Polling stays active while a message is in 'sending' state.
    assert 'hx-trigger="every 3s"' in resp.text


@patch("api.routes.searches.ebay_browser.place_best_offer_via_browser")
def test_run_browser_send_success_marks_sent_and_bumps_rounds(mock_place, client):
    """When place_best_offer_via_browser returns success, the BackgroundTask
    must mark the message sent + bump the round counter + flip negotiation
    to awaiting_seller — same downstream bookkeeping as an API send."""
    from api.routes.searches import _run_browser_send
    from integrations.ebay_browser import OfferResult

    search_id, listing_id, mid = _seed_pending_message_bo()
    client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})
    # Simulate the route having already flipped status to 'sending'.
    with repo.get_conn() as conn:
        conn.execute("UPDATE messages SET status = 'sending' WHERE id = ?", (mid,))

    mock_place.return_value = OfferResult(success=True, offer_ref=None, screenshot_path=None)
    _run_browser_send(mid)

    m = repo.get_message(mid)
    assert m["status"] == "sent"
    n = repo.get_negotiation(m["negotiation_id"])
    assert n["rounds"] == 1
    assert n["status"] == "awaiting_seller"


@patch("api.routes.searches.ebay_browser.place_best_offer_via_browser")
def test_run_browser_send_timeout_reverts_to_approved(mock_place, client):
    """User didn't click Send within the timeout — the task reverts status
    so they can click Send via Browser again."""
    from api.routes.searches import _run_browser_send
    from integrations.ebay_browser import OfferResult

    _, _, mid = _seed_pending_message_bo()
    with repo.get_conn() as conn:
        conn.execute("UPDATE messages SET status = 'sending' WHERE id = ?", (mid,))

    mock_place.return_value = OfferResult(
        success=False, offer_ref=None, screenshot_path=None,
        error_message="User didn't click Send within 300s",
    )
    _run_browser_send(mid)

    # Status reverts to 'approved' so the user can retry from the dashboard.
    assert repo.get_message(mid)["status"] == "approved"
    # Round counter does NOT bump on a failed send.
    nid = repo.get_message(mid)["negotiation_id"]
    assert repo.get_negotiation(nid)["rounds"] == 0


@patch("api.routes.searches.ebay_browser.place_best_offer_via_browser")
def test_run_browser_send_browser_error_reverts_to_approved(mock_place, client):
    """Same revert behavior when the integration raises (e.g. eBay UI changed
    and a selector missed) — user can retry. Future iteration could surface
    the screenshot path; for now we just keep the message live."""
    from api.routes.searches import _run_browser_send
    from integrations.ebay_browser import EbayBrowserError

    _, _, mid = _seed_pending_message_bo()
    with repo.get_conn() as conn:
        conn.execute("UPDATE messages SET status = 'sending' WHERE id = ?", (mid,))

    mock_place.side_effect = EbayBrowserError("Couldn't find the 'Make Offer' button")
    _run_browser_send(mid)

    assert repo.get_message(mid)["status"] == "approved"


@patch("api.routes.searches.ebay_trading.send_member_message")
def test_send_message_non_bo_listing_still_uses_aaq(mock_aaq, client):
    """Existing non-BO send path stays unchanged — regression guard. Non-BO
    listings have no buying_options or only FIXED_PRICE, so the route reaches
    the AAQ branch which calls send_member_message."""
    search_id, listing_id, mid = _approved_message(client)  # uses non-BO seed
    resp = client.post(f"/searches/{search_id}/messages/{mid}/send")
    assert resp.status_code == 200
    mock_aaq.assert_called_once()




# ---------- Approve message: editable offer_amount ----------

def test_approve_with_edited_offer_amount_persists(client):
    """User dragging the offer up/down on the approval card must overwrite the
    LLM's number — PlaceOffer reads this column directly."""
    search_id, _, mid = _seed_pending_message_bo()
    resp = client.post(
        f"/searches/{search_id}/messages/{mid}/approve",
        data={"body": "Would you take $240?", "offer_amount": "240"},
    )
    assert resp.status_code == 200
    m = repo.get_message(mid)
    assert m["status"] == "approved"
    assert m["offer_amount"] == 240.0


def test_approve_rejects_offer_above_max_price(client):
    """PRD §187: max_price ceiling enforced at every entry point including user edits."""
    search_id, _, mid = _seed_pending_message_bo()  # max_price = 400.0
    resp = client.post(
        f"/searches/{search_id}/messages/{mid}/approve",
        data={"body": "Wildly high offer here for testing purposes", "offer_amount": "999"},
    )
    assert resp.status_code == 400
    assert repo.get_message(mid)["status"] == "pending"


def test_approve_rejects_non_numeric_offer_amount(client):
    search_id, _, mid = _seed_pending_message_bo()
    resp = client.post(
        f"/searches/{search_id}/messages/{mid}/approve",
        data={"body": "long enough body to clear the validator", "offer_amount": "not a number"},
    )
    assert resp.status_code == 400


# ---------- Check offer status ----------

def _bo_negotiation_with_offer_placed(client) -> tuple[int, int, int]:
    """Seed: BO listing → approved → simulated offer placed.

    With Option 1 (assisted handoff) the send route never calls PlaceOffer, so
    `ebay_best_offer_id` is normally never set today. These tests still cover
    the GetBestOffers polling code path because that path stays useful for the
    future case where an offer id arrives via another route (browser automation,
    successful API send under different auth, etc.). We seed the id directly
    rather than going through the send route."""
    search_id, listing_id, mid = _seed_pending_message_bo()
    client.post(f"/searches/{search_id}/messages/{mid}/approve", data={"body": ""})
    # Assisted handoff: send renders the handoff UI but doesn't call eBay.
    client.post(f"/searches/{search_id}/messages/{mid}/send")
    # User clicks "I sent it manually" after submitting on eBay's UI.
    client.post(f"/searches/{search_id}/messages/{mid}/mark-sent")
    nid = repo.get_message(mid)["negotiation_id"]
    # Simulate that the offer id was captured (placeholder for a future
    # browser-automation path that would set it). check-offer-status only
    # needs the id to be present.
    repo.set_best_offer_id(nid, "OFFER1")
    return search_id, listing_id, nid


@patch("api.routes.searches.ebay_trading.get_best_offer_status",
       return_value={"offer_id": "OFFER1", "status": "Accepted",
                     "counter_amount": None, "seller_message": None})
def test_check_offer_status_accepted_marks_deal(_mock_status, client):
    search_id, listing_id, nid = _bo_negotiation_with_offer_placed(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-offer-status")
    assert resp.status_code == 200
    n = repo.get_negotiation(nid)
    assert n["status"] == "deal"
    assert n["final_price"] == 250.0  # the offer_amount we placed
    assert repo.get_search(search_id)["status"] == "done"


@patch("api.routes.searches.ebay_trading.get_best_offer_status",
       return_value={"offer_id": "OFFER1", "status": "Declined",
                     "counter_amount": None, "seller_message": None})
def test_check_offer_status_declined_walks_away(_mock_status, client):
    search_id, listing_id, nid = _bo_negotiation_with_offer_placed(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-offer-status")
    assert resp.status_code == 200
    assert repo.get_negotiation(nid)["status"] == "walked_away"
    assert repo.get_search(search_id)["status"] == "done"


@patch("api.routes.searches._run_counter_graph")
@patch("api.routes.searches.ebay_trading.get_best_offer_status",
       return_value={"offer_id": "OFFER1", "status": "Countered",
                     "counter_amount": 290.0, "seller_message": "Best I can do is 290."})
def test_check_offer_status_countered_persists_seller_msg_and_fires_counter(
    _mock_status, mock_counter, client,
):
    search_id, listing_id, nid = _bo_negotiation_with_offer_placed(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-offer-status")
    assert resp.status_code == 200
    seller_msgs = [m for m in repo.get_messages_by_negotiation(nid) if m["role"] == "seller"]
    assert len(seller_msgs) == 1
    assert seller_msgs[0]["body"] == "Best I can do is 290."
    assert seller_msgs[0]["offer_amount"] == 290.0
    mock_counter.assert_called_once_with(search_id, listing_id, nid)
    assert repo.get_negotiation(nid)["status"] == "open"


@patch("api.routes.searches._run_counter_graph")
@patch("api.routes.searches.ebay_trading.get_best_offer_status",
       return_value={"offer_id": "OFFER1", "status": "Pending",
                     "counter_amount": None, "seller_message": None})
def test_check_offer_status_pending_is_noop(_mock_status, mock_counter, client):
    search_id, listing_id, nid = _bo_negotiation_with_offer_placed(client)
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-offer-status")
    assert resp.status_code == 200
    assert repo.get_negotiation(nid)["status"] == "awaiting_seller"
    mock_counter.assert_not_called()


def test_check_offer_status_without_placed_offer_returns_409(client):
    """Calling this route before any BO has been placed must fail clearly —
    no offer_id to poll against."""
    search_id, listing_id, mid = _seed_pending_message_bo()
    # No place_best_offer call has happened, so negotiation.ebay_best_offer_id is NULL
    resp = client.post(f"/searches/{search_id}/listings/{listing_id}/check-offer-status")
    assert resp.status_code == 409


# ---------- Per-search cost dashboard ----------

def test_detail_page_renders_cost_summary_line(client):
    """Cost line should show Apify + Claude + total when both have value."""
    search_id, ids = _seed_search_with_listings()
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.07)
    nid = repo.create_negotiation(ids[0], "anchor_low", {}, 80.0)
    repo.add_message(nid, "agent", "long enough body for cost test", 80.0, cost_usd=0.1234)

    resp = client.get(f"/searches/{search_id}/content")
    assert resp.status_code == 200
    body = resp.text
    assert "Search cost:" in body
    assert "$0.19" in body          # total = 0.07 + 0.1234 -> rounds to 0.19
    assert "Apify $0.07" in body
    assert "Claude $0.12" in body


def test_detail_page_cost_line_shows_zero_when_no_spend(client):
    """A brand-new search with no ref-prices and no messages should still
    render a $0.00 cost line — not crash, not omit the section."""
    search_id, _ = _seed_search_with_listings()
    resp = client.get(f"/searches/{search_id}/content")
    assert resp.status_code == 200
    assert "Search cost:" in resp.text
    assert "$0.00" in resp.text


# ---------- Reference-price-based listing ranking ----------

def _seed_listings_with_known_prices(prices: list[float], max_price: float = 1000.0) -> tuple[int, list[int]]:
    """Seed a search with N listings at exact prices. Used for ranking tests
    where the absolute prices vs the ref_median determine sort order."""
    search_id = repo.create_search(
        criteria_nl="x", criteria_structured={"title_keywords": "x"}, max_price=max_price,
    )
    repo.update_search_status(search_id, "awaiting_selection")
    listings = [
        {
            "ebay_item_id": f"v1|{i}|0",
            "title": f"Item {i}",
            "price": p,
            "url": f"https://example.test/{i}",
            "seller_id": f"seller_{i}",
        }
        for i, p in enumerate(prices)
    ]
    return search_id, repo.add_listings(search_id, listings)


def test_listings_sort_by_gap_asc_when_ref_median_present(client):
    """When ref_median is known, listings appear cheapest-vs-market first.
    Prices below median go to the top regardless of absolute value."""
    # Median = $100. Prices straddle: under by 50%, near (+5%), over (+30%).
    search_id, _ = _seed_listings_with_known_prices([130.0, 50.0, 105.0])
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.05)

    resp = client.get(f"/searches/{search_id}/content")
    body = resp.text
    # Expected order in the HTML body: $50 (under), $105 (near), $130 (over).
    # Check by relative substring position.
    pos_50 = body.find("$50.00")
    pos_105 = body.find("$105.00")
    pos_130 = body.find("$130.00")
    assert pos_50 < pos_105 < pos_130, f"order off: 50={pos_50}, 105={pos_105}, 130={pos_130}"


def test_listings_fall_back_to_price_asc_when_no_ref_median(client):
    """Without a ref_median, the existing price-asc ordering must be preserved
    so legacy behavior is identical for searches that lack ref-price data."""
    search_id, _ = _seed_listings_with_known_prices([130.0, 50.0, 105.0])
    # NO add_reference_price — ref_median is None.

    resp = client.get(f"/searches/{search_id}/content")
    body = resp.text
    # Expected order: $50, $105, $130 (pure price ascending).
    assert body.find("$50.00") < body.find("$105.00") < body.find("$130.00")


def test_gap_badges_render_at_threshold_boundaries(client):
    """Strategy chooser thresholds: gap<=0 → under, 0<gap<0.15 → near,
    gap>=0.15 → over. Seed prices that land exactly on each boundary."""
    # Median = $100. Prices: $100 (gap=0, under), $114 (gap=0.14, near), $115 (gap=0.15, over).
    search_id, _ = _seed_listings_with_known_prices([100.0, 114.0, 115.0])
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.05)

    resp = client.get(f"/searches/{search_id}/content")
    body = resp.text
    assert "badge-under" in body
    assert "badge-near" in body
    assert "badge-over" in body


def test_selected_listing_card_shows_gap_badge(client):
    """Selecting a listing should still surface the badge on the selected-row."""
    search_id, ids = _seed_listings_with_known_prices([85.0, 130.0])
    repo.add_reference_price(search_id, "google_shopping", "new", 100.0, 90.0, 110.0, [], 0.05)
    # Select the $85 listing → should show under-market badge in the selected card.
    client.post(f"/searches/{search_id}/listings/{ids[0]}/select")

    resp = client.get(f"/searches/{search_id}/content")
    body = resp.text
    # The selected card includes "Selected for negotiation" as a heading; check
    # the badge appears in the body. Same badge classes as the candidates table.
    assert "Selected for negotiation" in body
    assert "badge-under" in body
