"""Unit tests for scripts/archive_old_sessions.py (sprint 16)."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Add scripts/ to import path.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import archive_old_sessions as aos  # noqa: E402


def _touch(path: Path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_archive_skips_recent_files(tmp_path):
    sessions = tmp_path / "sessions"
    today = date(2026, 5, 4)
    # Recent: today, yesterday, 30 days ago — all keep.
    _touch(sessions / "session-2026-05-04.json")
    _touch(sessions / "session-2026-05-03.json")
    _touch(sessions / "session-2026-04-04.json")  # 30 days ago

    archived, skipped = aos.archive_old_sessions(sessions, days=90, today=today)
    assert archived == 0
    assert skipped == 0
    assert (sessions / "session-2026-05-04.json").exists()


def test_archive_moves_old_files_into_yyyy_mm_subdir(tmp_path):
    sessions = tmp_path / "sessions"
    today = date(2026, 5, 4)
    # Old: 100 days ago.
    _touch(sessions / "session-2026-01-24.json")
    _touch(sessions / "briefing-2026-01-24.html")
    _touch(sessions / "correspondence-2026-01-24.json")

    archived, skipped = aos.archive_old_sessions(sessions, days=90, today=today)
    assert archived == 3
    # Files moved into archive/2026/01/.
    assert (sessions / "archive" / "2026" / "01" / "session-2026-01-24.json").exists()
    assert (sessions / "archive" / "2026" / "01" / "briefing-2026-01-24.html").exists()
    assert (sessions / "archive" / "2026" / "01" / "correspondence-2026-01-24.json").exists()
    # Originals gone.
    assert not (sessions / "session-2026-01-24.json").exists()


def test_archive_dry_run_takes_no_action(tmp_path):
    sessions = tmp_path / "sessions"
    today = date(2026, 5, 4)
    _touch(sessions / "session-2026-01-24.json")

    archived, skipped = aos.archive_old_sessions(
        sessions, days=90, today=today, dry_run=True
    )
    assert archived == 1
    # Original still exists; archive dir not created.
    assert (sessions / "session-2026-01-24.json").exists()
    assert not (sessions / "archive").exists()


def test_archive_skips_unparseable_filenames(tmp_path):
    sessions = tmp_path / "sessions"
    today = date(2026, 5, 4)
    # Local dry-run files don't follow YYYY-MM-DD pattern in our regex.
    _touch(sessions / "session-2026-01-24.local.json")  # has date but matches pattern

    archived, skipped = aos.archive_old_sessions(sessions, days=90, today=today)
    # The .local.json file matches "session-(\d{4}-\d{2}-\d{2})" since the date
    # appears before .local — so it WILL be archived.
    assert archived == 1


def test_archive_skips_already_archived_files(tmp_path):
    """Files inside sessions/archive/ must NOT be re-archived."""
    sessions = tmp_path / "sessions"
    today = date(2026, 5, 4)
    # Pre-place a file directly in archive subdir.
    archived_path = sessions / "archive" / "2026" / "01" / "session-2026-01-24.json"
    _touch(archived_path)

    archived, skipped = aos.archive_old_sessions(sessions, days=90, today=today)
    assert archived == 0
    assert archived_path.exists()


def test_archive_handles_multiple_pattern_types(tmp_path):
    sessions = tmp_path / "sessions"
    today = date(2026, 5, 4)
    _touch(sessions / "session-2025-12-01.json")
    _touch(sessions / "briefing-2025-12-01.html")
    _touch(sessions / "correspondence-2025-12-01.json")
    _touch(sessions / "correspondence-2025-12-01.html")
    _touch(sessions / "run-manifest-2025-12-01.json")
    _touch(sessions / "debug-2025-12-01.html")

    archived, skipped = aos.archive_old_sessions(sessions, days=90, today=today)
    assert archived == 6
