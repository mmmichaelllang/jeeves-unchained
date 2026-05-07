"""Tests for F-001 — _validate_audit_model_output + fix_empty_with_data CoT rejection.

Forensic context: 2026-05-06 nemotron-3-super-120b-a12b emitted reasoning
chain ("We need to produce a paragraph...", "Word count: counting now")
which was spliced verbatim into briefing-2026-05-06.html lines 60-77 by
``scripts/audit_fix.py:fix_empty_with_data``. Splice happens at line 507
with zero structural validation — only a falsy guard on ``text``.

These tests pin the validator contract (4 unit tests + 1 integration test).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


# ---------------------------------------------------------------------------
# Unit: _validate_audit_model_output
# ---------------------------------------------------------------------------


def test_validator_rejects_pure_cot():
    """Pure reasoning prose with no HTML tag must be rejected."""
    from audit_fix import _validate_audit_model_output  # noqa: PLC0415

    text = (
        "We need to produce a paragraph for Talk of the Town using the "
        "data below. Word count: 60-180 words. Let me start by drafting "
        "the opening sentence."
    )
    ok, reason = _validate_audit_model_output(text)
    assert ok is False
    assert "non-html" in reason.lower() or "cot" in reason.lower()


def test_validator_rejects_cot_prefix_then_html():
    """Reasoning prefix before a real <p> tag must be rejected.

    This is the actual May-6 failure shape — nemotron emitted a planning
    paragraph then included some HTML at the end. The current code
    splices the whole thing including the prefix.
    """
    from audit_fix import _validate_audit_model_output  # noqa: PLC0415

    text = (
        "We need to produce a paragraph. Word count: counting now to make "
        "sure I land between 60 and 180 words. Let me think about the "
        "voice — Jeeves is dry, precise, occasionally barbed.\n\n"
        "<p>The Edmonds Comprehensive Plan workshop drew thirty residents "
        "to a discussion of zoning that, in the butler's experience, "
        "rarely improves with public consultation. Mister Lang's interest "
        "in the proceedings is, presumably, professional.</p>"
    )
    ok, reason = _validate_audit_model_output(text)
    assert ok is False
    assert "non-html" in reason.lower()


def test_validator_rejects_html_then_cot_marker():
    """HTML paragraph that contains a CoT marker mid-text must be rejected."""
    from audit_fix import _validate_audit_model_output  # noqa: PLC0415

    text = (
        "<p>The workshop drew thirty residents. The butler observes that "
        "such gatherings rarely improve with public consultation, though "
        "Mister Lang's interest is presumably professional. Word count: "
        "let me check — that should be around 50 words. Adding more.</p>"
    )
    ok, reason = _validate_audit_model_output(text)
    assert ok is False
    assert "cot" in reason.lower() or "marker" in reason.lower()


def test_validator_rejects_too_short():
    """Below word-count floor must be rejected."""
    from audit_fix import _validate_audit_model_output  # noqa: PLC0415

    text = "<p>Too short.</p>"
    ok, reason = _validate_audit_model_output(text)
    assert ok is False
    assert "word" in reason.lower()


def test_validator_accepts_clean_paragraph():
    """A real Jeeves-voice <p>...</p> with adequate length passes."""
    from audit_fix import _validate_audit_model_output  # noqa: PLC0415

    # ~75 words of plausible Jeeves voice — no CoT, starts with <p>.
    text = (
        "<p>The Edmonds Comprehensive Plan workshop drew, by the count of "
        "the Beacon, some thirty residents to a Wednesday-evening "
        "discussion of zoning — a subject that, in the butler's experience, "
        "rarely improves with consultation. Mister Lang's interest in the "
        "proceedings is, one assumes, professional rather than civic, "
        "which is just as well; the alternative would suggest a leisure "
        "deficit of some severity. The packet runs to forty-three pages.</p>"
    )
    ok, reason = _validate_audit_model_output(text)
    assert ok is True, f"clean text rejected: {reason}"
    assert reason == "ok"


def test_validator_accepts_div_or_h_wrapper():
    """Block-level wrappers other than <p> are also acceptable."""
    from audit_fix import _validate_audit_model_output  # noqa: PLC0415

    # 31 words just over min — a div wrapper.
    text = (
        "<div>The Edmonds Comprehensive Plan workshop drew thirty residents "
        "to a Wednesday-evening discussion of zoning, a subject that "
        "rarely improves with consultation. Mister Lang's interest is "
        "presumably professional rather than civic, which is just as well.</div>"
    )
    ok, reason = _validate_audit_model_output(text)
    assert ok is True, f"div wrapper rejected: {reason}"


# ---------------------------------------------------------------------------
# Integration: fix_empty_with_data with stubbed CoT-emitting model
# ---------------------------------------------------------------------------


def _write_briefing_trio(tmp_path, briefing_html, defects):
    """Mirror tests/test_audit_fix._write helper, narrowed for empty_with_data."""
    (tmp_path / "briefing-2026-05-06.html").write_text(briefing_html, encoding="utf-8")
    (tmp_path / "audit-2026-05-06.json").write_text(
        json.dumps({"date": "2026-05-06", "defects": defects}), encoding="utf-8",
    )
    session = {
        "date": "2026-05-06",
        "weather": "Mostly cloudy, high 66°F",
        "literary_pick": {
            "available": True,
            "title": "Gilead",
            "url": "https://example.com/gilead",
            "summary": "A 2004 novel by Marilynne Robinson",
        },
    }
    (tmp_path / "session-2026-05-06.json").write_text(
        json.dumps(session), encoding="utf-8",
    )
    return tmp_path


def test_fix_empty_with_data_rejects_cot_output(tmp_path, monkeypatch):
    """Reproduce the May-6 failure: model returns CoT, fix path must NOT splice it.

    Previously: text spliced verbatim, FixAction status="applied".
    With validator: splice skipped, FixAction status="failed", briefing untouched.
    """
    from audit_fix import run_fix  # noqa: PLC0415
    import audit_fix as fix_mod  # noqa: PLC0415

    html = """<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Library Stacks</h3>
