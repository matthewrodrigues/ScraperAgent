"""Tests for integrations.ebay_trading. Mocks requests.post so tests are hermetic.

The XML format is gnarly enough that pinning the exact request shapes catches
the most common breakage class: someone changes a field name and the call
silently starts erroring on eBay's end."""

from unittest.mock import MagicMock, patch

import pytest

import config
from integrations import ebay_trading


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(config, "EBAY_USER_TOKEN", "test-token")
    monkeypatch.setattr(config, "EBAY_APP_ID", "app")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "cert")
    monkeypatch.setattr(config, "EBAY_DEV_ID", "dev")


def _mock_response(xml_body: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.content = xml_body.encode("utf-8")
    resp.text = xml_body
    return resp


_SUCCESS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<AddMemberMessageAAQToPartnerResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Timestamp>2026-06-17T00:00:00.000Z</Timestamp>
  <Ack>Success</Ack>
  <Version>1199</Version>
  <Build>E1199_CORE_API_18760830_R1</Build>
</AddMemberMessageAAQToPartnerResponse>"""


def test_send_member_message_posts_correct_envelope(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_SUCCESS_RESPONSE)) as mock_post:
        # Pass the Browse-API versioned form to verify normalization happens here too
        ebay_trading.send_member_message("v1|123456789|0", "audiogear_pro", "Hello, is this still available at the listed price?")

    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.ebay.com/ws/api.dll"
    body = kwargs["data"].decode("utf-8")
    # ItemID must be the bare numeric form Trading API requires, not the v1|...|0 input
    assert "<ItemID>123456789</ItemID>" in body
    assert "v1|" not in body
    assert "<eBayAuthToken>test-token</eBayAuthToken>" in body
    assert "<QuestionType>General</QuestionType>" in body
    assert "<RecipientID>audiogear_pro</RecipientID>" in body
    assert "Hello, is this still available" in body
    # Headers carry the operation name + creds
    headers = kwargs["headers"]
    assert headers["X-EBAY-API-CALL-NAME"] == "AddMemberMessageAAQToPartner"
    assert headers["X-EBAY-API-APP-NAME"] == "app"
    assert headers["X-EBAY-API-DEV-NAME"] == "dev"
    assert headers["X-EBAY-API-CERT-NAME"] == "cert"


def test_send_message_xml_escapes_special_chars(configured):
    body_with_specials = "Price <fine> & shipping > $10 please" + "x" * 20  # pad past min length
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_SUCCESS_RESPONSE)) as mock_post:
        ebay_trading.send_member_message("123", "seller1", body_with_specials)
    sent_xml = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "&amp;" in sent_xml
    assert "&lt;fine&gt;" in sent_xml
    assert "<fine>" not in sent_xml.split("<Body>", 1)[1].split("</Body>", 1)[0]


def test_send_message_too_short_raises_without_calling_api(configured):
    with patch("integrations.ebay_trading.requests.post") as mock_post:
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.send_member_message("123", "seller1", "too short")
        mock_post.assert_not_called()


def test_send_message_missing_recipient_raises_without_calling_api(configured):
    """Empty/None recipient must fail fast — eBay's error message ('Recipient
    User Id is missing') is identical to what you get when you send a perfectly
    formed call without the RecipientID field, which is misleading."""
    with patch("integrations.ebay_trading.requests.post") as mock_post:
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.send_member_message("123", "", "this is long enough to pass body validation")
        mock_post.assert_not_called()


def test_send_message_missing_token_raises(monkeypatch):
    monkeypatch.setattr(config, "EBAY_USER_TOKEN", "")
    with patch("integrations.ebay_trading.requests.post") as mock_post:
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.send_member_message("123", "seller1", "this is a long enough body to pass the validator")
        mock_post.assert_not_called()


def test_send_message_failure_ack_raises_with_eBay_error(configured):
    failure_xml = """<?xml version="1.0"?>
    <AddMemberMessageAAQToPartnerResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack>
      <Errors>
        <ShortMessage>Item not found</ShortMessage>
        <LongMessage>The specified item could not be located.</LongMessage>
        <ErrorCode>17</ErrorCode>
      </Errors>
    </AddMemberMessageAAQToPartnerResponse>"""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(failure_xml)):
        with pytest.raises(ebay_trading.EbayTradingError) as exc_info:
            ebay_trading.send_member_message("123", "seller1", "this is a long enough body to test the call")
    assert "specified item could not be located" in str(exc_info.value).lower()


def test_send_message_no_partner_raises_specific_subclass(configured):
    """The 'not the partner of the transaction' error is its own type so the
    route can distinguish it from generic failures and route to the copy-paste UI."""
    failure_xml = """<?xml version="1.0"?>
    <AddMemberMessageAAQToPartnerResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack>
      <Errors>
        <ShortMessage>Not partner</ShortMessage>
        <LongMessage>The sender or recipient is not the partner of the transaction.</LongMessage>
      </Errors>
    </AddMemberMessageAAQToPartnerResponse>"""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(failure_xml)):
        with pytest.raises(ebay_trading.NoPartnerRelationshipError):
            ebay_trading.send_member_message("123", "seller1", "this is a long enough body to test the call")


def test_send_message_http_500_raises(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response("server error", status=500)):
        with pytest.raises(ebay_trading.EbayTradingError) as exc_info:
            ebay_trading.send_member_message("123", "seller1", "this is a long enough body to test the call")
    assert "HTTP 500" in str(exc_info.value)


_GET_MESSAGES_HEADERS = """<?xml version="1.0"?>
<GetMyMessagesResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Messages>
    <Message>
      <MessageID>1001</MessageID>
      <ItemID>111222333</ItemID>
    </Message>
    <Message>
      <MessageID>1002</MessageID>
      <ItemID>444555666</ItemID>
    </Message>
    <Message>
      <MessageID>1003</MessageID>
      <ItemID>111222333</ItemID>
    </Message>
  </Messages>
</GetMyMessagesResponse>"""


_GET_MESSAGES_BODIES = """<?xml version="1.0"?>
<GetMyMessagesResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <Messages>
    <Message>
      <MessageID>1003</MessageID>
      <Sender>seller2</Sender>
      <Text>I can do $200.</Text>
      <ReceiveDate>2026-06-15T12:00:00.000Z</ReceiveDate>
      <ItemID>111222333</ItemID>
    </Message>
    <Message>
      <MessageID>1001</MessageID>
      <Sender>seller1</Sender>
      <Text>Hello, the item is available.</Text>
      <ReceiveDate>2026-06-14T09:00:00.000Z</ReceiveDate>
      <ItemID>111222333</ItemID>
    </Message>
  </Messages>
</GetMyMessagesResponse>"""


def test_get_messages_filters_to_requested_item_and_sorts_oldest_first(configured):
    """First call lists headers; second call fetches bodies for matching IDs only."""
    responses = [_mock_response(_GET_MESSAGES_HEADERS), _mock_response(_GET_MESSAGES_BODIES)]
    with patch("integrations.ebay_trading.requests.post", side_effect=responses) as mock_post:
        msgs = ebay_trading.get_messages_for_item("111222333")

    # We made exactly two calls (header list → body fetch)
    assert mock_post.call_count == 2
    # The body fetch payload includes only the two matching IDs, not the third
    body_call_xml = mock_post.call_args_list[1].kwargs["data"].decode("utf-8")
    assert "<MessageID>1001</MessageID>" in body_call_xml
    assert "<MessageID>1003</MessageID>" in body_call_xml
    assert "<MessageID>1002</MessageID>" not in body_call_xml

    # Results are oldest-first
    assert [m["message_id"] for m in msgs] == ["1001", "1003"]
    assert msgs[0]["body"] == "Hello, the item is available."
    assert msgs[1]["body"] == "I can do $200."


def test_get_messages_no_matches_returns_empty_without_second_call(configured):
    """If header listing has zero matches for the item, we skip the body fetch."""
    no_match_xml = """<?xml version="1.0"?>
    <GetMyMessagesResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <Messages>
        <Message><MessageID>9</MessageID><ItemID>444555666</ItemID></Message>
      </Messages>
    </GetMyMessagesResponse>"""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(no_match_xml)) as mock_post:
        msgs = ebay_trading.get_messages_for_item("111222333")
    assert msgs == []
    assert mock_post.call_count == 1


def test_oauth_token_goes_in_iaf_header_not_xml_body(monkeypatch):
    """OAuth user tokens (v^... format) must use the X-EBAY-API-IAF-TOKEN
    header. Putting them in <RequesterCredentials> causes auth failure."""
    monkeypatch.setattr(config, "EBAY_USER_TOKEN", "v^1.1#i^1#some_oauth_token")
    monkeypatch.setattr(config, "EBAY_APP_ID", "app")
    monkeypatch.setattr(config, "EBAY_CERT_ID", "cert")
    monkeypatch.setattr(config, "EBAY_DEV_ID", "dev")
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_SUCCESS_RESPONSE)) as mock_post:
        ebay_trading.send_member_message("123", "seller1", "this is a long enough body to test the call path")

    headers = mock_post.call_args.kwargs["headers"]
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert headers["X-EBAY-API-IAF-TOKEN"] == "v^1.1#i^1#some_oauth_token"
    assert "<eBayAuthToken>" not in body
    assert "<RequesterCredentials>" not in body


def test_legacy_authnauth_token_goes_in_xml_body(configured):
    """Test fixture uses a 'test-token' string (legacy format) — confirm it
    still flows through the body path, not the IAF header path."""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_SUCCESS_RESPONSE)) as mock_post:
        ebay_trading.send_member_message("123", "seller1", "this is a long enough body to test the call path")
    headers = mock_post.call_args.kwargs["headers"]
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "X-EBAY-API-IAF-TOKEN" not in headers
    assert "<eBayAuthToken>test-token</eBayAuthToken>" in body


def test_legacy_item_id_handles_versioned_browse_format():
    assert ebay_trading._legacy_item_id("v1|123456789|0") == "123456789"
    # Variant suffix (non-zero) shouldn't change extraction
    assert ebay_trading._legacy_item_id("v1|256460558255|7654321") == "256460558255"
    # Already-numeric input passes through
    assert ebay_trading._legacy_item_id("123456789") == "123456789"


def test_legacy_item_id_raises_on_unparseable():
    with pytest.raises(ebay_trading.EbayTradingError):
        ebay_trading._legacy_item_id("not|a|number")
    with pytest.raises(ebay_trading.EbayTradingError):
        ebay_trading._legacy_item_id("")


def test_xml_parse_error_raises_ebay_trading_error(configured):
    garbage = _mock_response("<not really xml")
    with patch("integrations.ebay_trading.requests.post", return_value=garbage):
        with pytest.raises(ebay_trading.EbayTradingError) as exc_info:
            ebay_trading.send_member_message("123", "seller1", "this is a long enough body to pass validation")
    assert "parse" in str(exc_info.value).lower()


# ---------- PlaceOffer (Best Offer send path) ----------

_PLACE_OFFER_SUCCESS = """<?xml version="1.0" encoding="UTF-8"?>
<PlaceOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <BestOffer>
    <BestOfferID>1234567890</BestOfferID>
    <Status>Pending</Status>
  </BestOffer>
</PlaceOfferResponse>"""


def test_place_best_offer_posts_correct_envelope_and_returns_id(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_PLACE_OFFER_SUCCESS)) as mock_post:
        result = ebay_trading.place_best_offer("v1|256460558255|0", 285.00, "Would you consider $285?")

    assert result == {"offer_id": "1234567890", "status": "Pending"}
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    # EndUserIP MUST be present — eBay's PlaceOffer XSD rejects the request
    # without it, surfacing the misleading "Offer.Action invalid or missing"
    # error. Regression-pinning this so a future refactor doesn't drop the line.
    assert "<EndUserIP>" in body
    # Numeric ItemID (no v1|...|0)
    assert "<ItemID>256460558255</ItemID>" in body
    # Action + MaxBid + Quantity — eBay's PlaceOffer XSD allows only these in <Offer>.
    assert "<Action>BestOffer</Action>" in body
    assert '<MaxBid currencyID="USD">285.00</MaxBid>' in body
    assert "<Quantity>1</Quantity>" in body
    # BuyerMessage is NOT in the Offer container — eBay rejects PlaceOffer with
    # the message embedded. Round-1 prose is logged and dropped; round-2+ via
    # RespondToBestOffer accepts the message and the LLM's prose lands then.
    assert "<BuyerMessage>" not in body
    # Operation name routes to PlaceOffer, not AddMemberMessage
    assert mock_post.call_args.kwargs["headers"]["X-EBAY-API-CALL-NAME"] == "PlaceOffer"


def test_place_best_offer_formats_amount_to_two_decimals(configured):
    """eBay rejects MaxBid values with too many decimals or missing them entirely."""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_PLACE_OFFER_SUCCESS)) as mock_post:
        ebay_trading.place_best_offer("123", 285.0, "x" * 30)
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert '<MaxBid currencyID="USD">285.00</MaxBid>' in body


def test_place_best_offer_invalid_amount_raises_without_calling_api(configured):
    with patch("integrations.ebay_trading.requests.post") as mock_post:
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.place_best_offer("123", 0.0, "x" * 30)
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.place_best_offer("123", -10.0, "x" * 30)
        mock_post.assert_not_called()


def test_place_best_offer_succeeds_with_empty_message(configured):
    """Empty message is fine — PlaceOffer doesn't carry prose. The signature
    accepts message for symmetry but it's dropped server-side."""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_PLACE_OFFER_SUCCESS)) as mock_post:
        result = ebay_trading.place_best_offer("123", 285.00, "")
    assert result["offer_id"] == "1234567890"
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "<BuyerMessage>" not in body


def test_place_best_offer_not_eligible_raises_specific_subclass(configured):
    """When a listing doesn't accept Best Offer, eBay returns a Failure ack
    with a 'not eligible' message. The route handles this distinctly from
    transport failures so the UI can fall back to AAQ / manual paste."""
    not_eligible_xml = """<?xml version="1.0"?>
    <PlaceOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack>
      <Errors>
        <ShortMessage>Best Offer not enabled</ShortMessage>
        <LongMessage>This item is not eligible for Best Offer.</LongMessage>
      </Errors>
    </PlaceOfferResponse>"""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(not_eligible_xml)):
        with pytest.raises(ebay_trading.BestOfferNotSupportedError):
            ebay_trading.place_best_offer("123", 100.0, "x" * 30)


