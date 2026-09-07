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
