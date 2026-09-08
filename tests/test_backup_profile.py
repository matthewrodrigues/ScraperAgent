"""Tests for the Chrome-profile backup script.

The profile directory holds the agent's eBay login session. It cannot be
regenerated without a manual, 2FA-gated sign-in, which makes it the one piece
of local state whose loss actually costs the user something.
"""

import zipfile
from datetime import datetime

import pytest

from scripts.backup_profile import BackupError, backup_profile, prune_backups


@pytest.fixture
def profile(tmp_path):
    d = tmp_path / "ebay_chrome_profile"
    (d / "Default").mkdir(parents=True)
    (d / "Default" / "Cookies").write_bytes(b"cookie-data")
    (d / "Local State").write_text("state")
    return d


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"


def test_backup_contains_the_profile_contents(profile, backup_dir):
    archive = backup_profile(profile, backup_dir)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        assert "Default/Cookies" in names
        assert "Local State" in names
        assert z.read("Default/Cookies") == b"cookie-data"


def test_backup_creates_the_destination_directory(profile, backup_dir):
    assert not backup_dir.exists()
    archive = backup_profile(profile, backup_dir)
    assert archive.parent == backup_dir
    assert archive.exists()


def test_backup_filename_is_timestamped(profile, backup_dir):
    archive = backup_profile(profile, backup_dir, now=datetime(2026, 9, 6, 14, 30, 5))
    assert archive.name == "ebay_chrome_profile-20260906-143005.zip"


def test_missing_profile_is_an_error_not_an_empty_archive(tmp_path, backup_dir):
    """An empty zip would look like a successful backup and silently replace
    good ones as the retention window rolls forward."""
    with pytest.raises(BackupError, match="does not exist"):
        backup_profile(tmp_path / "nope", backup_dir)


def test_empty_profile_is_an_error_too(tmp_path, backup_dir):
    empty = tmp_path / "ebay_chrome_profile"
    empty.mkdir()
    with pytest.raises(BackupError, match="empty"):
        backup_profile(empty, backup_dir)


def test_unreadable_files_are_skipped_rather_than_aborting_the_backup(profile, backup_dir, monkeypatch):
    """Chrome holds an exclusive lock on parts of a live profile on Windows. A
    backup that aborts on the first locked file would only ever succeed while
    the browser is closed, which is not when a scheduled task runs."""
    real_open = zipfile.ZipFile.write

    def flaky_write(self, filename, arcname=None, **kw):
        if str(filename).endswith("Cookies"):
            raise PermissionError("locked by Chrome")
        return real_open(self, filename, arcname, **kw)

    monkeypatch.setattr(zipfile.ZipFile, "write", flaky_write)
    archive = backup_profile(profile, backup_dir)
    with zipfile.ZipFile(archive) as z:
        assert "Local State" in z.namelist()
        assert "Default/Cookies" not in z.namelist()


def test_prune_keeps_only_the_newest_n_backups(backup_dir):
    backup_dir.mkdir()
    made = []
    for i in range(7):
        p = backup_dir / f"ebay_chrome_profile-2026090{i}-000000.zip"
        p.write_text("x")
        made.append(p)

    prune_backups(backup_dir, keep=3)

    survivors = sorted(p.name for p in backup_dir.glob("*.zip"))
    assert survivors == [p.name for p in made[-3:]]


def test_prune_ignores_unrelated_files_in_the_backup_directory(backup_dir):
    """The backup dir may be a general-purpose folder. Retention must only ever
    delete archives this script created."""
    backup_dir.mkdir()
    for i in range(4):
        (backup_dir / f"ebay_chrome_profile-2026090{i}-000000.zip").write_text("x")
    (backup_dir / "important-tax-return.zip").write_text("keep me")
    (backup_dir / "notes.txt").write_text("keep me")

    prune_backups(backup_dir, keep=1)

    assert (backup_dir / "important-tax-return.zip").exists()
    assert (backup_dir / "notes.txt").exists()
    assert len(list(backup_dir.glob("ebay_chrome_profile-*.zip"))) == 1


def test_backup_prunes_as_part_of_a_normal_run(profile, backup_dir):
    for i in range(5):
        backup_profile(profile, backup_dir, keep=2, now=datetime(2026, 9, 1 + i, 12, 0, 0))
    assert len(list(backup_dir.glob("*.zip"))) == 2
