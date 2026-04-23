"""End-to-end: scripts/write.py --dry-run on a checked-in session JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jeeves.testing.mocks import canned_session

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_repo(tmp_path: Path):
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("scripts", "jeeves", "pyproject.toml"):
        (target / name).symlink_to(REPO / name)
    (target / "sessions").mkdir()

    # Drop a fixture session JSON so write can load it.
    session = canned_session_as_json("2026-04-23")
    (target / "sessions" / "session-2026-04-23.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    yield target


def canned_session_as_json(date_str: str) -> dict:
    from datetime import date

    return canned_session(date.fromisoformat(date_str))


def _run(isolated_repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GITHUB_REPOSITORY"] = "test/fixture"
    env["JEEVES_REPO_ROOT"] = str(isolated_repo)
    env.pop("GITHUB_TOKEN", None)
    return subprocess.run(
        [sys.executable, "scripts/write.py", *args],
        cwd=isolated_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_write_dry_run_produces_valid_html(isolated_repo: Path):
    result = _run(isolated_repo, "--dry-run", "--date", "2026-04-23")
    assert result.returncode == 0, f"stderr: {result.stderr}"

    briefing = isolated_repo / "sessions" / "briefing-2026-04-23.local.html"
    assert briefing.exists(), f"expected {briefing}, got stderr: {result.stderr}"

    html = briefing.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "COVERAGE_LOG" in html


def test_write_plan_only_summarizes_session(isolated_repo: Path):
    result = _run(isolated_repo, "--plan-only", "--date", "2026-04-23")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    assert "Session date: 2026-04-23" in result.stdout
    assert "Enriched articles:" in result.stdout


def test_write_missing_session_fails_cleanly(isolated_repo: Path):
    result = _run(isolated_repo, "--dry-run", "--date", "2099-01-01")
    assert result.returncode == 3
    assert "No session file found" in result.stderr


def test_write_use_fixture_bypasses_session_load(isolated_repo: Path):
    # 2099-01-01 has no session file, but --use-fixture skips that load.
    result = _run(isolated_repo, "--use-fixture", "--dry-run", "--date", "2099-01-01")
    assert result.returncode == 0, f"stderr: {result.stderr}"
    briefing = isolated_repo / "sessions" / "briefing-2099-01-01.local.html"
    assert briefing.exists(), f"stderr: {result.stderr}"
    assert "canned mock session" in result.stderr


def test_write_skip_send_requires_groq_key(isolated_repo: Path):
    # --skip-send without --dry-run calls Groq; GROQ_API_KEY must be set.
    env = os.environ.copy()
    env["GITHUB_REPOSITORY"] = "test/fixture"
    env["JEEVES_REPO_ROOT"] = str(isolated_repo)
    env.pop("GROQ_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "scripts/write.py", "--skip-send", "--use-fixture", "--date", "2026-04-23"],
        cwd=isolated_repo, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "GROQ_API_KEY" in result.stderr
