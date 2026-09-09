"""Tests for the smoke script's own guard rails.

The script itself needs a live database, so what is tested here is that it
refuses to run against the wrong one and never leaks a credential.
"""

import pytest

import config
from scripts import smoke_supabase


def test_refuses_to_run_without_a_url(monkeypatch, capsys):
    monkeypatch.setattr(config, "SUPABASE_DB_URL", "")
    assert smoke_supabase.main([]) == 1
    assert "SUPABASE_DB_URL" in capsys.readouterr().err


def test_never_prints_the_connection_string(monkeypatch, capsys):
    """A smoke script's output gets pasted into issues and chat logs."""
    secret = "postgresql://postgres:hunter2@db.example.supabase.co:5432/postgres"
    monkeypatch.setattr(config, "SUPABASE_DB_URL", secret)
    monkeypatch.setattr(smoke_supabase, "_run_checks", lambda _m: None)
    smoke_supabase.main([])
    out = capsys.readouterr()
    assert "hunter2" not in out.out + out.err
    assert secret not in out.out + out.err


def test_reports_the_host_without_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        config, "SUPABASE_DB_URL",
        "postgresql://postgres:hunter2@db.example.supabase.co:5432/postgres",
    )
    monkeypatch.setattr(smoke_supabase, "_run_checks", lambda _m: None)
    smoke_supabase.main([])
    assert "db.example.supabase.co" in capsys.readouterr().out
