"""Routes for the searches resource.

  POST /searches/parse — HTMX fragment swap; calls Haiku and re-renders the
                         structured-fields fragment with extracted values.
  POST /searches        — Form submit; validates, persists, redirects.
  GET  /searches/{id}   — Stub detail page; echoes the persisted criteria.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

import config
from agents.criteria_parser import SearchSubmission, parse_criteria
from agents.graph import MAX_ROUNDS, build_counter_graph, build_graph, build_negotiate_graph
from db import repo
from agents.poller import process_seller_replies_for_listing
from integrations import ebay_browser, ebay_trading
from strategies import _gap_fraction
from integrations.ebay_browser import (
    EbayBrowserError,
    EbaySessionExpiredError,
)
from integrations.ebay_trading import (
    EbayTradingError,
    NoPartnerRelationshipError,
)


def _listing_accepts_best_offer(listing: dict) -> bool:
    """True when Browse API flagged this item as Best-Offer-eligible. The
    column is the comma-joined string we stored at insert time."""
    bo = listing.get("buying_options") or ""
    return "BEST_OFFER" in bo.split(",")


def _gap_to_badge(gap: float | None) -> tuple[str, str] | None:
    """Map a (price - ref_median) / ref_median value to a ("label", "css_class")
    pair for the template's "vs Market" badge.

    Thresholds match `strategies/__init__.py:_pick_name` exactly so the badge
    on a listing tells the user which strategy will fire if they select it:
      * gap <= 0    → time_pressure  → "Under market" green
      * 0 < gap < 0.15  → split_the_difference  → "Near market" amber
      * gap >= 0.15 → anchor_low  → "Over market" red
    None gap (no ref_median available) → no badge."""
    if gap is None:
        return None
    if gap <= 0:
        return ("Under market", "badge-under")
    if gap < 0.15:
        return ("Near market", "badge-near")
    return ("Over market", "badge-over")


def _annotate_with_gap(listings: list[dict], ref_median: float | None) -> list[dict]:
    """Attach `gap` (float | None) and `badge` ((label, class) | None) to each
    listing dict so the template can render the "vs Market" column without
    re-running threshold logic per row. Pure: no DB calls."""
    for l in listings:
        gap = _gap_fraction(float(l["price"]), ref_median)
        l["gap"] = gap
        l["badge"] = _gap_to_badge(gap)
    return listings


def _run_browser_send(message_id: int) -> None:
    """BackgroundTask: drive the browser to place a BO + wait for the user's
    final click on eBay's UI. Updates the message row's status based on the
    outcome — dashboard polling picks up the change automatically.

    State transitions:
      * Success  → set_message_sent (status='sent', bumps rounds, flips
                   negotiation to 'awaiting_seller').
      * Timeout  → revert message status='approved' so the user can retry.
      * Browser error → same revert + log error.
      * Session expired → same revert + log; dashboard surfaces a re-login
                   notice once we wire that branch (TODO future).

    Module-level (not a closure) so tests can monkey-patch it cleanly.
    """
    msg = repo.get_message(message_id)
    if msg is None:
        log.error("_run_browser_send: message %s not found", message_id)
        return
    negotiation = repo.get_negotiation(msg["negotiation_id"])
    if negotiation is None:
        log.error("_run_browser_send: negotiation for message %s not found", message_id)
        return
    listing = repo.get_listing(negotiation["listing_id"])
    if listing is None:
        log.error("_run_browser_send: listing for message %s not found", message_id)
        return

    try:
        result = ebay_browser.place_best_offer_via_browser(
            listing_url=listing["url"],
            amount=float(msg["offer_amount"]),
            message=msg["body"],
        )
    except (EbaySessionExpiredError, EbayBrowserError) as exc:
        log.warning("browser send failed for message %s: %s", message_id, exc)
        # Revert to 'approved' so the user can retry. A future iteration could
        # surface the screenshot path via a flash-message-style column.
        with repo.get_conn() as conn:
            conn.execute(
                "UPDATE messages SET status = 'approved' WHERE id = ? AND status = 'sending'",
                (message_id,),
            )
        return

    if result.success:
        repo.set_message_sent(message_id)  # bumps rounds + flips to awaiting_seller
        # Don't touch the search status here; the polling re-render reads it.
    else:
        log.info("browser send returned non-success for message %s: %s", message_id, result.error_message)
        with repo.get_conn() as conn:
            conn.execute(
                "UPDATE messages SET status = 'approved' WHERE id = ? AND status = 'sending'",
                (message_id,),
            )

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


def _run_graph(search_id: int) -> None:
    """Background-task entry point. Kept module-level (not a closure) so the
    function is easily mockable in tests."""
    build_graph().invoke({"search_id": search_id})


def _run_negotiate_graph(search_id: int, listing_id: int) -> None:
    """Background-task entry point for the negotiate subgraph. Fires on listing
    selection. Same module-level pattern so tests can mock it cleanly."""
    build_negotiate_graph().invoke({"search_id": search_id, "listing_id": listing_id})


def _run_counter_graph(search_id: int, listing_id: int, negotiation_id: int) -> None:
    """Background-task entry point for the counter draft subgraph. Fires when a
    seller reply lands (via check-replies or paste-fallback)."""
    build_counter_graph().invoke({
        "search_id": search_id,
        "listing_id": listing_id,
        "negotiation_id": negotiation_id,
    })


@router.post("/searches")
def submit_search(
    background_tasks: BackgroundTasks,
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
    # Kick off the graph as a background task and redirect immediately. The
    # detail page polls /searches/{id}/content until status flips, so the user
    # sees the discovering state right away instead of staring at a blank
    # browser tab for 30-60s while Apify runs.
    background_tasks.add_task(_run_graph, search_id)
    return RedirectResponse(url=f"/searches/{search_id}", status_code=303)


@router.get("/searches/{search_id}", response_class=HTMLResponse)
def search_detail(request: Request, search_id: int) -> HTMLResponse:
    row = repo.get_search(search_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"search {search_id} not found")
    return templates.TemplateResponse(
        request,
        "search_detail.html",
        _content_context(search_id, row),
    )


@router.get("/searches/{search_id}/content", response_class=HTMLResponse)
def search_content(request: Request, search_id: int) -> HTMLResponse:
    """Polling target. Returns just the _search_content.html fragment so HTMX
    can swap it in without re-rendering the whole detail page."""
    row = repo.get_search(search_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"search {search_id} not found")
    return templates.TemplateResponse(
        request,
        "_search_content.html",
        _content_context(search_id, row),
    )


def _content_context(search_id: int, row: dict) -> dict:
    """Compute everything the search-content template needs to render any state.

    `negotiation`, `messages`, and `last_seller_msg` are populated only when a
    listing has been selected — they let the template render the message thread
    + state-dependent action buttons (Send / Check / Counter / Walk-away / Deal).
    """
    # Pull the single representative ref_median used to rank listings + drive
    # the "vs Market" badge. None means we skip ranking + badges and fall back
    # to the repo's natural price-asc order.
    ref_median = repo.get_aggregated_ref_median(search_id)
    listings = _annotate_with_gap(repo.list_listings(search_id), ref_median)
    if ref_median is not None:
        # Sort by gap ascending — most-under-market first. PRD §98.
        listings.sort(key=lambda l: (l["gap"] is None, l["gap"]))

    selected = repo.get_selected_listing(search_id)
    if selected is not None:
        # Same badge on the selected-listing card. repo returns a plain dict
        # already, so we can attach the keys in place.
        selected["gap"] = _gap_fraction(float(selected["price"]), ref_median)
        selected["badge"] = _gap_to_badge(selected["gap"])

    negotiation = None
    messages: list[dict] = []
    last_seller_msg: dict | None = None
    has_unsent_pending = False
    can_counter = False
    is_drafting = False
    is_browser_sending = False
    pending_message = repo.get_pending_message_for_search(search_id)
    if selected:
        negotiation = repo.get_active_negotiation_for_listing(selected["id"])
        if negotiation:
            messages = repo.get_messages_by_negotiation(negotiation["id"])
            for m in reversed(messages):
                if m["role"] == "seller":
                    last_seller_msg = m
                    break
            has_unsent_pending = any(
                m["role"] == "agent" and m["status"] == "approved" for m in messages
            )
            # "browser sending" — Playwright is mid-task. Drives the in-flight
            # UI showing "Browser opened — finish on eBay" and keeps polling
            # active until the task flips status to 'sent' (or back to 'approved'
            # on retry-able failure).
            is_browser_sending = any(
                m["role"] == "agent" and m["status"] == "sending" for m in messages
            )
            if last_seller_msg and not has_unsent_pending and not is_browser_sending and negotiation["rounds"] < MAX_ROUNDS:
                can_counter = True
            has_any_agent_msg = any(m["role"] == "agent" for m in messages)
            is_drafting = (not pending_message) and (not is_browser_sending) and (
                not has_any_agent_msg or (can_counter and last_seller_msg is not None)
            )
    return {
        "search": row,
        "listings": listings,
        "selected": selected,
        "reference_prices": repo.list_reference_prices(search_id),
        "pending_message": pending_message,
        "negotiation": negotiation,
        "messages": messages,
        "last_seller_msg": last_seller_msg,
        "has_unsent_pending": has_unsent_pending,
        "can_counter": can_counter,
        "is_drafting": is_drafting,
        "is_browser_sending": is_browser_sending,
        "max_rounds": MAX_ROUNDS,
        # Cost summary line: Apify scraping + Claude drafting + total. PRD §253.
        "costs": repo.sum_total_cost(search_id),
    }


@router.post("/searches/{search_id}/listings/{listing_id}/select", response_class=HTMLResponse)
def select_listing(
    request: Request,
    search_id: int,
    listing_id: int,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    """Mark a listing for negotiation. Idempotent on the same listing; conflicts
    if a *different* listing for the same search is already selected (one pick
    per search — see decision in 2026-06 selection-flow design).

    Side effect: kicks off the negotiate graph as a BackgroundTask the first
    time. The detail page's polling loop will swap in the drafted message
    when it's ready (~5s for Sonnet 4.6)."""
    listing = repo.get_listing(listing_id)
    if listing is None or listing["search_id"] != search_id:
        raise HTTPException(status_code=404, detail="listing not found for this search")

    existing = repo.get_selected_listing(search_id)
    if existing is not None and existing["id"] != listing_id:
        raise HTTPException(
            status_code=409,
            detail=f"another listing ({existing['id']}) is already selected for this search",
        )

    repo.mark_listing_selected(listing_id)
    if existing is None:
        repo.update_search_status(search_id, "negotiating")
        # Only fire the negotiate graph the first time — re-selects shouldn't
        # spawn duplicate Claude calls (and the chooser is idempotent anyway).
        background_tasks.add_task(_run_negotiate_graph, search_id, listing_id)

    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/listings/{listing_id}/deselect", response_class=HTMLResponse)
