"""Smoke tests for scripts/audit.py — detector behaviour on synthetic data.

Hermetic — no LLM calls (pass use_llm=False to bypass D7/D9). Tests the
deterministic detectors and verifies the run_audit returns a populated
AuditReport. The 2026-05-06 broken briefing has no fixture in this test
file (it lives under sessions/) but it would surface 14+ defects.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

# Make scripts/ importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _write_session(tmp_path: Path, **overrides) -> Path:
    base = {
        "date": "2026-05-06",
        "status": "complete",
        "weather": "Mostly cloudy, high 66F",
        "correspondence": {
            "found": True,
            "fallback_used": False,
            "text": "- [escalation] Andy: action item\n- [escalation] Mrs. Lang: reply needed",
        },
        "local_news": [
            {"url": "https://myedmondsnews.com/2026/05/council-postpones-vote-on-city",
             "headline": "Council postpones vote"},
        ],
        "intellectual_journals": [
            {"url": "https://aeon.co/essays/some-real-essay", "headline": "An essay"},
        ],
        "ai_systems": {"findings": "", "urls": []},
        "triadic_ontology": {"findings": "", "urls": []},
        "uap": {"findings": "", "urls": []},
        "career": {},
        "family": {},
        "global_news": [],
        "wearable_ai": [],
        "literary_pick": {
            "available": True,
            "title": "Gilead",
            "author": "Marilynne Robinson",
            "url": "https://example.com/gilead",
        },
        "newyorker": {
            "available": True,
            "title": "An Article",
            "url": "https://www.newyorker.com/magazine/2026/05/11/an-article",
            "text": "x" * 1000,
        },
        "dedup": {"covered_urls": [], "covered_headlines": [], "cross_sector_dupes": []},
    }
    base.update(overrides)
    p = tmp_path / "session-2026-05-06.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


def _write_briefing(tmp_path: Path, html: str) -> Path:
    p = tmp_path / "briefing-2026-05-06.html"
    p.write_text(html, encoding="utf-8")
    return p


def _good_briefing() -> str:
    """A briefing with NO defects — every section at least 30 words to
    clear the empty_with_data floor. Greeting includes weather, weekday,
    and named correspondence contacts (Andy + Mrs. Lang) that match the
    session handoff text used in _write_session."""
    return """<!DOCTYPE html><html><head></head><body>
<h1>Briefing</h1>
<p>Good morning, Mister Lang. This Wednesday brings mostly cloudy skies with a high of 66°F shifting through the afternoon. I have classified your inbox this morning, Sir, and there are two escalations awaiting your eye: one from Andy on a group action item, and one from Mrs. Lang on a reply she needs from you.</p>
<h3>The Domestic Sphere</h3>
<p><a href="https://myedmondsnews.com/2026/05/council-postpones-vote-on-city">The Council postponed</a> a vote on a city administrator role this week, Sir, with the matter returning to the chamber next Tuesday for further discussion. Public comment was light. Two seats remain unfilled in the planning commission.</p>
<h3>Beyond the Geofence</h3>
<p>Sir, the global desk turned up nothing of consequence today. Wires from London, Brussels, and Tokyo are unusually quiet, which is itself a tell. We will resume substantive coverage tomorrow when the markets reopen and the legislatures convene.</p>
<h3>The Reading Room</h3>
<p><a href="https://aeon.co/essays/some-real-essay">Aeon publishes a thoughtful essay</a> this week on the linear timeline, tracing how the nineteenth-century Western imagination replaced cyclical and mythic temporality with an arrow that marches inexorably from past to future. Worth your morning, Sir.</p>
<h3>The Specific Enquiries</h3>
<p>Nothing of substance in the deep sectors today, Sir. The triadic literature has gone quiet, the AI systems desk reports no new papers worth your time, and the UAP hearings are in recess. We resume tomorrow when Congress returns from break.</p>
<h3>The Commercial Ledger</h3>
<p>Quiet on the wearable AI front this morning, Sir, and equally so on the broader commercial AI ledger. No new releases, no funding announcements of note, and nothing from the productivity-tools cluster that warrants your attention.</p>
<h3>The Library Stacks</h3>
<p><a href="https://example.com/gilead">Gilead by Marilynne Robinson</a> — your literary pick for today, Sir. A 2004 novel framed as a Reverend\'s letter to his young son, recounting three generations of fathers and sons, a meditation on faith and doubt and the inheritance of war.</p>
<h3>Talk of the Town</h3>
<p>This week\'s Talk of the Town from The New Yorker, Sir: <a href="https://www.newyorker.com/magazine/2026/05/11/an-article">an article</a> on the semiquincentennial and what commemoration tells us about the times in which it is held. The verbatim text follows below.</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Aggregate URL collection
# ---------------------------------------------------------------------------


