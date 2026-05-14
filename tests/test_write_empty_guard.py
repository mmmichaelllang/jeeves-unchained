"""GATE-A: scripts/write.py refuses to write a briefing when every non-TOTT
research sector is empty.

Why: 2026-05-13 daily run shipped a briefing fabricating URLs (congress.gov,
pentagon.gov, etc.) when research returned nothing. GATE-A blocks the write+
send and exits non-zero so the failure is loud rather than silent-with-
hallucinated-content.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

from jeeves.testing.mocks import canned_session

REPO = Path(__file__).resolve().parent.parent


# Import the helper directly for unit-level tests.
sys.path.insert(0, str(REPO))
from scripts.write import _session_research_empty  # noqa: E402


# ----------------------------------------------------------------------- #
# Unit tests on _session_research_empty()                                 #
# ----------------------------------------------------------------------- #


def _empty_session_dict(date_str: str = "2026-05-13") -> dict:
    """Mirrors the shape of session-2026-05-13.json after the failed run."""
    return {
        "date": date_str,
        "status": "complete",
        "triadic_ontology": {"findings": "", "urls": []},
        "ai_systems": {"findings": "", "urls": []},
        "uap": {"findings": "", "urls": []},
        "career": {},
        "family": {},
        "local_news": [],
        "global_news": [],
        "intellectual_journals": [],
        "wearable_ai": [],
        "enriched_articles": [],
        "english_lesson_plans": {
            "classroom_ready": [],
            "pedagogy_pieces": [],
            "notes": "",
        },
        "weather": "",
        # TOTT is populated by the direct fetcher even on agent failure.
        "newyorker": {
            "available": True,
            "title": "Sample TOTT title",
            "section": "art dept.",
            "url": "https://example.com",
        },
        "literary_pick": {"available": False},
        "correspondence": {"found": True, "text": "..."},
        "dedup": {"covered_urls": [], "covered_headlines": [], "cross_sector_dupes": []},
    }


def test_empty_session_detected():
    is_empty, summary = _session_research_empty(_empty_session_dict())
    assert is_empty is True
    assert "(none)" in summary or "populated=[]" in summary


def test_tott_alone_is_not_enough():
    """Newyorker (TOTT) populated but all research sectors empty -> still empty.
    The Option A policy: don't ship a briefing built on JUST a New Yorker excerpt
    plus fabricated everything-else.
    """
    s = _empty_session_dict()
    # newyorker already populated in fixture
    is_empty, _ = _session_research_empty(s)
    assert is_empty is True


def test_one_populated_sector_clears_gate():
    s = _empty_session_dict()
    s["local_news"] = [{"title": "Edmonds council vote", "url": "https://example.org/a"}]
    is_empty, summary = _session_research_empty(s)
    assert is_empty is False
    assert "local_news" in summary


def test_findings_text_alone_counts_as_populated():
    s = _empty_session_dict()
    s["triadic_ontology"] = {"findings": "Some long synthesis...", "urls": []}
    is_empty, _ = _session_research_empty(s)
    assert is_empty is False


def test_urls_alone_counts_as_populated():
    s = _empty_session_dict()
    s["ai_systems"] = {"findings": "", "urls": ["https://arxiv.org/abs/2401.00001"]}
    is_empty, _ = _session_research_empty(s)
    assert is_empty is False


def test_weather_alone_counts_as_populated():
    s = _empty_session_dict()
    s["weather"] = "73F partly cloudy, 8mph WSW"
    is_empty, _ = _session_research_empty(s)
    assert is_empty is False


def test_lesson_plans_classroom_ready_counts():
    s = _empty_session_dict()
    s["english_lesson_plans"]["classroom_ready"] = [{"title": "Verbs"}]
    is_empty, _ = _session_research_empty(s)
    assert is_empty is False


def test_canned_fixture_passes_gate():
    """The canned mock session should always pass GATE-A (regression check)."""
    sess = canned_session(date.fromisoformat("2026-04-23"))
    is_empty, summary = _session_research_empty(sess)
    assert is_empty is False, f"canned_session classified as empty: {summary}"


# ----------------------------------------------------------------------- #
# End-to-end: scripts/write.py against an empty session                   #
# ----------------------------------------------------------------------- #


@pytest.fixture
def isolated_repo_empty_session(tmp_path: Path):
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("scripts", "jeeves", "pyproject.toml"):
        (target / name).symlink_to(REPO / name)
    (target / "sessions").mkdir()
    session = _empty_session_dict("2026-05-13")
    (target / "sessions" / "session-2026-05-13.json").write_text(
        json.dumps(session, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    yield target


def _run_write(isolated_repo: Path, *args: str, extra_env: dict | None = None):
    env = os.environ.copy()
    env["GITHUB_REPOSITORY"] = "test/fixture"
    env["JEEVES_REPO_ROOT"] = str(isolated_repo)
    env.pop("GITHUB_TOKEN", None)
    # Provide a Groq key shape so dry-run-less paths don't trip the secret check.
    env.setdefault("GROQ_API_KEY", "gsk_fake_for_gate_a_test")
    env.setdefault("OPENROUTER_API_KEY", "sk-or-fake_for_gate_a_test")
    env.setdefault("CEREBRAS_API_KEY", "csk-fake_for_gate_a_test")
    env.setdefault("JEEVES_RECIPIENT_EMAIL", "test@example.com")
    env.setdefault("GMAIL_APP_PASSWORD", "abcdwxyzabcdwxyz")
    env.setdefault("NVIDIA_API_KEY", "nvapi-fake_for_gate_a_test")
    env.setdefault("SERPER_API_KEY", "fake_for_gate_a_test_serper_key_12345678")
    env.setdefault("TAVILY_API_KEY", "tvly-fake_for_gate_a_test_12345678901234")
    env.setdefault("EXA_API_KEY", "fake_for_gate_a_test_exa_key_abcdef")
    env.setdefault("GOOGLE_API_KEY", "AIzaSyfake_for_gate_a_test_abcdefghij")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, "scripts/write.py", "--date", "2026-05-13", "--skip-send", *args],
        cwd=isolated_repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_e2e_empty_session_blocks_write(isolated_repo_empty_session: Path):
    """write.py on an all-empty session returns exit code 5 and emits no briefing."""
    proc = _run_write(isolated_repo_empty_session)
    assert proc.returncode == 5, (
        f"expected exit 5 (GATE-A block), got {proc.returncode}\n"
        f"stdout: {proc.stdout[-2000:]}\n"
        f"stderr: {proc.stderr[-2000:]}"
    )
    # No briefing should have been written.
    assert not (isolated_repo_empty_session / "sessions" / "briefing-2026-05-13.html").exists()
    # GATE-A diagnostic should be in stderr.
    assert "GATE-A" in proc.stderr


def test_e2e_force_empty_bypass(isolated_repo_empty_session: Path):
    """JEEVES_FORCE_WRITE_EMPTY=1 lets the write proceed past GATE-A.

    We don't assert the write SUCCEEDS — without a real GROQ_API_KEY it
    will fail later — but the failure must be AFTER GATE-A, not at it.
    The diagnostic should say "GATE-A bypassed".
    """
    proc = _run_write(
        isolated_repo_empty_session,
        extra_env={"JEEVES_FORCE_WRITE_EMPTY": "1"},
    )
    # GATE-A should NOT be the proximate failure.
    assert "GATE-A bypassed" in proc.stderr, (
        f"expected GATE-A bypass message in stderr; got:\n{proc.stderr[-2000:]}"
    )
