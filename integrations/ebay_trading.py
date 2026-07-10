"""eBay Trading API (XML) integration — buyer-side messaging only.

The Trading API is eBay's older XML API. The newer REST APIs (Browse, Sell)
don't expose `AddMemberMessageAAQToPartner` or `GetMyMessages` — buyer-to-seller
messaging only lives on the Trading API for now.

Two operations we need:
  * AddMemberMessageAAQToPartner — send a message to the seller of an item
  * GetMyMessages — pull the user's inbox (we filter by item_id client-side)

All calls go through one HTTP endpoint with headers selecting the operation.
Auth uses the long-lived Auth'n'Auth token in EBAY_USER_TOKEN.
"""

import logging
import re
from typing import Any
from xml.etree import ElementTree as ET

import defusedxml.ElementTree as defused_ET
import requests

import config


log = logging.getLogger(__name__)


_ENDPOINT = "https://api.ebay.com/ws/api.dll"
_COMPATIBILITY_LEVEL = "1199"  # Stable Trading API version; rarely needs bumping.
_SITE_ID = "0"                  # eBay US

# Trading API XML responses are namespaced. ElementTree's "find" needs the URL
# inline, so we just strip the namespace on parse — simpler than the alternative.
_NS_RE = re.compile(r"\{[^}]+\}")


class EbayTradingError(RuntimeError):
    """Raised for transport failures, eBay API errors, or unexpected XML shapes."""


class BestOfferNotSupportedError(EbayTradingError):
    """Raised when PlaceOffer rejects with 'not eligible for Best Offer'.

    The send-route catches this specifically so the UI can fall back to AAQ
    (which only works if a prior transaction relationship exists) or the
    manual copy-paste flow. Pre-flight `buyingOptions` checks should normally
    prevent this from firing, but eBay's listing state can change between
    Browse-API fetch and send.
    """


class NoPartnerRelationshipError(EbayTradingError):
    """Raised specifically when AddMemberMessageAAQToPartner rejects with the
    'sender or recipient is not the partner of the transaction' error.

    This means the buyer has no transaction relationship with the seller —
    no prior purchase, no completed Best Offer, no won bid. Pre-purchase
    cold-contact via Trading API is not possible for these listings; the user
    must copy-paste manually or use the future Best Offer flow.

    Caught separately by the route so the UI can guide the user to copy-paste
    instead of showing a generic 502.
    """


def _legacy_item_id(item_id: str) -> str:
    """Browse API returns IDs as `v1|<numeric>|<variant>` (e.g. `v1|256460558255|0`).
    Trading API requires the bare numeric portion. This helper accepts either
    format and returns the numeric ID.

    Raises EbayTradingError if no numeric segment can be extracted — callers
    shouldn't be sending garbage, but a clear error beats eBay's vague rejection.
    """
    if not item_id:
        raise EbayTradingError("empty item_id")
    # Already plain numeric?
    if item_id.isdigit():
        return item_id
    # Versioned format: take the middle segment between pipes.
    parts = item_id.split("|")
    for p in parts:
        if p.isdigit():
            return p
    raise EbayTradingError(f"could not extract numeric item id from {item_id!r}")


def _is_oauth_user_token(token: str) -> bool:
    """OAuth User Tokens have a structured prefix; legacy Auth'n'Auth tokens
    are flat opaque strings. The prefix `v^N.N#` is the documented marker."""
    return token.startswith("v^") and "#" in token


def _build_headers(call_name: str) -> dict[str, str]:
    h = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": _COMPATIBILITY_LEVEL,
        "X-EBAY-API-DEV-NAME": config.EBAY_DEV_ID or "",
        "X-EBAY-API-APP-NAME": config.EBAY_APP_ID or "",
        "X-EBAY-API-CERT-NAME": config.EBAY_CERT_ID or "",
        "X-EBAY-API-CALL-NAME": call_name,
        "X-EBAY-API-SITEID": _SITE_ID,
        "Content-Type": "text/xml; charset=utf-8",
    }
    token = config.EBAY_USER_TOKEN or ""
    if _is_oauth_user_token(token):
        # OAuth user tokens go in the IAF header; the XML body's
        # RequesterCredentials block is omitted in this auth mode.
        h["X-EBAY-API-IAF-TOKEN"] = token
    return h