<p></p>
<h3>Talk of the Town</h3>
<p>End.</p>
</body></html>"""
    defects = [{
        "type": "empty_with_data",
        "severity": "high",
        "section": "The Library Stacks",
        "detail": "empty",
        "evidence": {"sectors": ["literary_pick"]},
    }]
    _write_briefing_trio(tmp_path, html, defects)

    cot_output = (
        "We need to produce a paragraph for Library Stacks using the data "
        "below. Word count target: 60-180 words. Let me think about the "
        "voice — Jeeves is dry, precise. <p>Gilead by Marilynne Robinson "
        "is the literary pick today.</p>"
    )
    monkeypatch.setattr(
        fix_mod, "_call_audit_model",
        lambda prompt, system="", max_tokens=600: (cot_output, "stub/reasoning-7b:free"),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")

    report = run_fix("2026-05-06", tmp_path, use_llm=True, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    # CRITICAL: the visible defect from May 6 — reasoning prose in the briefing.
    assert "We need to produce a paragraph" not in out
    assert "Word count target" not in out
    assert "Let me think" not in out

    # Briefing must NOT have been mutated — the original empty <p></p>
    # under <h3>The Library Stacks</h3> is preserved.
    rerender = [a for a in report.actions if a.type == "rerender_empty_with_data"]
    assert len(rerender) == 1
    assert rerender[0].status == "failed", (
        f"expected status='failed' on validator reject, got {rerender[0].status!r}; "
        f"detail={rerender[0].detail!r}"
    )
    assert "validator" in rerender[0].detail.lower()


def test_fix_empty_with_data_accepts_clean_output(tmp_path, monkeypatch):
    """Sanity: clean <p> still splices via the validator — no regression on happy path."""
    from audit_fix import run_fix  # noqa: PLC0415
    import audit_fix as fix_mod  # noqa: PLC0415

    html = """<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Library Stacks</h3>
<p></p>
<h3>Talk of the Town</h3>
<p>End.</p>
</body></html>"""
    defects = [{
        "type": "empty_with_data",
        "severity": "high",
        "section": "The Library Stacks",
        "detail": "empty",
        "evidence": {"sectors": ["literary_pick"]},
    }]
    _write_briefing_trio(tmp_path, html, defects)

    # 35 words, clean <p>, no CoT markers.
    clean_output = (
        "<p>Gilead by Marilynne Robinson is the literary pick this morning, "
        "a 2004 novel of considerable patience. The butler suspects Mister "
        "Lang will appreciate its refusal to hurry — the rare American "
        "novel that earns its meditative pace honestly rather than by stalling.</p>"
    )
    monkeypatch.setattr(
        fix_mod, "_call_audit_model",
        lambda prompt, system="", max_tokens=600: (clean_output, "stub/reasoning-7b:free"),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")

    report = run_fix("2026-05-06", tmp_path, use_llm=True, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    assert "Gilead by Marilynne Robinson" in out
    rerender = [a for a in report.actions if a.type == "rerender_empty_with_data"]
    assert len(rerender) == 1
    assert rerender[0].status == "applied", (
        f"clean output should have applied, got {rerender[0].status!r}"
    )
