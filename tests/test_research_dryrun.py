"""End-to-end dry-run test: scripts/research.py --dry-run writes a valid session JSON."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def isolated_sessions(tmp_path: Path, monkeypatch):
    """Run in an isolated sessions/ dir so we don't pollute the repo."""

    # we redirect by overriding cwd to tmp_path and symlinking the source tree
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("scripts", "jeeves", "pyproject.toml"):
        (target / name).symlink_to(REPO / name)
    (target / "sessions").mkdir()
    yield target


def test_dry_run_produces_valid_session(isolated_sessions: Path, monkeypatch):
    env = os.environ.copy()
    env["GITHUB_REPOSITORY"] = "test/fixture"
    env["JEEVES_REPO_ROOT"] = str(isolated_sessions)
    env.pop("GITHUB_TOKEN", None)

    # Use the real scripts/research.py but cwd into the isolated tree so the
    # session lands in tmp.
    result = subprocess.run(
        [sys.executable, "scripts/research.py", "--dry-run", "--date", "2026-04-23"],
        cwd=isolated_sessions,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"

    session_file = isolated_sessions / "sessions" / "session-2026-04-23.local.json"
    assert session_file.exists(), f"expected {session_file}, got stderr: {result.stderr}"

    data = json.loads(session_file.read_text())
    assert data["date"] == "2026-04-23"
    assert data["status"] == "complete"
    assert len(data["enriched_articles"]) >= 3
    assert data["newyorker"]["available"] is True
