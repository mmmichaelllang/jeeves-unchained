"""Tests for scripts/audit_health_report.py — hermetic, no SMTP, no real git."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _make_audit_fix(tmp_path: Path, day: str, actions: list[dict],
                    audit_model: str | None = "stub/test:free") -> Path:
    """Write a synthetic audit-fix-<day>.json into tmp_path/sessions/."""
    sd = tmp_path / "sessions"
    sd.mkdir(exist_ok=True)
    p = sd / f"audit-fix-{day}.json"
    p.write_text(json.dumps({
        "date": day,
        "pre_fix_chars": 11000,
        "post_fix_chars": 13000,
        "actions": actions,
        "pre_fix_defect_count": len(actions),
        "post_fix_defect_count": None,
        "audit_model_used": audit_model,
    }))
    return sd


def test_collect_week_report_counts_status_buckets(tmp_path):
    from audit_health_report import collect_week_report  # noqa: PLC0415

    sd = _make_audit_fix(tmp_path, "2026-05-05", [
        {"type": "rerender_empty_with_data", "section": "Library Stacks",
         "detail": "applied", "status": "applied"},
        {"type": "rerender_greeting", "section": "(greeting)",
         "detail": "validator rejected: non-html prefix", "status": "failed"},
        {"type": "strip_hallucinated_url", "section": None,
         "detail": "stripped", "status": "applied"},
    ])
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    assert len(rep.days) == 1
    d = rep.days[0]
    assert d.date == "2026-05-05"
    assert d.applied == 2
    assert d.skipped == 0
    assert d.failed == 1
    assert d.failed_validator_rejected == 1
    assert d.failed_llm_call == 0
    assert d.failed_other == 0
    assert d.actions_by_type["rerender_empty_with_data"] == 1
    assert d.actions_by_type["rerender_greeting"] == 1
    assert d.actions_by_type["strip_hallucinated_url"] == 1
    assert d.audit_model_used == "stub/test:free"


def test_collect_week_report_failure_buckets_distinguish_validator_vs_llm(tmp_path):
    from audit_health_report import collect_week_report  # noqa: PLC0415

    sd = _make_audit_fix(tmp_path, "2026-05-05", [
        {"type": "rerender_empty_with_data", "section": "x",
         "detail": "validator rejected: cot marker: 'we need to'", "status": "failed"},
        {"type": "rerender_empty_with_data", "section": "y",
         "detail": "skipped — LLM call failed", "status": "failed"},
        {"type": "rerender_empty_with_data", "section": "z",
         "detail": "h3 vanished mid-fix", "status": "failed"},
    ])
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    d = rep.days[0]
    assert d.failed == 3
    assert d.failed_validator_rejected == 1
    assert d.failed_llm_call == 1
    assert d.failed_other == 1


def test_collect_week_report_window_excludes_old_files(tmp_path):
    from audit_health_report import collect_week_report  # noqa: PLC0415

    sd = _make_audit_fix(tmp_path, "2026-04-01", [{"type":"x","section":None,"detail":"","status":"applied"}])
    _make_audit_fix(tmp_path, "2026-05-05", [{"type":"y","section":None,"detail":"","status":"applied"}])

    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    assert len(rep.days) == 1
    assert rep.days[0].date == "2026-05-05"


def test_collect_week_report_lists_missing_days(tmp_path):
    from audit_health_report import collect_week_report  # noqa: PLC0415

    sd = _make_audit_fix(tmp_path, "2026-05-05", [{"type":"x","section":None,"detail":"","status":"applied"}])
    _make_audit_fix(tmp_path, "2026-05-03", [{"type":"x","section":None,"detail":"","status":"applied"}])

    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    # Window: 04-30, 05-01, 05-02, 05-03, 05-04, 05-05, 05-06
    # Have:   05-03 + 05-05
    # Missing: 04-30, 05-01, 05-02, 05-04, 05-06 -> 5
    assert len(rep.missing_days) == 5
    assert "2026-05-04" in rep.missing_days
    assert "2026-05-06" in rep.missing_days
    assert "2026-05-03" not in rep.missing_days


def test_collect_week_report_skips_malformed_json(tmp_path):
    from audit_health_report import collect_week_report  # noqa: PLC0415

    sd = tmp_path / "sessions"
    sd.mkdir()
    (sd / "audit-fix-2026-05-05.json").write_text("not json at all {")

    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    assert len(rep.days) == 0
    # The malformed file's date IS in window; should appear as missing.
    assert "2026-05-05" in rep.missing_days


def test_render_text_no_runs_explains_why(tmp_path):
    from audit_health_report import collect_week_report, render_text  # noqa: PLC0415

    sd = tmp_path / "sessions"
    sd.mkdir()
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    out = render_text(rep)
    assert "No audit-fix runs" in out
    assert "JEEVES_AUDITOR_AUTO_FIX=0" in out


def test_render_text_sentinel_warns_on_high_validator_rate(tmp_path):
    from audit_health_report import collect_week_report, render_text  # noqa: PLC0415

    # Three days each with 3 validator-rejected = 9 total over 3 days = 3.0/day -> warn.
    for day in ("2026-05-04", "2026-05-05", "2026-05-06"):
        _make_audit_fix(tmp_path, day, [
            {"type":"rerender_empty_with_data","section":"x",
             "detail":"validator rejected: cot marker", "status":"failed"},
        ] * 3)

    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    out = render_text(rep)
    assert "Validator rejection rate" in out
    assert "drift" in out.lower() or "tune" in out.lower()


def test_render_text_sentinel_clean_when_no_signals(tmp_path):
    from audit_health_report import collect_week_report, render_text  # noqa: PLC0415

    _make_audit_fix(tmp_path, "2026-05-05", [
        {"type":"strip_hallucinated_url","section":None,"detail":"applied","status":"applied"},
    ])
    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    out = render_text(rep)
    assert "Sentinel: green" in out


def test_git_log_revert_commits_returns_empty_when_git_fails(tmp_path, monkeypatch):
    """If git log errors (no repo, missing binary), return empty list — never crash."""
    from audit_health_report import _git_log_revert_commits  # noqa: PLC0415

    sd = tmp_path / "sessions"
    sd.mkdir()
    # tmp_path is not a git repo -> git log errors -> empty list.
    out = _git_log_revert_commits(date(2026, 4, 1), sd)
    assert out == []


def test_render_html_escapes_special_chars(tmp_path):
    """Escape any < or > in user-controlled fields that surface in render."""
    from audit_health_report import collect_week_report, render_html  # noqa: PLC0415

    # audit_model_used IS rendered in the per-day table; use it as the
    # injection vector for the escape test.
    _make_audit_fix(tmp_path, "2026-05-05",
                    actions=[{"type":"x","section":None,"detail":"applied","status":"applied"}],
                    audit_model="<script>alert(1)</script>")
    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    html = render_html(rep)
    # Raw < and > from the model name must be escaped wherever it surfaces.
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html  # raw form must NOT appear
    # Outer body tag is not escaped — it's part of the email layout.
    assert "<body" in html


def test_render_html_empty_window_short_circuits(tmp_path):
    """Empty-window path returns the short message, not a full layout."""
    from audit_health_report import collect_week_report, render_html  # noqa: PLC0415

    sd = tmp_path / "sessions"
    sd.mkdir()
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    html = render_html(rep)
    assert "No audit-fix runs in window" in html
    # Should NOT contain a per-day table or summary chips.
    assert "Per day" not in html
    assert "applied" not in html.lower() or "Either" in html  # the only "applied" is in code-tag context


def test_render_html_includes_summary_chips(tmp_path):
    """Happy path includes applied/skipped/failed/reverts chips with values."""
    from audit_health_report import collect_week_report, render_html  # noqa: PLC0415

    _make_audit_fix(tmp_path, "2026-05-05", [
        {"type":"strip_hallucinated_url","section":None,"detail":"applied","status":"applied"},
        {"type":"rerender_empty_with_data","section":"x",
         "detail":"validator rejected: cot marker", "status":"failed"},
    ])
    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    html = render_html(rep)
    # Chip labels (uppercased in the inline CSS via text-transform).
    assert "applied" in html
    assert "failed" in html
    # Numeric values present.
    assert ">1<" in html  # one applied + one failed -> both render as 1
    # Per-day table rendered.
    assert "Per day" in html
    assert "2026-05-05" in html


def test_render_html_sentinel_red_when_reverts_exist(tmp_path, monkeypatch):
    """When a revert commit is found, sentinel uses the alert color."""
    from audit_health_report import collect_week_report, render_html  # noqa: PLC0415
    import audit_health_report as mod  # noqa: PLC0415

    monkeypatch.setattr(mod, "_git_log_revert_commits",
                        lambda since, sd: [("abc1234", "auditor: 2026-05-05 records (reverted — auditor regressed)")])

    _make_audit_fix(tmp_path, "2026-05-05", [
        {"type":"strip_hallucinated_url","section":None,"detail":"applied","status":"applied"},
    ])
    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    html = render_html(rep)
    # Alert color (red #cf222e) appears in the rendered HTML.
    assert "#cf222e" in html
    # Revert commit list rendered.
    assert "abc1234" in html
    assert "auditor regressed" in html


def test_history_covers_window_heuristic():
    """Heuristic: depth >= days * 6 is enough; less may miss reverts."""
    from audit_health_report import _history_covers_window  # noqa: PLC0415

    assert _history_covers_window(None, 7) is True   # unknown -> don't false-warn
    assert _history_covers_window(50, 7) is True     # 50 >= 7*6 = 42
    assert _history_covers_window(42, 7) is True     # exactly equal is enough
    assert _history_covers_window(30, 7) is False    # 30 < 42
    assert _history_covers_window(0, 1) is False     # zero-depth always insufficient


def test_collect_week_report_records_history_depth(tmp_path, monkeypatch):
    """`git_history_depth` and `history_covers_window` populated on the report."""
    from audit_health_report import collect_week_report  # noqa: PLC0415
    import audit_health_report as mod  # noqa: PLC0415

    monkeypatch.setattr(mod, "_git_history_depth", lambda sd: 30)

    _make_audit_fix(tmp_path, "2026-05-05",
                    actions=[{"type":"x","section":None,"detail":"a","status":"applied"}])
    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    assert rep.git_history_depth == 30
    assert rep.history_covers_window is False  # 30 < 7*6


def test_render_text_warns_on_shallow_history(tmp_path, monkeypatch):
    """Shallow-history sentinel surfaces in plain-text render."""
    from audit_health_report import collect_week_report, render_text  # noqa: PLC0415
    import audit_health_report as mod  # noqa: PLC0415

    monkeypatch.setattr(mod, "_git_history_depth", lambda sd: 10)

    _make_audit_fix(tmp_path, "2026-05-05",
                    actions=[{"type":"x","section":None,"detail":"a","status":"applied"}])
    sd = tmp_path / "sessions"
    rep = collect_week_report(sd, days=7, today=date(2026, 5, 6))
    out = render_text(rep)
    assert "Git history depth" in out
    assert "fetch-depth" in out