def deselect_listing(request: Request, search_id: int, listing_id: int) -> HTMLResponse:
    """Undo a selection. Listing returns to the unselected pool, search status
    drops back to awaiting_selection so the user can pick again. Idempotent."""
    listing = repo.get_listing(listing_id)
    if listing is None or listing["search_id"] != search_id:
        raise HTTPException(status_code=404, detail="listing not found for this search")

    repo.unmark_listing_selected(listing_id)

    # Cascade any in-progress negotiation for this listing: mark it walked_away
    # and its pending agent message rejected. Without this, the next select on
    # a different listing would still see the old pending message in the UI.
    active = repo.get_active_negotiation_for_listing(listing_id)
    if active is not None:
        repo.update_negotiation_status(active["id"], "walked_away")
        pending = repo.get_pending_message_for_search(search_id)
        if pending is not None:
            repo.reject_message(pending["id"])

    # If nothing's selected now, drop status back. Guard against future statuses
    # that have moved past 'negotiating' (e.g. awaiting_send) so we don't
    # silently rewind a search that's done real work.
    if repo.get_selected_listing(search_id) is None:
        current_status = repo.get_search(search_id)["status"]
        if current_status in ("negotiating", "awaiting_send"):
            repo.update_search_status(search_id, "awaiting_selection")

    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/messages/{message_id}/approve", response_class=HTMLResponse)
