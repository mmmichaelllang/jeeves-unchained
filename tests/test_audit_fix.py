"""Tests for scripts/audit_fix.py — fix actions on synthetic defects.

Hermetic — no LLM calls (use_llm=False). LLM-backed fixes (F6 + F7) get
their own focused tests via monkey-patching the audit-model resolver.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _write(tmp_path, briefing_html, audit_defects, session_overrides=None):
    """Write the trio of files audit_fix expects and return tmp_path."""
    (tmp_path / "briefing-2026-05-06.html").write_text(briefing_html, encoding="utf-8")
    audit = {
        "date": "2026-05-06",
        "defects": audit_defects,
    }
    (tmp_path / "audit-2026-05-06.json").write_text(json.dumps(audit), encoding="utf-8")
    session = {
        "date": "2026-05-06",
        "weather": "Mostly cloudy, high 66°F",
        "correspondence": {"text": "- [escalation] Andy: action item"},
        "literary_pick": {
            "available": True,
            "title": "Gilead",
            "url": "https://example.com/gilead",
            "summary": "A 2004 novel",
        },
        "intellectual_journals": [
            {"url": "https://aeon.co/x", "headline": "An essay"},
        ],
    }
    if session_overrides:
        session.update(session_overrides)
    (tmp_path / "session-2026-05-06.json").write_text(json.dumps(session), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# F1 — strip hallucinated URLs
# ---------------------------------------------------------------------------


def test_f1_strips_hallucinated_url_keeps_text(tmp_path):
    from audit_fix import run_fix

    html = """<!DOCTYPE html><html><body>
<p>Greeting Wednesday weather Andy.</p>
<h3>The Domestic Sphere</h3>
<p>An <a href="https://fakesite.com/">unrelated link</a> appears here.</p>
</body></html>"""
    defects = [{
        "type": "hallucinated_url",
        "severity": "high",
        "section": "The Domestic Sphere",
        "detail": "x",
        "evidence": {"url": "https://fakesite.com/", "is_homepage": True},
    }]
    _write(tmp_path, html, defects)
    report = run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)

    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")
    assert "fakesite.com" not in out
    assert "unrelated link" in out
    assert any(a.type == "strip_hallucinated_url" for a in report.actions)


# ---------------------------------------------------------------------------
# F2 — reorder sections
# ---------------------------------------------------------------------------


def test_f2_reorders_to_canonical(tmp_path):
    from audit_fix import run_fix

    html = """<!DOCTYPE html><html><body>
<p>Greeting.</p>
<h3>Beyond the Geofence</h3>
<p>Global news content.</p>
<h3>The Reading Room</h3>
<p>Reading content.</p>
<h3>The Domestic Sphere</h3>
<p>Domestic content.</p>
</body></html>"""
    defects = [{
        "type": "section_order",
        "severity": "high",
        "section": None,
        "detail": "out of order",
        "evidence": {"actual": ["Beyond the Geofence", "The Reading Room",
                                "The Domestic Sphere"],
                     "canonical": ["The Domestic Sphere", "Beyond the Geofence",
                                   "The Reading Room"]},
    }]
    _write(tmp_path, html, defects)
    report = run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    # Domestic before Geofence before Reading.
    domestic_pos = out.find("The Domestic Sphere")
    geofence_pos = out.find("Beyond the Geofence")
    reading_pos = out.find("The Reading Room")
    assert 0 < domestic_pos < geofence_pos < reading_pos
    assert any(a.type == "reorder_sections" for a in report.actions)


# ---------------------------------------------------------------------------
# F3 — dedup within run
# ---------------------------------------------------------------------------


def test_f3_dedups_url_kept_first(tmp_path):
    from audit_fix import run_fix

    html = """<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Domestic Sphere</h3>
<p>Local <a href="https://x.com/a">link</a> here.</p>
<h3>Beyond the Geofence</h3>
<p>Global <a href="https://x.com/a">link</a> repeated.</p>
</body></html>"""
    defects = [{
        "type": "dedup_url_cross_section",
        "severity": "high",
        "section": None,
        "detail": "url in 2 sections",
        "evidence": {"url": "https://x.com/a",
                     "sections": ["The Domestic Sphere", "Beyond the Geofence"]},
    }]
    _write(tmp_path, html, defects)
    run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    # Only one <a href="https://x.com/a"> remains.
    assert out.count('href="https://x.com/a"') == 1
    # Both "link" texts still present (anchor stripped on duplicate).
    assert out.count("link") == 2


# ---------------------------------------------------------------------------
# F4 — strip repeated asides
# ---------------------------------------------------------------------------


def test_f4_strips_2nd_aside_keeps_first(tmp_path):
    from audit_fix import run_fix

    html = """<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Domestic Sphere</h3>
