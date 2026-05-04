"""Tests for the 2026-05-03 dedup + repetition fix sprint.

Covers:
- _strip_continuation_wrapper truncates at first embedded </body>/</html>.
- _strip_part_zero_premature_close truncates Part 1 hallucinated full briefing.
- _validate_part_fragment enforces h3 count budget per part.
- _truncate_to_h3_budget cuts content past the (max+1)-th <h3>.
- _dedup_h3_sections_across_blocks keeps richest copy when same h3 repeats.
- _dedup_paragraphs_across_blocks shingle-Jaccard drops near-duplicates.
- _extract_written_topics now captures single proper nouns + acronyms.
- _trim_session_for_prompt surfaces cross_sector_dupes.
"""

from __future__ import annotations

import pytest

from jeeves.write import (
    _dedup_h3_sections_across_blocks,
    _dedup_paragraphs_across_blocks,
    _extract_written_topics,
    _strip_continuation_wrapper,
    _strip_part_zero_premature_close,
    _truncate_to_h3_budget,
    _validate_part_fragment,
    _trim_session_for_prompt,
)

try:  # SessionModel needs pydantic; skip its tests if missing in this sandbox.
    from jeeves.schema import SessionModel  # noqa: F401
    _HAS_SCHEMA = True
except Exception:
    _HAS_SCHEMA = False


# --- _strip_continuation_wrapper -------------------------------------------


def test_strip_continuation_wrapper_truncates_at_embedded_body_close():
    """Middle-part fragment with embedded </body> + trailing pass content
    should be cut at the first </body> — not just trailing close-tags."""
    fragment = (
        "<h3>My Section</h3><p>real content</p>"
        "</body></html>"
        "<h3>Hallucinated Pass 2</h3><p>extra junk</p>"
    )
    out = _strip_continuation_wrapper(fragment)
    assert "Hallucinated" not in out
    assert "real content" in out
    assert "</body>" not in out.lower()
    assert "</html>" not in out.lower()


def test_strip_continuation_wrapper_keeps_clean_fragment():
    """A fragment with no embedded close tags should be unchanged."""
    fragment = "<h3>Section</h3><p>content</p>"
    out = _strip_continuation_wrapper(fragment)
    assert "<h3>Section</h3>" in out
    assert "<p>content</p>" in out


# --- _strip_part_zero_premature_close --------------------------------------


def test_part_zero_strip_truncates_full_briefing_hallucination():
    """Part 1 should not contain </body> mid-fragment. If it does (model
    wrote a full briefing under Part 1), truncate at that close-tag."""
    part1 = (
        "<!DOCTYPE html><html><body><div class='container'>"
        "<p>Good morning</p>"
        "<h3>Domestic Sphere</h3><p>local news</p>"
        "<h3>Beyond the Geofence</h3><p>global news</p>"
        "<div class='signoff'><p>signoff</p></div>"
        "</div></body></html>"
        "<h3>Domestic Sphere</h3><p>second-pass duplicate</p>"
    )
    out = _strip_part_zero_premature_close(part1)
    assert "second-pass duplicate" not in out
    assert "Good morning" in out


# --- _truncate_to_h3_budget ------------------------------------------------


def test_truncate_to_h3_budget_cuts_excess_sections():
    html = (
        "<h3>Section 1</h3><p>one</p>"
        "<h3>Section 2</h3><p>two</p>"
        "<h3>Section 3</h3><p>three</p>"
    )
    out = _truncate_to_h3_budget(html, max_h3=1)
    assert "Section 1" in out
    assert "Section 2" not in out
    assert "Section 3" not in out


def test_truncate_to_h3_budget_unchanged_when_within_budget():
    html = "<h3>Only</h3><p>fine</p>"
    out = _truncate_to_h3_budget(html, max_h3=2)
    assert out == html


# --- _validate_part_fragment h3 enforcement --------------------------------


def test_validate_part_fragment_truncates_overbudget_part1():
    """Part 1 with 7 h3 sections (full briefing) should be truncated to 1."""
    part1_overflow = (
        "<!DOCTYPE html><html><body>"
        "<h3>Domestic Sphere</h3><p>local</p>"
        "<h3>Beyond the Geofence</h3><p>global</p>"
        "<h3>The Calendar</h3><p>cal</p>"
        "<h3>The Wider World</h3><p>w</p>"
        "<h3>The Reading Room</h3><p>r</p>"
        "<h3>The Specific Enquiries</h3><p>s</p>"
        "<h3>The Commercial Ledger</h3><p>c</p>"
    )
    out, warnings = _validate_part_fragment(0, "part1", part1_overflow, total_parts=9)
    assert any("h3_budget_exceeded" in w for w in warnings)
    h3_count = out.lower().count("<h3")
    assert h3_count <= 1, f"expected <=1 h3 after truncation, got {h3_count}"


