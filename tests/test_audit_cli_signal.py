"""F-009 — proof that audit.py defect counts change between pre-fix and
post-fix briefing states. This is the signal the daily.yml gate consumes.

Hermetic — no LLM calls (use_llm=False). The audit pipeline runs both
detection passes against a tmp briefing file we mutate between calls.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))


def _write_session(tmp_path):
    """Minimal session.json so audit.py loads cleanly."""
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
        "local_news": [],
    }
    (tmp_path / "session-2026-05-06.json").write_text(
        json.dumps(session), encoding="utf-8",
    )


def test_audit_defect_count_drops_when_section_filled(tmp_path):
    """Pre-fix briefing with empty Library Stacks -> audit reports the empty
    section. Post-fix briefing with content under Library Stacks -> audit no
    longer reports it. The gate in daily.yml uses this delta to decide
    whether to keep or revert the auditor's work.
    """
    from audit import run_audit  # noqa: PLC0415

    _write_session(tmp_path)

    pre_html = """<!DOCTYPE html><html><body>
<p>Wednesday morning, Mister Lang. The forecast settles on cloud as its working principle. Andy has flagged something overnight.</p>
<h3>The Domestic Sphere</h3>
<p>Some local content with <a href="https://myedmondsnews.com/x">a link</a>.</p>
<h3>The Library Stacks</h3>
<p></p>
<h3>Talk of the Town</h3>
<p>End.</p>
<div class="signoff">Yours, &c.</div>
</body></html>"""
    (tmp_path / "briefing-2026-05-06.html").write_text(pre_html, encoding="utf-8")

    pre_report = run_audit("2026-05-06", tmp_path, use_llm=False)
    pre_defects = len(pre_report.defects)
    pre_empty = [d for d in pre_report.defects
                 if d.type == "empty_with_data" and d.section == "The Library Stacks"]

    assert pre_empty, (
        "expected at least one empty_with_data defect for Library Stacks; "
        f"got defects: {[d.type for d in pre_report.defects]}"
    )

    # Fill Library Stacks with >=30 words so the empty_with_data detector
    # stops firing on it. The exact words don't matter for the gate logic;
    # only that the section now has enough content.
    post_html = pre_html.replace(
        "<h3>The Library Stacks</h3>\n<p></p>",
        "<h3>The Library Stacks</h3>\n<p>Gilead by Marilynne Robinson is the "
        "literary pick this morning, a 2004 novel of considerable patience "
        "and very little hurry. The butler suspects Mister Lang will "
        "appreciate its refusal to hurry, the rare American novel that "
        "earns its meditative pace honestly rather than by stalling against "
        "an artificial deadline.</p>",
    )
    (tmp_path / "briefing-2026-05-06.html").write_text(post_html, encoding="utf-8")

    post_report = run_audit("2026-05-06", tmp_path, use_llm=False)
    post_defects = len(post_report.defects)
    post_empty = [d for d in post_report.defects
                  if d.type == "empty_with_data" and d.section == "The Library Stacks"]

    assert post_defects < pre_defects, (
        f"defect count did not drop after filling section: pre={pre_defects} "
        f"post={post_defects}; pre.types={[d.type for d in pre_report.defects]}; "
        f"post.types={[d.type for d in post_report.defects]}"
    )
    assert not post_empty, (
        f"Library Stacks still flagged as empty after fill: {post_empty!r}"
    )


def test_audit_defect_count_unchanged_on_no_op_change(tmp_path):
    """Cosmetic-only change to briefing must NOT drop defect count. The gate
    must distinguish 'auditor did real work' from 'auditor wrote whitespace'.
    """
    from audit import run_audit  # noqa: PLC0415

    _write_session(tmp_path)

    pre_html = """<!DOCTYPE html><html><body>
<p>Wednesday morning, Mister Lang. The forecast settles on cloud as its working principle. Andy has flagged something overnight.</p>
<h3>The Domestic Sphere</h3>
<p>Some local content with <a href="https://myedmondsnews.com/x">a link</a>.</p>
<h3>The Library Stacks</h3>
<p></p>
<h3>Talk of the Town</h3>
<p>End.</p>
<div class="signoff">Yours, &c.</div>
</body></html>"""
    (tmp_path / "briefing-2026-05-06.html").write_text(pre_html, encoding="utf-8")
    pre_count = len(run_audit("2026-05-06", tmp_path, use_llm=False).defects)

    post_html = pre_html.replace("</body>", "\n</body>")
    (tmp_path / "briefing-2026-05-06.html").write_text(post_html, encoding="utf-8")
    post_count = len(run_audit("2026-05-06", tmp_path, use_llm=False).defects)

    assert post_count >= pre_count, (
        f"cosmetic change unexpectedly dropped defect count: pre={pre_count} "
        f"post={post_count}"
    )