def test_place_best_offer_generic_failure_raises_ebay_trading_error(configured):
    """Other failure modes (e.g. item ended) surface as the generic exception."""
    bad_xml = """<?xml version="1.0"?>
    <PlaceOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Failure</Ack>
      <Errors>
        <ShortMessage>Listing ended</ShortMessage>
        <LongMessage>This listing has ended and no longer accepts offers.</LongMessage>
      </Errors>
    </PlaceOfferResponse>"""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(bad_xml)):
        with pytest.raises(ebay_trading.EbayTradingError) as exc:
            ebay_trading.place_best_offer("123", 100.0, "x" * 30)
        # Specifically not the BestOfferNotSupported subclass
        assert not isinstance(exc.value, ebay_trading.BestOfferNotSupportedError)


# ---------- GetBestOffers (status polling) ----------

_GET_BEST_OFFERS_PENDING = """<?xml version="1.0"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <BestOfferArray>
    <BestOffer>
      <BestOfferID>1234567890</BestOfferID>
      <Status>Pending</Status>
      <Price currencyID="USD">285.00</Price>
      <Buyer><UserID>buyer1</UserID></Buyer>
    </BestOffer>
  </BestOfferArray>
</GetBestOffersResponse>"""


_GET_BEST_OFFERS_COUNTERED = """<?xml version="1.0"?>
<GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
  <BestOfferArray>
    <BestOffer>
      <BestOfferID>1234567890</BestOfferID>
      <Status>Countered</Status>
      <Price currencyID="USD">285.00</Price>
      <SellerMessage>Best I can do is 310.</SellerMessage>
      <CounterOfferPrice currencyID="USD">310.00</CounterOfferPrice>
    </BestOffer>
  </BestOfferArray>
</GetBestOffersResponse>"""