def approve_message(
    request: Request,
    search_id: int,
    message_id: int,
    body: str = Form(""),
    offer_amount: str = Form(""),
) -> HTMLResponse:
    """Approve a pending agent message, optionally with an edited body and/or
    offer amount. Both fields come from inputs on the approval card; either may
    be unchanged.

    The offer_amount edit matters for the Best Offer path — PlaceOffer reads
    the persisted offer_amount directly, so a user tweak here is authoritative.
    Hard-capped at search.max_price (PRD §187).

    Flips the search to status='awaiting_send' so the Send button activates."""
    msg = repo.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    pending = repo.get_pending_message_for_search(search_id)
    if pending is None or pending["id"] != message_id:
        raise HTTPException(status_code=404, detail="no pending message for this search")

    new_body = body.strip() or None
    if new_body is not None and len(new_body) < 20:
        raise HTTPException(status_code=400, detail="message body too short (minimum 20 characters)")

    new_offer_amount: float | None = None
    if offer_amount.strip():
        try:
            new_offer_amount = float(offer_amount)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="offer_amount must be a number") from exc
        if new_offer_amount <= 0:
            raise HTTPException(status_code=400, detail="offer_amount must be positive")
        search = repo.get_search(search_id)
        # PRD §187: max_price is enforced by code, never by the LLM. Same rule
        # applies to user edits — fat-finger guard.
        if new_offer_amount > search["max_price"]:
            raise HTTPException(
                status_code=400,
                detail=f"offer_amount ${new_offer_amount:.2f} exceeds max_price ${search['max_price']:.2f}",
            )

    repo.approve_message(message_id, new_body=new_body, new_offer_amount=new_offer_amount)
    repo.update_search_status(search_id, "awaiting_send")
    return _render_search_content(request, search_id)


