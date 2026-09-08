"""Tests for environment-driven configuration.

`config` reads everything at import time, so these tests reload the module
under a patched environment rather than poking attributes. `importlib.reload`
re-runs the module body, which is exactly the code path a fresh process takes.
"""

import importlib
from pathlib import Path

import pytest

import config as config_module


@pytest.fixture
def reloaded_config(monkeypatch):
    """Reload `config` with whatever env the test has set, then restore it.

    Restoring matters: other tests hold a reference to the same module object,
    and leaving it pointed at a tmp_path would break them in file order.
    """
    def _reload():
        return importlib.reload(config_module)
    yield _reload
    monkeypatch.undo()
    importlib.reload(config_module)


def test_db_path_defaults_to_repo_root(reloaded_config, monkeypatch):
    monkeypatch.delenv("SCRAPERAGENT_DB_PATH", raising=False)
    monkeypatch.delenv("SCRAPERAGENT_DATA_DIR", raising=False)
    cfg = reloaded_config()
    assert cfg.DB_PATH == cfg.ROOT_DIR / "scraperagent.db"


def test_db_path_honors_explicit_env_override(reloaded_config, monkeypatch):
    monkeypatch.setenv("SCRAPERAGENT_DB_PATH", "/var/lib/scraperagent/app.db")
    cfg = reloaded_config()
    assert cfg.DB_PATH == Path("/var/lib/scraperagent/app.db")


def test_data_dir_relocates_db_and_secrets_together(reloaded_config, monkeypatch, tmp_path):
    monkeypatch.delenv("SCRAPERAGENT_DB_PATH", raising=False)
    monkeypatch.delenv("SCRAPERAGENT_SECRETS_DIR", raising=False)
    monkeypatch.setenv("SCRAPERAGENT_DATA_DIR", str(tmp_path))
    cfg = reloaded_config()
    assert cfg.DB_PATH == tmp_path / "scraperagent.db"
    assert cfg.SECRETS_DIR == tmp_path / "secrets"


def test_browser_profile_dir_follows_relocated_secrets_dir(reloaded_config, monkeypatch, tmp_path):
    """The Chrome profile and screenshots derive from SECRETS_DIR, so moving
    the data dir must carry them along — otherwise a relocated install would
    silently look for the eBay login session in the old place."""
    monkeypatch.delenv("EBAY_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.delenv("EBAY_BROWSER_SCREENSHOTS_DIR", raising=False)
    monkeypatch.setenv("SCRAPERAGENT_DATA_DIR", str(tmp_path))
    cfg = reloaded_config()
    assert cfg.EBAY_BROWSER_PROFILE_DIR == tmp_path / "secrets" / "ebay_chrome_profile"
    assert cfg.EBAY_BROWSER_SCREENSHOTS_DIR == tmp_path / "secrets" / "screenshots"


def test_templates_and_static_stay_pinned_to_repo_root(reloaded_config, monkeypatch, tmp_path):
    """Templates and static assets are code, not data — relocating the data
    dir must not send Jinja2 looking for them on a mounted volume."""
    monkeypatch.setenv("SCRAPERAGENT_DATA_DIR", str(tmp_path))
    cfg = reloaded_config()
    assert cfg.TEMPLATES_DIR == cfg.ROOT_DIR / "templates"
    assert cfg.STATIC_DIR == cfg.ROOT_DIR / "static"


def test_ebay_search_budget_default():
    assert config_module.EBAY_SEARCH_BUDGET_USD == 0.15


def test_apify_budget_default_covers_discovery_plus_pricing():
    # Google Shopping costs ~$0.49 and discovery ~$0.05; the cap must clear both
    # or cost_guard blocks reference pricing on every search.
    assert config_module.APIFY_BUDGET_USD >= 0.60
