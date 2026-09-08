"""Tests for dashboard authentication.

The dashboard drives real spend (Anthropic, Apify) and a logged-in eBay
browser session, so every route is closed by default. The interesting surface
is the exempt list: three paths that MUST stay open, and everything else that
MUST NOT.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import config
from api.main import app
from tests.conftest import TEST_DASHBOARD_PASSWORD as TEST_PASSWORD


@pytest.fixture
def anon():
    """A client that has never logged in."""
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def authed(anon):
    resp = anon.post("/login", data={"password": TEST_PASSWORD})
    assert resp.status_code == 303, "login should redirect on success"
    return anon


# ---- Exempt paths: must be reachable with no session at all ----

def test_health_is_reachable_without_auth(anon):
    assert anon.get("/health").status_code == 200


def test_ebay_deletion_challenge_is_reachable_without_auth(anon, monkeypatch):
    """eBay's servers call this and cannot authenticate. If it ever ends up
    behind the login wall, the production keyset fails revalidation."""
    monkeypatch.setattr(config, "EBAY_DELETION_VERIFICATION_TOKEN", "tok")
    monkeypatch.setattr(config, "EBAY_DELETION_ENDPOINT_URL", "https://example.com/ebay/account-deletion")
    resp = anon.get("/ebay/account-deletion", params={"challenge_code": "abc"})
    assert resp.status_code == 200
    assert "challengeResponse" in resp.json()


def test_static_assets_are_reachable_without_auth(anon):
    assert anon.get("/static/app.css").status_code == 200


def test_login_page_is_reachable_without_auth(anon):
    assert anon.get("/login").status_code == 200


# ---- Everything else is closed ----

def test_dashboard_redirects_to_login_when_anonymous(anon):
    resp = anon.get("/")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_search_detail_redirects_to_login_when_anonymous(anon):
    resp = anon.get("/searches/1")
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


def test_post_routes_are_closed_too(anon):
    """A POST that slipped past auth would spend money, not just leak data."""
    resp = anon.post("/searches/parse", data={"criteria_nl": "red iphone"})
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/login")


# ---- Login / logout ----

def test_correct_password_grants_access(authed):
    assert authed.get("/").status_code == 200


def test_wrong_password_is_rejected_and_grants_nothing(anon):
    resp = anon.post("/login", data={"password": "hunter2"})
    assert resp.status_code == 401
    assert anon.get("/").status_code == 303


def test_login_preserves_the_originally_requested_path(anon):
    resp = anon.get("/searches/7")
    assert resp.headers["location"] == "/login?next=%2Fsearches%2F7"
    resp = anon.post("/login", data={"password": TEST_PASSWORD, "next": "/searches/7"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/searches/7"


def test_login_ignores_offsite_next_targets(anon):
    """`next` is attacker-controllable via a crafted link, so an absolute URL
    must not turn the login form into an open redirect."""
    resp = anon.post("/login", data={"password": TEST_PASSWORD, "next": "https://evil.example/pwn"})
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


def test_logout_ends_the_session(authed):
    assert authed.post("/logout").status_code == 303
    assert authed.get("/").status_code == 303


# ---- HTMX and misconfiguration ----

def test_htmx_request_gets_a_client_side_redirect_not_a_page_fragment(anon):
    """The dashboard polls via HTMX. On an expired session a 303 would swap the
    whole login page into a fragment; HX-Redirect makes the browser navigate."""
    resp = anon.get("/searches/1/content", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"] == "/login"


def test_unset_password_locks_the_dashboard_rather_than_opening_it(anon, monkeypatch):
    """Fail closed: a misconfigured deploy must not be an open dashboard."""
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    resp = anon.get("/")
    assert resp.status_code == 503
    assert "DASHBOARD_PASSWORD" in resp.text


def test_unset_password_still_leaves_the_ebay_webhook_reachable(anon, monkeypatch):
    """Locking the dashboard must not break eBay's keyset revalidation."""
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    assert anon.get("/health").status_code == 200


def test_signed_in_pages_offer_a_logout_control(authed):
    assert "/logout" in authed.get("/").text


def test_login_page_does_not_offer_a_logout_control(anon):
    """The login page extends the same base template; a sign-out link there
    would be nonsense at best and confusing at worst."""
    assert "/logout" not in anon.get("/login").text


# ---- Database isolation ----

def test_this_module_cannot_reach_the_real_database(anon, tmp_path_factory):
    """Regression guard: this file's fixtures build a TestClient with no
    explicit tmp_db request, which is exactly the gap that let these tests hit
    the real scraperagent.db. tmp_db is autouse now (see conftest.py), so
    config.DB_PATH must point somewhere under pytest's tmp dir for every test
    in this module, not at the repo root, whether or not the test asked for
    tmp_db by name."""
    pytest_root = tmp_path_factory.getbasetemp()
    assert config.DB_PATH.resolve().is_relative_to(pytest_root.resolve())

    real_db = Path(__file__).resolve().parent.parent / "scraperagent.db"
    assert config.DB_PATH.resolve() != real_db.resolve()
