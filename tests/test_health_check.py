"""Tests for scripts/health_check.py — M6 acceptance criterion enforcer.

Hermetic: builds temp session JSON files, never touches the real sessions/.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts import health_check as hc


# ---------------------------------------------------------------------------
# Per-session evaluator
# ---------------------------------------------------------------------------


class TestEvaluateSession:
    def _write(self, tmp_path, day, payload):
        path = tmp_path / f"session-{day}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_rich_session_non_empty(self, tmp_path):
        rich = {
            "date": "2026-05-22",
            "local_news": [{"findings": "x" * 300}],
            "global_news": [{"findings": "y" * 300}],
            "intellectual_journals": [{"findings": "z" * 300}],
        }
        path = self._write(tmp_path, "2026-05-22", rich)
        result = hc.evaluate_session(path)
        assert result["is_non_empty"] is True
        assert result["populated_sectors"] >= 3

    def test_thin_session_marked_empty(self, tmp_path):
        thin = {
            "date": "2026-05-22",
            "local_news": [{"findings": "x" * 50}],     # <200 chars
            "global_news": [],                          # empty
            "weather": "",                              # empty
        }
        path = self._write(tmp_path, "2026-05-22", thin)
        result = hc.evaluate_session(path)
        assert result["is_non_empty"] is False
        assert result["populated_sectors"] < 3

    def test_today_22_pattern_is_thin(self, tmp_path):
        """The actual 2026-05-22 production session shape: 6 rich sectors,
        7 empty. Should evaluate as non_empty (>=3 populated)."""
        # 6 rich (intellectual_journals, triadic_ontology, ai_systems,
        # uap, literary_pick, newyorker), 7 empty.
        payload = {
            "intellectual_journals": [{"findings": "x" * 200}] * 5,  # 1000+ chars
            "triadic_ontology": {"findings": "x" * 1000},
            "ai_systems": {"findings": "x" * 1000},
            "uap": {"findings": "x" * 1000},
            "literary_pick": {"summary": "x" * 400},
            "weather": "",
            "local_news": [],
            "global_news": [],
            "wearable_ai": [],
            "career": {},
            "family": {},
            "english_lesson_plans": {"classroom_ready": [], "notes": "x" * 41},
            "enriched_articles": [],
        }
        path = self._write(tmp_path, "2026-05-22", payload)
        result = hc.evaluate_session(path)
        # 5 populated agent sectors (>=3 = non_empty), but well below 10 avg.
        assert result["is_non_empty"] is True
        assert 5 == result["populated_sectors"]

    def test_handles_malformed_json(self, tmp_path):
        path = tmp_path / "session-2026-05-22.json"
        path.write_text("not valid json {{{", encoding="utf-8")
        result = hc.evaluate_session(path)
        assert "error" in result


# ---------------------------------------------------------------------------
# Window collection
# ---------------------------------------------------------------------------


class TestCollectSessions:
    def test_respects_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hc, "SESSIONS_DIR", tmp_path)
        today = date.today()
        for d in range(0, 20):
            day = today - timedelta(days=d)
            (tmp_path / f"session-{day.isoformat()}.json").write_text("{}")

        within = hc.collect_sessions(window_days=7)
        # 7-day window includes today + 6 days back = 7 files
        assert len(within) == 7

    def test_ignores_non_session_files(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hc, "SESSIONS_DIR", tmp_path)
        (tmp_path / "session-2026-05-22.json").write_text("{}")
        (tmp_path / "correspondence-2026-05-22.json").write_text("{}")
        (tmp_path / "briefing-2026-05-22.html").write_text("")
        files = hc.collect_sessions(window_days=30)
        names = [p.name for p in files]
        assert names == ["session-2026-05-22.json"]


# ---------------------------------------------------------------------------
# Full M6 acceptance check
# ---------------------------------------------------------------------------


class TestM6Acceptance:
    def _build_window(self, tmp_path, monkeypatch, non_empty_count: int, total: int):
        monkeypatch.setattr(hc, "SESSIONS_DIR", tmp_path)
        today = date.today()
        rich_payload = {
            "local_news": [{"findings": "x" * 250}],
            "global_news": [{"findings": "y" * 250}],
            "intellectual_journals": [{"findings": "z" * 250}],
            "triadic_ontology": {"findings": "a" * 250},
            "ai_systems": {"findings": "b" * 250},
            "uap": {"findings": "c" * 250},
            "weather": "d" * 250,
            "career": {"notes": "e" * 250},
            "family": {"choir": "f" * 250},
            "english_lesson_plans": {"notes": "g" * 250},
            "wearable_ai": [{"findings": "h" * 250}],
        }
        thin_payload = {"local_news": [{"findings": "x" * 50}]}
        for i in range(total):
            day = (today - timedelta(days=i)).isoformat()
            payload = rich_payload if i < non_empty_count else thin_payload
            (tmp_path / f"session-{day}.json").write_text(json.dumps(payload))

    def test_pass_when_all_criteria_met(self, tmp_path, monkeypatch):
        self._build_window(tmp_path, monkeypatch, non_empty_count=10, total=12)
        # Stub git log check — no kill switch.
        monkeypatch.setattr(hc, "check_kill_switch",
                            lambda w: {"deploy_count": 0, "deploy_hits": []})
        result = hc.run_check(window_days=12)
        assert result["m6_pass"] is True
        assert result["non_empty_count"] == 10
        assert result["avg_populated_sectors"] >= 10.0

    def test_fail_when_too_few_non_empty(self, tmp_path, monkeypatch):
        self._build_window(tmp_path, monkeypatch, non_empty_count=3, total=12)
        monkeypatch.setattr(hc, "check_kill_switch",
                            lambda w: {"deploy_count": 0, "deploy_hits": []})
        result = hc.run_check(window_days=12)
        assert result["m6_pass"] is False
        assert result["m6_criterion_1_non_empty_count"] is False

    def test_fail_when_avg_sectors_low(self, tmp_path, monkeypatch):
        # 10 non-empty but avg <10 sectors each.
        monkeypatch.setattr(hc, "SESSIONS_DIR", tmp_path)
        today = date.today()
        # 3 rich sectors per non-empty (satisfies "is_non_empty" but fails avg≥10).
        thin_rich = {
            "local_news": [{"findings": "x" * 250}],
            "global_news": [{"findings": "y" * 250}],
            "intellectual_journals": [{"findings": "z" * 250}],
        }
        for i in range(12):
            day = (today - timedelta(days=i)).isoformat()
            (tmp_path / f"session-{day}.json").write_text(json.dumps(thin_rich))
        monkeypatch.setattr(hc, "check_kill_switch",
                            lambda w: {"deploy_count": 0, "deploy_hits": []})
        result = hc.run_check(window_days=12)
        assert result["non_empty_count"] == 12
        assert result["avg_populated_sectors"] == 3.0
        assert result["m6_criterion_3_avg_sectors"] is False
        assert result["m6_pass"] is False

    def test_fail_when_kill_switch_deployed(self, tmp_path, monkeypatch):
        self._build_window(tmp_path, monkeypatch, non_empty_count=10, total=12)
        monkeypatch.setattr(
            hc, "check_kill_switch",
            lambda w: {"deploy_count": 1, "deploy_hits": ["deadbeef deploy KILL_SWITCH"]},
        )
        result = hc.run_check(window_days=12)
        assert result["m6_pass"] is False
        assert result["m6_criterion_2_zero_kill_switch"] is False

    def test_exit_code_pass(self, tmp_path, monkeypatch):
        self._build_window(tmp_path, monkeypatch, non_empty_count=10, total=12)
        monkeypatch.setattr(hc, "check_kill_switch",
                            lambda w: {"deploy_count": 0, "deploy_hits": []})
        exit_code = hc.main(["--window", "12"])
        assert exit_code == 0

    def test_exit_code_fail(self, tmp_path, monkeypatch):
        self._build_window(tmp_path, monkeypatch, non_empty_count=2, total=12)
        monkeypatch.setattr(hc, "check_kill_switch",
                            lambda w: {"deploy_count": 0, "deploy_hits": []})
        exit_code = hc.main(["--window", "12"])
        assert exit_code == 1


class TestMinNonEmptyOverride:
    """The --min-non-empty flag lets one script gate two ROADMAP criteria:
    the legacy M6 12-day window (default threshold 4) and the M9 90-day
    window (caller passes 85 to match ROADMAP M9's >=85/90).
    Before the flag, --window 90 trivially exited 0 with >=4 rich sessions
    in the entire 90-day window — the M9 gate had no teeth.
    """

    def _build_window(self, tmp_path, monkeypatch, non_empty_count: int, total: int):
        monkeypatch.setattr(hc, "SESSIONS_DIR", tmp_path)
        today = date.today()
        rich_payload = {
            "local_news": [{"findings": "x" * 250}],
            "global_news": [{"findings": "y" * 250}],
            "intellectual_journals": [{"findings": "z" * 250}],
            "triadic_ontology": {"findings": "a" * 250},
            "ai_systems": {"findings": "b" * 250},
            "uap": {"findings": "c" * 250},
            "weather": "d" * 250,
            "career": {"notes": "e" * 250},
            "family": {"choir": "f" * 250},
            "english_lesson_plans": {"notes": "g" * 250},
            "wearable_ai": [{"findings": "h" * 250}],
        }
        thin_payload = {"local_news": [{"findings": "x" * 50}]}
        for i in range(total):
            day = (today - timedelta(days=i)).isoformat()
            payload = rich_payload if i < non_empty_count else thin_payload
            (tmp_path / f"session-{day}.json").write_text(json.dumps(payload))
        monkeypatch.setattr(hc, "check_kill_switch",
                            lambda w: {"deploy_count": 0, "deploy_hits": []})

    def test_default_threshold_when_override_none(self, tmp_path, monkeypatch):
        """Default behaviour preserved: omit --min-non-empty and the script
        still uses M6_MIN_NON_EMPTY (4). 6 rich sessions in a 10-day window
        passes the legacy gate."""
        self._build_window(tmp_path, monkeypatch, non_empty_count=6, total=10)
        result = hc.run_check(window_days=10)
        assert result["non_empty_threshold"] == hc.M6_MIN_NON_EMPTY
        assert result["m6_criterion_1_non_empty_count"] is True

    def test_override_above_actual_fails(self, tmp_path, monkeypatch):
        """Setting --min-non-empty above the actual non_empty count flips
        crit_1 to False — this is the M9 gate doing its job."""
        self._build_window(tmp_path, monkeypatch, non_empty_count=80, total=90)
        result = hc.run_check(window_days=90, min_non_empty=85)
        assert result["non_empty_threshold"] == 85
        assert result["non_empty_count"] == 80
        assert result["m6_criterion_1_non_empty_count"] is False
        assert result["m6_pass"] is False

    def test_override_at_or_below_actual_passes(self, tmp_path, monkeypatch):
        """When non_empty_count >= threshold, crit_1 is True. This is the
        target outcome when M9 is genuinely reached."""
        self._build_window(tmp_path, monkeypatch, non_empty_count=85, total=90)
        result = hc.run_check(window_days=90, min_non_empty=85)
        assert result["non_empty_threshold"] == 85
        assert result["non_empty_count"] == 85
        assert result["m6_criterion_1_non_empty_count"] is True

    def test_cli_min_non_empty_flag_passes_through(self, tmp_path, monkeypatch):
        """End-to-end: passing --min-non-empty 85 via argv reaches run_check
        and flips the exit code accordingly."""
        # 80 rich of 90 — fails at threshold 85.
        self._build_window(tmp_path, monkeypatch, non_empty_count=80, total=90)
        exit_code = hc.main(["--window", "90", "--min-non-empty", "85"])
        assert exit_code == 1

    def test_cli_min_non_empty_pass_path(self, tmp_path, monkeypatch):
        """Same plumbing, pass path. 85 rich of 90 + avg_sectors >= 10 +
        no kill switch deploy → exit 0."""
        self._build_window(tmp_path, monkeypatch, non_empty_count=85, total=90)
        exit_code = hc.main(["--window", "90", "--min-non-empty", "85"])
        assert exit_code == 0


def test_render_text_grep_matches_loop_state_pattern():
    """LOOP_STATE.md's VERIFY grep is `non_empty|KILL_SWITCH|avg_sectors`.
    Output must contain all three tokens so the grep is satisfied."""
    fake_result = {
        "window_days": 12,
        "sessions_found": 12,
        "non_empty_count": 10,
        "non_empty_threshold": 9,
        "avg_populated_sectors": 11.0,
        "avg_sectors_threshold": 10.0,
        "max_sectors": 13,
        "kill_switch": {"deploy_count": 0, "deploy_hits": []},
        "m6_pass": True,
        "per_session": [],
    }
    out = hc.render_text(fake_result)
    assert "non_empty" in out
    assert "KILL_SWITCH" in out
    assert "avg_sectors" in out
    assert "m6_pass=True" in out