def test_get_best_offer_status_pending(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_GET_BEST_OFFERS_PENDING)) as mock_post:
        result = ebay_trading.get_best_offer_status("v1|256460558255|0", "1234567890")

    assert result["status"] == "Pending"
    assert result["offer_id"] == "1234567890"
    assert result["counter_amount"] is None
    assert result["seller_message"] is None
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "<ItemID>256460558255</ItemID>" in body
    assert "<BestOfferID>1234567890</BestOfferID>" in body
    assert mock_post.call_args.kwargs["headers"]["X-EBAY-API-CALL-NAME"] == "GetBestOffers"


def test_get_best_offer_status_countered_surfaces_counter_price_and_message(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_GET_BEST_OFFERS_COUNTERED)):
        result = ebay_trading.get_best_offer_status("123", "1234567890")
    assert result["status"] == "Countered"
    assert result["counter_amount"] == 310.00
    assert result["seller_message"] == "Best I can do is 310."


def test_get_best_offer_status_unknown_offer_returns_status_unknown(configured):
    """If the BestOfferID isn't in the array (e.g. expired/purged), return a
    sentinel status rather than raising — the route shouldn't 502 on a stale poll."""
    empty_xml = """<?xml version="1.0"?>
    <GetBestOffersResponse xmlns="urn:ebay:apis:eBLBaseComponents">
      <Ack>Success</Ack>
      <BestOfferArray></BestOfferArray>
    </GetBestOffersResponse>"""
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(empty_xml)):
        result = ebay_trading.get_best_offer_status("123", "9999999")
    assert result["status"] == "Unknown"