def _wrap_request(call_name: str, inner_xml: str) -> str:
    """eBay Trading wraps every call in <{CallName}Request>. Auth path depends
    on token type: OAuth user tokens use the IAF header (no body credentials);
    legacy Auth'n'Auth tokens go in <RequesterCredentials><eBayAuthToken>...</."""
    token = config.EBAY_USER_TOKEN or ""
    credentials_xml = (
        ""  # OAuth path puts the token in the IAF header instead
        if _is_oauth_user_token(token)
        else f'<RequesterCredentials><eBayAuthToken>{token}</eBayAuthToken></RequesterCredentials>'
    )
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        f'<{call_name}Request xmlns="urn:ebay:apis:eBLBaseComponents">'
        f'{credentials_xml}'
        f'{inner_xml}'
        f'</{call_name}Request>'
    )


def _strip_ns(elem: ET.Element) -> ET.Element:
    """Remove the urn:ebay:apis:eBLBaseComponents namespace from every tag so
    `.find('Foo/Bar')` works without ugly Clark-notation strings."""
    for el in elem.iter():
        el.tag = _NS_RE.sub("", el.tag)
    return elem


def _post(call_name: str, inner_xml: str) -> ET.Element:
    """POST a Trading call. Raises EbayTradingError on transport failure, on
    eBay-level Errors (Ack != Success/Warning), or on non-2xx HTTP."""
    if not config.EBAY_USER_TOKEN:
        raise EbayTradingError("EBAY_USER_TOKEN is not set in .env")

    payload = _wrap_request(call_name, inner_xml)
    try:
        resp = requests.post(_ENDPOINT, headers=_build_headers(call_name), data=payload.encode("utf-8"), timeout=30)
    except requests.RequestException as exc:
        raise EbayTradingError(f"transport failure calling {call_name}: {exc}") from exc

    if resp.status_code != 200:
        raise EbayTradingError(f"{call_name} HTTP {resp.status_code}: {resp.text[:500]}")

    try:
        # defusedxml hardens against XXE/billion-laughs/etc. Trading API responses
        # are technically trusted (HTTPS + auth) but defusing is near-free and
        # eliminates a class of bug entirely.
        root = _strip_ns(defused_ET.fromstring(resp.content))
    except ET.ParseError as exc:
        raise EbayTradingError(f"{call_name} XML parse failed: {exc}; body={resp.text[:300]}") from exc

    ack = (root.findtext("Ack") or "").strip()
    if ack in ("Failure", "PartialFailure"):
        # Surface the first Error's message; eBay returns LongMessage + ShortMessage.
        msg = root.findtext("Errors/LongMessage") or root.findtext("Errors/ShortMessage") or "(no error message)"
        # Log every Error element + ErrorCode for cases where the LongMessage is
        # misleading (e.g. "Offer.Action invalid" is what eBay returns for many
        # unrelated PlaceOffer problems — multi-variation listings, seller
        # auto-decline thresholds, etc). Without this we can't tell which
        # listing-specific cause fired.
        all_errors = []
        for err in root.findall("Errors"):
            code = err.findtext("ErrorCode") or ""
            short = err.findtext("ShortMessage") or ""
            long = err.findtext("LongMessage") or ""
            params = "; ".join(
                f"{p.findtext('Value') or ''}" for p in err.findall("ErrorParameters")
            )
            all_errors.append(f"[code={code}] {short} — {long}" + (f" (params: {params})" if params else ""))
        log.warning("%s %s — all errors: %s", call_name, ack, " || ".join(all_errors) or "(none parsed)")
        # Recognize the specific "no transaction relationship" failure so the
        # route layer can route this to the copy-paste fallback UI instead of
        # surfacing a generic 502. Pattern-matching on the error text is
        # brittle but eBay doesn't expose a structured error code we can rely
        # on for this class of failure.
        if "not the partner" in msg.lower():
            raise NoPartnerRelationshipError(f"{call_name} returned {ack}: {msg}")
        # PlaceOffer-specific: listing doesn't accept Best Offer. The phrasing
        # eBay uses is "not eligible for Best Offer" / "Best Offer is not enabled".
        msg_lower = msg.lower()
        if "best offer" in msg_lower and ("not eligible" in msg_lower or "not enabled" in msg_lower):
            raise BestOfferNotSupportedError(f"{call_name} returned {ack}: {msg}")
        raise EbayTradingError(f"{call_name} returned {ack}: {msg}")
    return root