def test_validate_part_fragment_within_budget_unchanged():
    """A part within budget should pass without truncation."""
    part2 = "<h3>Domestic Sphere</h3><p>local news, real coverage</p>"
    out, warnings = _validate_part_fragment(1, "part2", part2, total_parts=9)
    assert "h3_budget_exceeded" not in " ".join(warnings)
    assert "Domestic Sphere" in out


# --- _dedup_h3_sections_across_blocks --------------------------------------


def test_dedup_h3_sections_keeps_richest():
    """When same h3 appears in 3 places, keep the section with most anchors."""
    html = (
        '<div class="container">'
        # First copy: 0 anchors, generic.
        '<h3>Domestic Sphere</h3>'
        '<p>The city is doing things, with notable activity in the area.</p>'
        # Second copy: 3 anchors (richest — should be kept).
        '<h3>Domestic Sphere</h3>'
        '<p>The <a href="https://example.com/1">council adopted</a> a plan; '
        '<a href="https://example.com/2">police</a> received funding; '
        '<a href="https://example.com/3">fire dept</a> reorganised.</p>'
        # Third copy: 1 anchor.
        '<h3>Domestic Sphere</h3>'
        '<p>The city <a href="https://example.com/4">approved</a> contracts.</p>'
        '<div class="signoff"><p>end</p></div>'
        '</div>'
    )
    out = _dedup_h3_sections_across_blocks(html)
    assert out.count("<h3>Domestic Sphere</h3>") == 1
    # Richest copy survived (3 anchors).
    assert "council adopted" in out
    assert "police" in out
    assert "fire dept" in out


def test_dedup_h3_sections_preserves_distinct_headers():
    html = (
        '<div class="container">'
        '<h3>Section A</h3><p>aaa</p>'
        '<h3>Section B</h3><p>bbb</p>'
        '<div class="signoff"><p>end</p></div>'
        '</div>'
    )
    out = _dedup_h3_sections_across_blocks(html)
    assert "Section A" in out
    assert "Section B" in out


# --- _dedup_paragraphs_across_blocks (shingle Jaccard) --------------------


def test_paragraph_dedup_keeps_richer_copy():
    """Two near-duplicate paragraphs — keep the one with more anchors."""
    html = (
        "<p>The Iran conflict remains at deadlock with peace talks stalled "
        "over control of the Strait of Hormuz and Iran's nuclear program.</p>"
        "<p>The Iran conflict remains at deadlock with peace talks stalled "
        'over control of the Strait of Hormuz, the <a href="https://bbc.com">'
        "BBC reports</a>, and Iran's nuclear program continues.</p>"
    )
    out = _dedup_paragraphs_across_blocks(html)
    assert out.count("<p>") == 1
    assert "BBC reports" in out  # Richer copy survived.


def test_paragraph_dedup_keeps_distinct():
    html = (
        "<p>Iran conflict update one with details about the strait closure.</p>"
        "<p>The Edmonds council adopted a budget for the next fiscal year.</p>"
    )
    out = _dedup_paragraphs_across_blocks(html)
    assert out.count("<p>") == 2


# --- _extract_written_topics (Phase 5 fix) --------------------------------


def test_extract_topics_captures_single_proper_nouns():
    """Old regex required 2-word capitalized sequences. New version
    captures single proper nouns ≥ 4 letters too."""
    text = "<p>President Trump met with Iran officials in Tehran today.</p>"
    topics = _extract_written_topics(text)
    lower = [t.lower() for t in topics]
    assert "trump" in lower
    assert "iran" in lower
    assert "tehran" in lower


def test_extract_topics_captures_acronyms():
    text = "<p>The OFAC issued an alert; AARO confirmed UAP review.</p>"
    topics = _extract_written_topics(text)
    upper = [t.upper() for t in topics]
    assert "OFAC" in upper
    assert "AARO" in upper
    assert "UAP" in upper


def test_extract_topics_skips_filler_words():
    text = "<p>This morning Mister Lang and Sir Jeeves had tea together.</p>"
    topics = _extract_written_topics(text)
    lower = [t.lower() for t in topics]
    assert "this" not in lower
    assert "mister" not in lower
    assert "jeeves" not in lower


# --- cross_sector_dupes wired through trim ---------------------------------


@pytest.mark.skipif(not _HAS_SCHEMA, reason="pydantic / schema not available")
def test_trim_session_preserves_cross_sector_dupes():
    """cross_sector_dupes used to be computed but discarded by write phase.

    Use minimal SessionModel — only date and dedup matter for this test.
    Other fields take their defaults from the model.
    """
    from jeeves.schema import SessionModel
    session = SessionModel(
        date="2026-05-03",
        dedup={
            "covered_urls": ["https://a.com", "https://b.com"],
            "covered_headlines": ["A", "B"],
            "cross_sector_dupes": [
                "https://shared.com/1",
                "https://shared.com/2",
            ],
        },
    )
    payload = _trim_session_for_prompt(session)
    assert "covered_urls" not in payload["dedup"]
    assert payload["dedup"]["cross_sector_dupes"] == [
        "https://shared.com/1",
        "https://shared.com/2",
    ]