<p>Some news. A proper, top-tier fucking shambles.</p>
<p>More news. A proper, top-tier fucking shambles.</p>
<p>And more. A proper, top-tier fucking shambles.</p>
</body></html>"""
    defects = [{
        "type": "aside_repetition",
        "severity": "medium",
        "section": None,
        "detail": "x",
        "evidence": {"template": "a proper, top-tier fucking shambles.",
                     "count": 3},
    }]
    _write(tmp_path, html, defects)
    run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    # First survives, repeats stripped.
    assert out.count("A proper, top-tier fucking shambles") == 1
    # Other prose preserved.
    assert "Some news" in out
    assert "More news" in out
    assert "And more" in out


# ---------------------------------------------------------------------------
# F5 — inject missing section
# ---------------------------------------------------------------------------


def test_f5_injects_library_stacks_when_missing(tmp_path):
    from audit_fix import run_fix

    html = """<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Reading Room</h3>
<p>Content.</p>
<h3>Talk of the Town</h3>
<p>Talk content.</p>
</body></html>"""
    defects = [{
        "type": "missing_section",
        "severity": "high",
        "section": "The Library Stacks",
        "detail": "x",
        "evidence": {"sectors": ["literary_pick"]},
    }]
    _write(tmp_path, html, defects)
    run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    assert "<h3>The Library Stacks</h3>" in out
    # Inserted between Reading Room and Talk of the Town.
    reading_pos = out.find("The Reading Room")
    library_pos = out.find("The Library Stacks")
    talk_pos = out.find("Talk of the Town")
    assert reading_pos < library_pos < talk_pos


# ---------------------------------------------------------------------------
# F6 — empty_with_data via stub LLM
# ---------------------------------------------------------------------------


def test_f6_rerenders_empty_with_data_via_stub(tmp_path, monkeypatch):
    from audit_fix import run_fix
    import audit_fix as fix_mod

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
    _write(tmp_path, html, defects)

    monkeypatch.setattr(
        fix_mod, "_call_audit_model",
        lambda prompt, system="", max_tokens=2048:
            ("<p>Gilead by Marilynne Robinson is the literary pick this "
             "morning, a 2004 novel of considerable patience. The butler "
             "suspects Mister Lang will appreciate its refusal to hurry — "
             "the rare American novel that earns its meditative pace "
             "honestly rather than by stalling.</p>",
             "stub/reasoning-7b:free"),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")

    report = run_fix("2026-05-06", tmp_path, use_llm=True, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")

    assert "Gilead by Marilynne Robinson" in out
    rerender = [a for a in report.actions if a.type == "rerender_empty_with_data"]
    assert len(rerender) == 1
    assert rerender[0].status == "applied"
    assert rerender[0].evidence["model"] == "stub/reasoning-7b:free"


# ---------------------------------------------------------------------------
# F7 — greeting rerender via stub LLM
# ---------------------------------------------------------------------------


def test_f7_rerenders_greeting_via_stub(tmp_path, monkeypatch):
    from audit_fix import run_fix
    import audit_fix as fix_mod

    html = """<!DOCTYPE html><html><body>
<p>Greet placeholder.</p>
<h3>The Domestic Sphere</h3>
<p>Content.</p>
</body></html>"""
    defects = [{
        "type": "greeting_missing_weather",
        "severity": "medium",
        "section": "(greeting)",
        "detail": "x",
        "evidence": {"weather_preview": "high 66°F"},
    }]
    _write(tmp_path, html, defects)

    monkeypatch.setattr(
        fix_mod, "_call_audit_model",
        lambda prompt, system="", max_tokens=2048:
            ("<p>Wednesday morning, Mister Lang. The forecast settles on "
             "cloud as its working principle, with a high near sixty-six "
             "and very little wind worth mentioning. Andy has flagged "
             "something overnight that will want attention before the "
             "third coffee, though not, the butler suspects, before the "
             "first.</p>",
             "stub/reasoning-7b:free"),
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake")

    report = run_fix("2026-05-06", tmp_path, use_llm=True, dry_run=False)
    out = (tmp_path / "briefing-2026-05-06.html").read_text(encoding="utf-8")
    assert "Greet placeholder" not in out
    assert "Wednesday morning" in out
    assert "sixty-six" in out


# ---------------------------------------------------------------------------
# Skip behavior when no defects
# ---------------------------------------------------------------------------


def test_no_defects_no_actions(tmp_path):
    from audit_fix import run_fix

    html = "<!DOCTYPE html><html><body><p>fine</p></body></html>"
    _write(tmp_path, html, [])
    report = run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)
    assert len(report.actions) == 0
    assert report.pre_fix_chars == report.post_fix_chars


def test_no_llm_skips_f6_f7(tmp_path):
    from audit_fix import run_fix

    html = """<!DOCTYPE html><html><body>
<p>Greet.</p>
<h3>The Library Stacks</h3>
<p></p>
</body></html>"""
    defects = [{
        "type": "empty_with_data",
        "severity": "high",
        "section": "The Library Stacks",
        "detail": "empty",
        "evidence": {},
    }]
    _write(tmp_path, html, defects)
    report = run_fix("2026-05-06", tmp_path, use_llm=False, dry_run=False)
    assert not any(a.type.startswith("rerender_") for a in report.actions)