# ---------- RespondToBestOffer (round 2+ structured response) ----------

_RESPOND_SUCCESS = """<?xml version="1.0"?>
<RespondToBestOfferResponse xmlns="urn:ebay:apis:eBLBaseComponents">
  <Ack>Success</Ack>
</RespondToBestOfferResponse>"""


def test_respond_to_best_offer_accept_omits_counter_price(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_RESPOND_SUCCESS)) as mock_post:
        ebay_trading.respond_to_best_offer("v1|123|0", "1234567890", "Accept", None, "Thanks, accepted.")
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "<Action>Accept</Action>" in body
    assert "<BestOfferID>1234567890</BestOfferID>" in body
    assert "<ItemID>123</ItemID>" in body
    assert "CounterOfferPrice" not in body  # Accept must not include a counter price
    assert mock_post.call_args.kwargs["headers"]["X-EBAY-API-CALL-NAME"] == "RespondToBestOffer"


def test_respond_to_best_offer_counter_includes_counter_price(configured):
    with patch("integrations.ebay_trading.requests.post", return_value=_mock_response(_RESPOND_SUCCESS)) as mock_post:
        ebay_trading.respond_to_best_offer("123", "1234567890", "Counter", 295.0, "How about $295?")
    body = mock_post.call_args.kwargs["data"].decode("utf-8")
    assert "<Action>Counter</Action>" in body
    assert '<CounterOfferPrice currencyID="USD">295.00</CounterOfferPrice>' in body


def test_respond_to_best_offer_counter_without_amount_raises(configured):
    """Counter action requires a price — surfacing the missing value early beats
    eBay's vague rejection."""
    with patch("integrations.ebay_trading.requests.post") as mock_post:
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.respond_to_best_offer("123", "1234567890", "Counter", None, "x" * 30)
        mock_post.assert_not_called()


def test_respond_to_best_offer_invalid_action_raises(configured):
    with patch("integrations.ebay_trading.requests.post") as mock_post:
        with pytest.raises(ebay_trading.EbayTradingError):
            ebay_trading.respond_to_best_offer("123", "1234567890", "BogusAction", None, "x" * 30)
        mock_post.assert_not_called()