# ---- Public API ----------------------------------------------------------

def send_member_message(item_id: str, recipient_id: str, body: str) -> None:
    """Send a buyer-to-seller message about a specific listing.

    Uses the AAQToPartner ("Ask A Question To Partner") flow, which is the
    appropriate channel for a buyer asking about / negotiating on an item.
    Body becomes the message text; eBay sets the subject automatically based on
    the listing. Raises EbayTradingError on failure.

    `recipient_id` is the seller's eBay username (stored as `listings.seller_id`).
    eBay does NOT auto-derive it from ItemID — it must be passed explicitly
    or the call fails with "Recipient User Id is missing."
    """
    if not item_id:
        raise EbayTradingError("item_id required")
    if not recipient_id:
        raise EbayTradingError("recipient_id (seller username) required")
    if not body or len(body) < 20:
        raise EbayTradingError("message body too short (eBay requires meaningful content)")

    # Trading API needs the bare-numeric form, not Browse's v1|...|0 format.
    numeric_id = _legacy_item_id(item_id)

    # XML escape body + recipient. Usernames can't contain special chars in
    # practice, but defensive escaping is free.
    def _esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    inner = (
        f"<ItemID>{numeric_id}</ItemID>"
        f"<MemberMessage>"
        f"<Body>{_esc(body)}</Body>"
        f"<QuestionType>General</QuestionType>"
        f"<RecipientID>{_esc(recipient_id)}</RecipientID>"
        f"</MemberMessage>"
    )
    _post("AddMemberMessageAAQToPartner", inner)


def get_messages_for_item(item_id: str, max_messages: int = 25) -> list[dict[str, Any]]:
    """Fetch recent messages from the user's eBay inbox and return only those
    associated with `item_id`. Trading's GetMyMessages doesn't support a
    per-item filter at request time, so we pull a window and filter locally.

    Returns a list of {message_id, sender, body, received_at, item_id} dicts,
    oldest-first. Empty list if no matches.
    """
    if not item_id:
        raise EbayTradingError("item_id required")

    # Normalize the buyer-side ID to numeric so it matches what eBay returns
    # in the <ItemID> tags of GetMyMessages (which are always numeric).
    numeric_id = _legacy_item_id(item_id)

    # Step 1: list the most recent messages (header info only — no bodies)
    summary_inner = (
        "<DetailLevel>ReturnHeaders</DetailLevel>"
        f"<Pagination><EntriesPerPage>{max_messages}</EntriesPerPage><PageNumber>1</PageNumber></Pagination>"
    )
    root = _post("GetMyMessages", summary_inner)

    # Find IDs of messages tied to our item
    matching_ids: list[str] = []
    for msg in root.findall("Messages/Message"):
        msg_item = msg.findtext("ItemID") or ""
        if msg_item == numeric_id:
            mid = msg.findtext("MessageID")
            if mid:
                matching_ids.append(mid)

    if not matching_ids:
        return []

    # Step 2: fetch full bodies for just those IDs (DetailLevel=ReturnMessages)
    ids_xml = "".join(f"<MessageID>{m}</MessageID>" for m in matching_ids)
    detail_inner = (
        "<DetailLevel>ReturnMessages</DetailLevel>"
        f"<MessageIDs>{ids_xml}</MessageIDs>"
    )
    detail_root = _post("GetMyMessages", detail_inner)

    results: list[dict[str, Any]] = []
    for msg in detail_root.findall("Messages/Message"):
        results.append({
            "message_id": msg.findtext("MessageID") or "",
            "sender": msg.findtext("Sender") or "",
            "body": msg.findtext("Text") or msg.findtext("Content") or "",
            "received_at": msg.findtext("ReceiveDate") or "",
            "item_id": msg.findtext("ItemID") or "",
        })

    # Sort oldest-first by received_at so callers can persist them in order
    results.sort(key=lambda m: m["received_at"])
    return results