def _render_search_content(request: Request, search_id: int) -> HTMLResponse:
    """Re-render the HTMX swap target after a selection-state change."""
    row = repo.get_search(search_id)
    return templates.TemplateResponse(request, "_search_content.html", _content_context(search_id, row))


# ---- Round-by-round multi-message negotiation ------------------------------


def _require_listing_in_search(search_id: int, listing_id: int) -> dict:
    """Common 404 guard for all per-listing actions. Prevents stale URLs from
    one search from mutating another search's listing."""
    listing = repo.get_listing(listing_id)
    if listing is None or listing["search_id"] != search_id:
        raise HTTPException(status_code=404, detail="listing not found for this search")
    return listing


@router.post("/searches/{search_id}/messages/{message_id}/send", response_class=HTMLResponse)
def send_message(
    request: Request,
    search_id: int,
    message_id: int,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    """Send the approved message to the seller via eBay's Trading API.

    Idempotent: if the message has already been sent, returns the current page
    state without re-sending. The XML transport is finicky enough that we
    surface errors as 502s so the UI can show a copy-paste fallback rather
    than silently failing.
    """
    msg = repo.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    # Defense-in-depth: belongs to this search?
    negotiation = repo.get_negotiation(msg["negotiation_id"])
    listing = _require_listing_in_search(search_id, negotiation["listing_id"])

    if msg["status"] == "sent":
        # Already sent; show current state. Idempotent retry path.
        return _render_search_content(request, search_id)
    if msg["status"] != "approved":
        raise HTTPException(status_code=409, detail=f"message status is {msg['status']}, not approved")

    # Pre-flight: figure out which send path applies to this message.
    #   * BO-enabled listing → Playwright browser automation (Option 2). The
    #     route fires a BackgroundTask that opens Chrome, fills the form,
    #     navigates to eBay's review page, and waits up to 5 min for the user
    #     to click "Send Offer" on eBay's UI. Confirmation triggers
    #     set_message_sent in the task; failure reverts to 'approved' for retry.
    #     Eligibility for PlaceOffer-via-API is blocked for this account type;
    #     see memory `feedback_ebay_trading_gotchas.md` §3.
    #   * non-BO listing → AAQ (works only if a partner relationship exists).
    bo_eligible = _listing_accepts_best_offer(listing)
    offer_amount = msg["offer_amount"]

    if bo_eligible:
        if offer_amount is None or offer_amount <= 0:
            raise HTTPException(
                status_code=409,
                detail="message has no offer_amount; re-approve with a price",
            )
        # Flip status to 'sending' BEFORE returning. The dashboard polling
        # picks up this state and renders the "browser opening" indicator.
        # The BackgroundTask either flips to 'sent' or back to 'approved'.
        with repo.get_conn() as conn:
            conn.execute(
                "UPDATE messages SET status = 'sending' WHERE id = ? AND status = 'approved'",
                (message_id,),
            )
        background_tasks.add_task(_run_browser_send, message_id)
        return _render_search_content(request, search_id)

    # Non-BO listing → existing AAQ flow.
    try:
        if not listing.get("seller_id"):
            raise HTTPException(
                status_code=409,
                detail="listing has no seller_id on record; cannot send (try copy-paste fallback)",
            )
        ebay_trading.send_member_message(
            listing["ebay_item_id"], listing["seller_id"], msg["body"]
        )
    except NoPartnerRelationshipError as exc:
        log.info("listing requires manual contact (no partner relationship): %s", exc)
        row = repo.get_search(search_id)
        ctx = _content_context(search_id, row)
        ctx["send_blocked"] = "no_partner"
        return templates.TemplateResponse(request, "_search_content.html", ctx)
    except EbayTradingError as exc:
        log.warning("eBay send failed for message %s: %s", message_id, exc)
        raise HTTPException(status_code=502, detail=f"eBay send failed: {exc}") from exc

    repo.set_message_sent(message_id)  # also bumps rounds + flips negotiation to awaiting_seller
    repo.update_search_status(search_id, "negotiating")
    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/messages/{message_id}/mark-sent", response_class=HTMLResponse)
def mark_message_sent(request: Request, search_id: int, message_id: int) -> HTMLResponse:
    """User confirms they manually sent the message via eBay's UI (typically
    because the listing rejected automated send). Same downstream bookkeeping
    as a successful API send — bumps rounds, flips to awaiting_seller — just
    without the eBay API call."""
    msg = repo.get_message(message_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="message not found")
    negotiation = repo.get_negotiation(msg["negotiation_id"])
    _require_listing_in_search(search_id, negotiation["listing_id"])

    if msg["status"] == "sent":
        return _render_search_content(request, search_id)  # idempotent
    if msg["status"] != "approved":
        raise HTTPException(status_code=409, detail=f"message status is {msg['status']}, not approved")

    repo.set_message_sent(message_id)
    repo.update_search_status(search_id, "negotiating")
    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/listings/{listing_id}/check-replies", response_class=HTMLResponse)
def check_replies(
    request: Request,
    search_id: int,
    listing_id: int,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    """Poll eBay for any new seller messages on this listing. Manual button —
    same work the background poller (`agents.poller`) does on a 5-min cadence,
    surfaced for the user when they want an immediate check rather than waiting.

    Delegates the fetch / dedup / counter-fire decision to the shared helper
    so both call sites stay aligned. The route layer adds: a 409 for "no
    active negotiation" and a 502 for eBay-side failures."""
    listing = _require_listing_in_search(search_id, listing_id)
    negotiation = repo.get_active_negotiation_for_listing(listing_id)
    if negotiation is None:
        raise HTTPException(status_code=409, detail="no active negotiation for this listing")

    try:
        should_counter = process_seller_replies_for_listing(
            search_id, listing_id, negotiation["id"],
        )
    except EbayTradingError as exc:
        log.warning("eBay check-replies failed for listing %s: %s", listing_id, exc)
        raise HTTPException(status_code=502, detail=f"eBay check failed: {exc}") from exc

    if should_counter:
        background_tasks.add_task(_run_counter_graph, search_id, listing_id, negotiation["id"])

    return _render_search_content(request, search_id)


@router.post(
    "/searches/{search_id}/listings/{listing_id}/check-offer-status",
    response_class=HTMLResponse,
)
def check_offer_status(
    request: Request,
    search_id: int,
    listing_id: int,
    background_tasks: BackgroundTasks,
) -> HTMLResponse:
    """Poll eBay's GetBestOffers for the active negotiation. Mirrors check-replies
    in spirit (manual button drives a poll → state update → optional counter draft),
    but reads the structured BO channel rather than free-form AAQ inbox.

    Status handling:
      * Pending  — no-op; user clicks again later.
      * Accepted — seller agreed at our offer; mark deal at offer_amount.
      * Declined — seller said no; mark walk_away.
      * Countered — persist seller's counter as a seller message (body=seller_message,
                    offer_amount=counter_amount) and fire the counter-draft graph
                    so the agent's next move shows up in the approval queue.
      * Unknown  — the BO id couldn't be located; no-op. Surfaces as Pending in UI.
    """
    listing = _require_listing_in_search(search_id, listing_id)
    negotiation = repo.get_active_negotiation_for_listing(listing_id)
    if negotiation is None:
        raise HTTPException(status_code=409, detail="no active negotiation for this listing")
    offer_id = negotiation.get("ebay_best_offer_id")
    if not offer_id:
        raise HTTPException(
            status_code=409,
            detail="no Best Offer has been placed for this negotiation yet",
        )

    try:
        status = ebay_trading.get_best_offer_status(listing["ebay_item_id"], offer_id)
    except EbayTradingError as exc:
        log.warning("GetBestOffers failed for offer %s: %s", offer_id, exc)
        raise HTTPException(status_code=502, detail=f"eBay check failed: {exc}") from exc

    state = status["status"]
    if state == "Accepted":
        # Find the agent's last sent offer_amount as the agreed price — that's
        # the value we placed, and Accepted means the seller took it.
        last_agent = next(
            (m for m in reversed(repo.get_messages_by_negotiation(negotiation["id"]))
             if m["role"] == "agent" and m["status"] == "sent"),
            None,
        )
        agreed_price = (last_agent or {}).get("offer_amount") or 0.0
        repo.mark_negotiation_deal(negotiation["id"], final_price=float(agreed_price))
        repo.update_search_status(search_id, "done")
    elif state == "Declined":
        repo.update_negotiation_status(negotiation["id"], "walked_away")
        repo.update_search_status(search_id, "done")
    elif state == "Countered":
        # Persist the counter as a seller message so the conversation thread
        # reads naturally. Same dedup contract as check-replies: skip if the
        # exact body is already there (e.g. user clicked check twice).
        seller_body = status.get("seller_message") or f"Counter offer: ${status['counter_amount']:.2f}"
        existing_bodies = {m["body"] for m in repo.get_messages_by_negotiation(negotiation["id"])}
        if seller_body not in existing_bodies:
            repo.add_message(
                negotiation["id"],
                role="seller",
                body=seller_body,
                offer_amount=status["counter_amount"],
                status="received",
            )
            if negotiation["rounds"] < MAX_ROUNDS:
                repo.update_negotiation_status(negotiation["id"], "open")
                background_tasks.add_task(
                    _run_counter_graph, search_id, listing_id, negotiation["id"]
                )
    # Pending / Unknown / Expired / Retracted → no-op. Template already shows
    # the waiting state; user can poll again or walk away manually.

    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/listings/{listing_id}/paste-reply", response_class=HTMLResponse)
def paste_reply(
    request: Request,
    search_id: int,
    listing_id: int,
    background_tasks: BackgroundTasks,
    body: str = Form(""),
) -> HTMLResponse:
    """Manual fallback: user pastes the seller's response text instead of
    calling the eBay API. Same downstream effect — persist as seller message,
    fire counter draft."""
    _require_listing_in_search(search_id, listing_id)
    body = body.strip()
    if len(body) < 5:
        raise HTTPException(status_code=400, detail="reply text too short to be meaningful")

    negotiation = repo.get_active_negotiation_for_listing(listing_id)
    if negotiation is None:
        raise HTTPException(status_code=409, detail="no active negotiation for this listing")

    repo.add_message(negotiation["id"], role="seller", body=body, offer_amount=None, status="received")

    if negotiation["rounds"] < MAX_ROUNDS:
        repo.update_negotiation_status(negotiation["id"], "open")
        background_tasks.add_task(_run_counter_graph, search_id, listing_id, negotiation["id"])

    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/listings/{listing_id}/walk-away", response_class=HTMLResponse)
def walk_away(request: Request, search_id: int, listing_id: int) -> HTMLResponse:
    """Terminal: mark the negotiation walked_away. Search → done."""
    _require_listing_in_search(search_id, listing_id)
    negotiation = repo.get_active_negotiation_for_listing(listing_id)
    if negotiation is None:
        raise HTTPException(status_code=409, detail="no active negotiation for this listing")
    repo.update_negotiation_status(negotiation["id"], "walked_away")
    # Any still-pending agent draft gets rejected so it doesn't show up as actionable.
    pending = repo.get_pending_message_for_search(search_id)
    if pending is not None:
        repo.reject_message(pending["id"])
    repo.update_search_status(search_id, "done")
    return _render_search_content(request, search_id)


@router.post("/searches/{search_id}/listings/{listing_id}/deal", response_class=HTMLResponse)
def mark_deal(
    request: Request,
    search_id: int,
    listing_id: int,
    final_price: str = Form(""),
) -> HTMLResponse:
    """Terminal: mark the negotiation as a closed deal at `final_price`.
    Search → done. The final_price comes from a free-form input because it
    often differs from any value previously offered (seller counters with a
    new number, you accept; or you split the difference verbally)."""
    _require_listing_in_search(search_id, listing_id)
    try:
        price = float(final_price)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="final_price must be a number") from exc
    if price <= 0:
        raise HTTPException(status_code=400, detail="final_price must be positive")

    search = repo.get_search(search_id)
    if price > search["max_price"]:
        # Last-line guard: even on deal entry, don't let a fat-fingered price
        # exceed the buyer's stated ceiling. PRD §181.
        raise HTTPException(
            status_code=400,
            detail=f"final_price ${price:.2f} exceeds buyer's max_price ${search['max_price']:.2f}",
        )

    negotiation = repo.get_active_negotiation_for_listing(listing_id)
    if negotiation is None:
        raise HTTPException(status_code=409, detail="no active negotiation for this listing")
    repo.mark_negotiation_deal(negotiation["id"], final_price=price)
    repo.update_search_status(search_id, "done")
    return _render_search_content(request, search_id)
