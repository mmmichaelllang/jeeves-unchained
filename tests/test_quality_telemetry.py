"""Tests for scripts/quality_telemetry_report.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "quality_telemetry_report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "quality_telemetry_report", SCRIPT_PATH,
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["quality_telemetry_report"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def _make_manifest(d: date, *, score=80, warnings=None, words=4500):
    return {
        "date": d.isoformat(),
        "groq_parts": 8,
        "nim_fallback_parts": 1,
        "nim_refine_succeeded": 8,
        "nim_refine_failed": 0,
        "briefing_word_count": words,
        "profane_aside_count": 5,
        "banned_word_hits": [],
        "banned_transition_hits": [],
        "quality_warnings": warnings or [],
        "quality_score": score,
    }


# ------------------------------------------------------------ aggregation ----

def test_aggregate_warnings_buckets_by_prefix(mod):
    manifests = [
        _make_manifest(date(2026, 5, 1), warnings=[
            "nim_refine_failed:part4:APITimeoutError;timeout",
            "nim_refine_failed:part6:RuntimeError;boom",
            "part7_uap_fallback_injected",
        ]),
        _make_manifest(date(2026, 5, 2), warnings=[
            "nim_refine_failed:part1:RuntimeError;x",
            "part7_uap_fallback_injected",
            "part9_tott_scaffolding_injected",
        ]),
    ]
    counter = mod.aggregate_warnings(manifests)
    assert counter["nim_refine_failed"] == 3
    assert counter["part7_uap_fallback_injected"] == 2
    assert counter["part9_tott_scaffolding_injected"] == 1


def test_aggregate_warnings_handles_empty(mod):
    assert mod.aggregate_warnings([]) == {}
    assert mod.aggregate_warnings([_make_manifest(date.today())]) == {}


def test_aggregate_warnings_skips_non_string(mod):
    """Defensive — manifests may have malformed warning entries."""
    bad = _make_manifest(date.today(), warnings=[42, None, {"x": 1}, "ok_warn"])
    counter = mod.aggregate_warnings([bad])
    assert counter["ok_warn"] == 1
    assert sum(counter.values()) == 1


def test_aggregate_scores(mod):
    manifests = [
        _make_manifest(date(2026, 5, 1), score=70),
        _make_manifest(date(2026, 5, 2), score=80),
        _make_manifest(date(2026, 5, 3), score=90),
    ]
    mean, mn, mx = mod.aggregate_scores(manifests)
    assert mean == 80.0
    assert mn == 70
    assert mx == 90


def test_aggregate_scores_empty(mod):
    assert mod.aggregate_scores([]) == (0.0, 0, 0)


# ------------------------------------------------------------- chronic -------

def test_detect_chronic_only_returns_at_or_above_threshold(mod):
    from collections import Counter
    counter = Counter({
        "part7_uap_fallback_injected": 4,        # >= threshold 3
        "part7_route_b_uap_dropped": 2,          # >= threshold 2
        "part7_literary_fallback_injected": 1,   # below threshold 3
        "nim_refine_failed": 6,                  # >= threshold 5
    })
    chronic = mod.detect_chronic(counter)
    bucket_names = {w for (w, _, _) in chronic}
    assert "part7_uap_fallback_injected" in bucket_names
    assert "part7_route_b_uap_dropped" in bucket_names
    assert "nim_refine_failed" in bucket_names
    assert "part7_literary_fallback_injected" not in bucket_names
    # Sorted descending by count.
    counts = [c for (_, c, _) in chronic]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------- load_manifests ---

def test_load_manifests_round_trip(tmp_path, mod, monkeypatch):
    """Files written for the last 3 days are loaded newest-first."""
    today = date(2026, 5, 8)
    monkeypatch.setattr(mod, "_utc_today", lambda: today)
    sessions = tmp_path / "sessions"
    sessions.mkdir()

    for d in (today, today - timedelta(days=1), today - timedelta(days=2)):
        path = sessions / f"run-manifest-{d.isoformat()}.json"
        path.write_text(json.dumps(_make_manifest(d)), encoding="utf-8")

    manifests = mod.load_manifests(sessions, days=3)
    dates = [m["date"] for m in manifests]
    # Newest-first order.
    assert dates == [
        today.isoformat(),
        (today - timedelta(days=1)).isoformat(),
        (today - timedelta(days=2)).isoformat(),
    ]


def test_load_manifests_skips_malformed(tmp_path, mod, monkeypatch):
    today = date(2026, 5, 8)
    monkeypatch.setattr(mod, "_utc_today", lambda: today)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / f"run-manifest-{today.isoformat()}.json").write_text(
        "not valid json", encoding="utf-8"
    )
    yesterday = today - timedelta(days=1)
    (sessions / f"run-manifest-{yesterday.isoformat()}.json").write_text(
        json.dumps(_make_manifest(yesterday)), encoding="utf-8"
    )
    manifests = mod.load_manifests(sessions, days=2)
    assert len(manifests) == 1
    assert manifests[0]["date"] == yesterday.isoformat()


def test_load_manifests_no_files(tmp_path, mod, monkeypatch):
    today = date(2026, 5, 8)
    monkeypatch.setattr(mod, "_utc_today", lambda: today)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    assert mod.load_manifests(sessions, days=7) == []


# ------------------------------------------------------- markdown rendering --

def test_markdown_report_contains_chronic_section(mod):
    manifests = [
        _make_manifest(date(2026, 5, 1), warnings=[
            "part7_uap_fallback_injected"
        ] * 5, score=70),
    ]
    counter = mod.aggregate_warnings(manifests)
    chronic = mod.detect_chronic(counter)
    report = mod.build_markdown_report(
        manifests=manifests, counter=counter,
        score_mean=70.0, score_min=70, score_max=70,
        chronic=chronic, days=7,
    )
    assert "# Jeeves — Quality Telemetry Report" in report
    assert "part7_uap_fallback_injected" in report
    assert "Chronic warnings" in report
    # 5 hits with threshold 3 → must show in chronic table.
    assert "| 5 |" in report or "| `part7_uap_fallback_injected` | 5 |" in report


def test_markdown_report_clean_when_no_warnings(mod):
    manifests = [_make_manifest(date(2026, 5, 1), warnings=[], score=95)]
    counter = mod.aggregate_warnings(manifests)
    chronic = mod.detect_chronic(counter)
    report = mod.build_markdown_report(
        manifests=manifests, counter=counter,
        score_mean=95.0, score_min=95, score_max=95,
        chronic=chronic, days=7,
    )
    assert "None — all monitored warnings below their threshold." in report
    assert "No warnings recorded in window." in report


# --------------------------------------------------------------- main --------

def test_main_no_manifests_returns_1(tmp_path, mod, monkeypatch):
    """When there are no manifests in window, main exits 1 (not 0, not 2)."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    # Redirect REPO_ROOT to tmp so script writes inside the sandbox.
    monkeypatch.setattr(mod, "REPO_ROOT", tmp_path)
    rc = mod.main(["--days", "7", "--no-write"])
    assert rc == 1
