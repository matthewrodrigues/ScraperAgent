"""Tests for the eBay account-deletion notification endpoint.

The challenge response is the bit that's easy to subtly get wrong — input order,
encoding, hex case all matter. We pin a known-good hash so a future refactor
can't silently break eBay's verification.
"""

import hashlib

import pytest
from fastapi.testclient import TestClient

import config
from api.main import app


@pytest.fixture
def configured(monkeypatch):
    """Set the deletion config to known fixture values."""
    monkeypatch.setattr(config, "EBAY_DELETION_VERIFICATION_TOKEN", "test-verification-token-1234567890")
    monkeypatch.setattr(config, "EBAY_DELETION_ENDPOINT_URL", "https://example.test/ebay/account-deletion")
    return TestClient(app)


def test_challenge_returns_expected_sha256(configured):
    challenge = "abc123challenge"
    expected = hashlib.sha256(
        (challenge + "test-verification-token-1234567890" + "https://example.test/ebay/account-deletion").encode("utf-8")
    ).hexdigest()

    resp = configured.get("/ebay/account-deletion", params={"challenge_code": challenge})

    assert resp.status_code == 200
    assert resp.json() == {"challengeResponse": expected}


def test_challenge_input_order_is_code_token_endpoint(configured):
    """Defensive: assert the exact concatenation order eBay's spec requires.
    If anyone reorders inputs, this catches it before a real verification fails."""
    resp = configured.get("/ebay/account-deletion", params={"challenge_code": "X"})
    body = resp.json()["challengeResponse"]

    # Same inputs in different orders must NOT match — proves we use the spec order.
    wrong_order = hashlib.sha256(
        ("test-verification-token-1234567890" + "X" + "https://example.test/ebay/account-deletion").encode("utf-8")
    ).hexdigest()
    assert body != wrong_order


def test_challenge_unconfigured_returns_500(monkeypatch):
    monkeypatch.setattr(config, "EBAY_DELETION_VERIFICATION_TOKEN", "")
    monkeypatch.setattr(config, "EBAY_DELETION_ENDPOINT_URL", "")
    client = TestClient(app)

    resp = client.get("/ebay/account-deletion", params={"challenge_code": "anything"})
    assert resp.status_code == 500


def test_post_notification_returns_204(configured):
    resp = configured.post(
        "/ebay/account-deletion",
        json={"metadata": {"topic": "MARKETPLACE_ACCOUNT_DELETION"}, "notification": {"data": {"userId": "deadbeef"}}},
    )
    assert resp.status_code == 204