def test_aggregate_session_urls_walks_all_shapes():
    from audit import aggregate_session_urls

    session = {
        "a": {"urls": ["https://a.com/x", "not-a-url"]},
        "b": [{"url": "https://b.com/y"}, {"link": "https://b.com/z"}],
        "c": {"href": "https://c.com/w"},
        "d": {"available": True, "url": "https://d.com/q"},
    }
    urls = aggregate_session_urls(session)
    assert "https://a.com/x" in urls
    assert "https://b.com/y" in urls
    assert "https://b.com/z" in urls
    assert "https://c.com/w" in urls
    assert "https://d.com/q" in urls
    assert "not-a-url" not in urls


# ---------------------------------------------------------------------------
# D1 hallucinated URLs
# ---------------------------------------------------------------------------


def test_d1_hallucinated_url_homepage_flagged_high(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    _write_briefing(tmp_path, """<h3>The Domestic Sphere</h3>
<p>An <a href="https://www.fakesite.com/">unrelated link</a>.</p>""")
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    halluc = [d for d in report.defects if d.type == "hallucinated_url"]
    assert len(halluc) == 1
    assert halluc[0].severity == "high"
    assert halluc[0].evidence["is_homepage"] is True


def test_d1_archive_urls_allowlisted(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    _write_briefing(tmp_path, _good_briefing().replace(
        '<a href="https://aeon.co/essays/some-real-essay">Aeon</a>',
        '<a href="https://web.archive.org/web/2026/https://aeon.co/essay">Aeon (archived)</a>'
    ))
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    halluc = [d for d in report.defects if d.type == "hallucinated_url"]
    assert len(halluc) == 0


# ---------------------------------------------------------------------------
# D2 empty-with-data
# ---------------------------------------------------------------------------


def test_d2_empty_library_stacks_with_literary_pick(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    # Strip the Library Stacks paragraph entirely, leaving an empty body.
    html = re.sub(
        r'<h3>The Library Stacks</h3>\s*<p>.*?</p>',
        '<h3>The Library Stacks</h3>\n<p></p>',
        _good_briefing(),
        flags=re.DOTALL,
    )
    _write_briefing(tmp_path, html)
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    empty = [d for d in report.defects if d.type == "empty_with_data"]
    assert len(empty) == 1
    assert empty[0].section == "The Library Stacks"


# ---------------------------------------------------------------------------
# D3 missing section
# ---------------------------------------------------------------------------


def test_d3_missing_library_stacks_when_pick_available(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    html = re.sub(
        r'<h3>The Library Stacks</h3>\s*<p>.*?</p>\s*',
        '',
        _good_briefing(),
        flags=re.DOTALL,
    )
    _write_briefing(tmp_path, html)
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    missing = [d for d in report.defects if d.type == "missing_section"]
    assert any(d.section == "The Library Stacks" for d in missing)


# ---------------------------------------------------------------------------
# D4 section order
# ---------------------------------------------------------------------------


def test_d4_section_order_violation(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    # Move Domestic Sphere to the end (the actual 2026-05-06 bug).
    html = _good_briefing()
    m = re.search(r'<h3>The Domestic Sphere</h3>\s*<p>.*?</p>', html, re.DOTALL)
    assert m, "fixture missing Domestic Sphere block"
    domestic_block = m.group(0)
    html = html.replace(domestic_block, "")
    html = html.replace("</body>", domestic_block + "\n</body>")
    _write_briefing(tmp_path, html)
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    order = [d for d in report.defects if d.type == "section_order"]
    assert len(order) == 1


def test_d4_clean_briefing_no_order_defect(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    _write_briefing(tmp_path, _good_briefing())
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    order = [d for d in report.defects if d.type == "section_order"]
    assert len(order) == 0


# ---------------------------------------------------------------------------
# D5 aside repetition
# ---------------------------------------------------------------------------


def test_d5_aside_repetition_overuse(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    asides = "\n".join(
        f"<p>Some news today. A proper, top-tier fucking shambles.</p>"
        for _ in range(8)
    )
    html = f"""<!DOCTYPE html><html><body>
<p>Greeting Mister Lang Wednesday 66°F Andy.</p>
<h3>The Domestic Sphere</h3>
{asides}
<h3>Talk of the Town</h3>
<p>End.</p>
</body></html>"""
    _write_briefing(tmp_path, html)
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    asides_defects = [d for d in report.defects
                      if d.type in ("aside_repetition", "aside_overuse")]
    assert len(asides_defects) >= 1


# ---------------------------------------------------------------------------
# D6 greeting incomplete
# ---------------------------------------------------------------------------


def test_d6_greeting_missing_weather(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    # Strip every temp signal so D6 has nothing to match.
    html = re.sub(r"\d{1,3}\s*°\s*[FfCc]", "the weather", _good_briefing())
    html = html.replace("high of", "")
    _write_briefing(tmp_path, html)
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    miss = [d for d in report.defects if d.type == "greeting_missing_weather"]
    assert len(miss) == 1


def test_d6_clean_greeting_no_defect(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    _write_briefing(tmp_path, _good_briefing())
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    greeting_defects = [d for d in report.defects if d.type.startswith("greeting_")]
    assert len(greeting_defects) == 0


# ---------------------------------------------------------------------------
# D8 dedup violations
# ---------------------------------------------------------------------------


def test_d8_cross_day_overlap(tmp_path):
    from audit import run_audit

    _write_session(tmp_path, dedup={
        "covered_urls": ["https://aeon.co/essays/some-real-essay"],
        "covered_headlines": [],
        "cross_sector_dupes": [],
    })
    _write_briefing(tmp_path, _good_briefing())
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    cross = [d for d in report.defects if d.type == "dedup_cross_day_overlap"]
    assert len(cross) == 1


def test_d8_within_run_url_dupe(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    html = re.sub(
        r'<h3>Beyond the Geofence</h3>\s*<p>.*?</p>',
        '<h3>Beyond the Geofence</h3>\n<p>See <a href="https://aeon.co/essays/some-real-essay">Aeon</a> once more.</p>',
        _good_briefing(),
        flags=re.DOTALL,
    )
    _write_briefing(tmp_path, html)
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    cross = [d for d in report.defects if d.type == "dedup_url_cross_section"]
    assert len(cross) == 1


# ---------------------------------------------------------------------------
# Detector run summary
# ---------------------------------------------------------------------------


def test_run_audit_skips_llm_detectors_with_no_llm(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    _write_briefing(tmp_path, _good_briefing())
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    assert "D7_narrative_flow" in report.detectors_skipped
    assert "D9_writing_quality" in report.detectors_skipped
    assert "D8_dedup" in report.detectors_run


def test_clean_briefing_zero_defects(tmp_path):
    from audit import run_audit

    _write_session(tmp_path)
    _write_briefing(tmp_path, _good_briefing())
    report = run_audit("2026-05-06", tmp_path, use_llm=False)
    assert len(report.defects) == 0
