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


def test_configured_true_with_only_broker_vars(monkeypatch):
    monkeypatch.setattr(config, "BROKER_URL", "https://broker.example.ts.net")
    monkeypatch.setattr(config, "BROKER_TOKEN", "sa_friendtoken")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    assert clients.anthropic_configured() is True
    assert clients.apify_configured() is True


def test_configured_true_with_only_direct_vendor_keys(monkeypatch):
    monkeypatch.setattr(config, "BROKER_URL", "")
    monkeypatch.setattr(config, "BROKER_TOKEN", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-real-anthropic")
    monkeypatch.setattr(config, "APIFY_TOKEN", "apify-real-token")
    assert clients.anthropic_configured() is True
    assert clients.apify_configured() is True


def test_configured_false_with_neither(monkeypatch):
    monkeypatch.setattr(config, "BROKER_URL", "")
    monkeypatch.setattr(config, "BROKER_TOKEN", "")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    monkeypatch.setattr(config, "APIFY_TOKEN", "")
    assert clients.anthropic_configured() is False
    assert clients.apify_configured() is False