# ---- Best Offer (PlaceOffer / GetBestOffers / RespondToBestOffer) --------
#
# These three calls cover the structured Best-Offer round-trip:
#   * place_best_offer       — buyer's opening Best Offer on a BO-enabled listing
#   * get_best_offer_status  — poll the seller's response (accept/decline/counter)
#   * respond_to_best_offer  — buyer's reply to a seller counter (Accept/Decline/Counter)
#
# Unlike AAQ messaging, PlaceOffer does NOT require a prior buyer/seller
# transaction relationship — it CREATES the relationship, which then unlocks
# AAQ for any subsequent free-form messaging. That's why this is the right
# pre-purchase contact channel for the agent's actual use case.


def _esc(s: str) -> str:
    """Minimal XML escape for body / message text. Same helper as send_member_message."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def place_best_offer(item_id: str, amount: float, message: str) -> dict[str, Any]:
    """Submit a Best Offer for `amount` on `item_id`. Returns
    `{"offer_id": str, "status": str}` where status is typically 'Pending'
    (seller hasn't responded) or 'Accepted' (auto-accept threshold met).

    `message` is accepted but NOT sent on round 1 — eBay's PlaceOffer schema
    rejects `<BuyerMessage>` inside `<Offer>` (XSD only allows Action / MaxBid /
    Quantity / BlockOnWarning). The strategic prose lands on round 2+ via
    `respond_to_best_offer`, which does accept a BuyerMessage. The signature
    keeps `message` for call-site symmetry with the other send paths.

    Raises BestOfferNotSupportedError if the listing doesn't accept BO; route
    layer uses that signal to fall back to AAQ + manual paste.
    """
    if not item_id:
        raise EbayTradingError("item_id required")
    if amount is None or amount <= 0:
        raise EbayTradingError(f"amount must be positive, got {amount!r}")

    numeric_id = _legacy_item_id(item_id)
    # Two decimals; eBay's price parser tolerates either form but logs are clearer
    # with the canonical 2-decimal representation and it matches CurrencyAmount type.
    amount_str = f"{float(amount):.2f}"

    if message:
        # Round-1 prose is dropped; log it so the user can re-send via AAQ post-relationship
        # or via the round-2 counter if the seller responds.
        log.info(
            "PlaceOffer dropping round-1 buyer message (eBay schema does not accept it on PlaceOffer): %.80s%s",
            message,
            "…" if len(message) > 80 else "",
        )

    # EndUserIP is required by eBay's PlaceOffer XSD. When it's missing, the
    # error returned is the misleading "Input data for tag <Offer.Action> is
    # invalid or missing" — eBay names the next required element instead of
    # the actually-missing one. A literal "0.0.0.0" is fine for server-side
    # callers; eBay accepts it and only logs it for fraud-pattern analysis.
    # BlockOnWarning=true converts Ack=Warning responses into Ack=Failure so
    # we don't silently accept partial / no-op offers. Without it, eBay can
    # return Success with placeholder data and the offer never actually lands.
    # CRITICAL: element order inside <Offer> is alphabetical and strict —
    # eBay's XSD uses xs:sequence for OfferType. Out-of-order elements trigger
    # an "Input data for tag <Offer.Action> is invalid" error that cascades
    # back to the first required field instead of naming the actual culprit.
    # Order: Action, BlockOnWarning, MaxBid, Quantity. Do not reorder.
    inner = (
        f"<EndUserIP>0.0.0.0</EndUserIP>"
        f"<ItemID>{numeric_id}</ItemID>"
        f"<Offer>"
        f"<Action>BestOffer</Action>"
        f"<BlockOnWarning>true</BlockOnWarning>"
        f'<MaxBid currencyID="USD">{amount_str}</MaxBid>'
        f"<Quantity>1</Quantity>"
        f"</Offer>"
    )
    root = _post("PlaceOffer", inner)

    offer_id = root.findtext("BestOffer/BestOfferID") or ""
    status = root.findtext("BestOffer/Status") or "Pending"
    # Log the full structured result so we can spot silent-success cases
    # (Ack=Success with offer_id but the offer never actually appeared on
    # the seller's side — has been observed for multi-variation listings).
    log.info("PlaceOffer for item %s: amount=$%s offer_id=%s status=%s", numeric_id, amount_str, offer_id, status)
    if not offer_id:
        raise EbayTradingError("PlaceOffer succeeded but no BestOfferID was returned")
    return {"offer_id": offer_id, "status": status}


def get_best_offer_status(item_id: str, offer_id: str) -> dict[str, Any]:
    """Look up the current state of a previously-placed Best Offer.

    Returns: {offer_id, status, counter_amount, seller_message}
        status ∈ {Pending, Accepted, Declined, Countered, Expired, Retracted, Unknown}
        counter_amount populated only when status == 'Countered'

    Returns status='Unknown' (not an exception) when the offer isn't in eBay's
    response array — stale polls shouldn't 502.
    """
    if not item_id:
        raise EbayTradingError("item_id required")
    if not offer_id:
        raise EbayTradingError("offer_id required")

    numeric_id = _legacy_item_id(item_id)
    # ItemID + BestOfferID together select a single offer; without ItemID, eBay
    # requires an additional filter and returns a paginated set.
    inner = f"<ItemID>{numeric_id}</ItemID><BestOfferID>{_esc(offer_id)}</BestOfferID>"
    root = _post("GetBestOffers", inner)

    for offer in root.findall("BestOfferArray/BestOffer"):
        if (offer.findtext("BestOfferID") or "") != offer_id:
            continue
        counter_text = offer.findtext("CounterOfferPrice")
        counter_amount = float(counter_text) if counter_text else None
        return {
            "offer_id": offer_id,
            "status": offer.findtext("Status") or "Unknown",
            "counter_amount": counter_amount,
            "seller_message": offer.findtext("SellerMessage") or None,
        }

    return {"offer_id": offer_id, "status": "Unknown", "counter_amount": None, "seller_message": None}


_VALID_RESPOND_ACTIONS = {"Accept", "Decline", "Counter"}


def respond_to_best_offer(
    item_id: str,
    offer_id: str,
    action: str,
    counter_amount: float | None,
    message: str,
) -> None:
    """Buyer's structured reply to a seller-side counter on a BO. `action` is
    Accept / Decline / Counter; `counter_amount` is required (and only meaningful)
    when action == 'Counter'.

    Keeps round 2+ inside the BO channel rather than falling to free-form AAQ —
    seller sees the negotiation as a single coherent offer thread on their side.
    """
    if not item_id:
        raise EbayTradingError("item_id required")
    if not offer_id:
        raise EbayTradingError("offer_id required")
    if action not in _VALID_RESPOND_ACTIONS:
        raise EbayTradingError(f"action must be one of {sorted(_VALID_RESPOND_ACTIONS)}, got {action!r}")
    if action == "Counter" and (counter_amount is None or counter_amount <= 0):
        raise EbayTradingError("Counter action requires a positive counter_amount")

    numeric_id = _legacy_item_id(item_id)

    parts = [
        f"<ItemID>{numeric_id}</ItemID>",
        f"<BestOfferID>{_esc(offer_id)}</BestOfferID>",
        f"<Action>{action}</Action>",
    ]
    if action == "Counter":
        parts.append(f'<CounterOfferPrice currencyID="USD">{float(counter_amount):.2f}</CounterOfferPrice>')
    if message:
        parts.append(f"<BuyerMessage>{_esc(message)}</BuyerMessage>")

    _post("RespondToBestOffer", "".join(parts))
