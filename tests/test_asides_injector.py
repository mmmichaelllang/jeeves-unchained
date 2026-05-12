"""Tests for Patch 2 (2026-05-10) — deterministic asides injector.

Coverage:
  - No-op when current_count is already at/above target_count
  - Picks asides from pool, filtered against recently_used
  - Filtered against asides already present in the HTML
  - Skips paragraphs under min_paragraph_words
  - Skips paragraphs inside .newyorker (TOTT verbatim) block
  - Skips paragraphs inside .signoff block
  - Prefers paragraphs with earned anchors (failure, decision, deadline, etc.)
  - Falls back to longest paragraph when no anchors present
  - Returns the actual list of injected asides
  - Handles empty pool / no available phrases gracefully
"""

from __future__ import annotations

import pytest

from jeeves.write import (
    _ASIDE_EARNED_ANCHORS,
    _inject_asides_to_floor,
)


# Synthetic pool keeps tests deterministic and free of profanity noise from
# the real write_system.md parsing path. The actual pool comes from
# `_parse_all_asides()`; we override via the ``pool`` kwarg.
_TEST_POOL = [
    "absolute clusterfuck of biblical proportions, Sir",
    "a total fucking disaster",
    "a proper shitshow",
    "monumental cock-up",
]


def _p(text: str) -> str:
    return f"<p>{text}</p>"


def _long_paragraph(seed: str, words: int = 60) -> str:
    """Build a <p> with at least `words` whitespace-separated tokens.

    The seed text is repeated as needed so the final split count meets
    `words`. Tests assume each paragraph clears the 50-word qualifying
    floor in `_inject_asides_to_floor`.
    """
    seed_tokens = seed.split()
    repeats = (words + len(seed_tokens) - 1) // max(1, len(seed_tokens))
    body = (seed + " ") * repeats
    return _p(body.strip())


# ============================================================================
# No-op cases
# ============================================================================

def test_no_op_when_already_at_floor():
    html = _long_paragraph("the council voted to delay the budget")
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=2, target_count=2,
    )
    assert new_html == html
    assert injected == []


def test_no_op_when_above_floor():
    html = _long_paragraph("the council voted to delay the budget")
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=5, target_count=2,
    )
    assert new_html == html
    assert injected == []


def test_no_op_when_pool_empty():
    html = _long_paragraph("the council voted to delay the budget")
    new_html, injected = _inject_asides_to_floor(
        html, pool=[], current_count=0, target_count=2,
    )
    assert new_html == html
    assert injected == []


def test_no_op_when_no_qualifying_paragraphs():
    """Short paragraphs (<min_paragraph_words) yield no injection sites."""
    html = "<p>short</p>" * 5
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=2,
    )
    assert new_html == html
    assert injected == []


# ============================================================================
# Happy-path injection
# ============================================================================

def test_injects_one_aside_to_reach_floor_of_one():
    html = _long_paragraph("the decision came down today")
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    # Aside text MUST appear in the modified HTML.
    assert injected[0] in new_html


def test_injects_two_asides_to_reach_floor_of_two():
    html = (
        _long_paragraph("the council vote was delayed")
        + _long_paragraph("the wastewater project cost is rising")
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=2,
    )
    assert len(injected) == 2
    for aside in injected:
        assert aside in new_html


def test_partial_rescue_when_only_one_qualifying_paragraph():
    """Target=2 but only one paragraph qualifies → inject one, return partial."""
    html = _long_paragraph("the deadline missed") + "<p>too short</p>"
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=2,
    )
    assert len(injected) == 1


# ============================================================================
# Dedup — recently_used + within-run
# ============================================================================

def test_skips_recently_used_asides():
    html = _long_paragraph("the council voted to delay")
    recently = ["absolute clusterfuck of biblical proportions, Sir"]
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, recently_used=recently,
        current_count=0, target_count=1,
    )
    assert len(injected) == 1
    # The recently-used phrase MUST NOT be picked.
    assert "clusterfuck of biblical proportions" not in injected[0].lower()


def test_skips_asides_already_in_html():
    """An aside present in the existing HTML must not be re-injected."""
    existing_aside = "a proper shitshow"
    html = _long_paragraph(
        f"the council voted to delay. it was, {existing_aside}, on display."
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=2,
    )
    # Injector pulls TWO asides; neither should be the one already present.
    for aside in injected:
        assert aside != existing_aside


def test_no_op_when_all_asides_filtered_out():
    """Every pool phrase is either recently-used or already in HTML."""
    html = _long_paragraph(
        "monumental cock-up was on display today as the council a total fucking disaster voted"
    )
    recently = [
        "absolute clusterfuck of biblical proportions, Sir",
        "a proper shitshow",
    ]
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, recently_used=recently,
        current_count=0, target_count=2,
    )
    assert injected == []


# ============================================================================
# Excluded zones — newyorker + signoff blocks
# ============================================================================

def test_skips_paragraphs_inside_newyorker_block():
    html = (
        '<div class="newyorker">'
        + _long_paragraph("Ellen Burstyn recalls her favourite poetry")
        + "</div>"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert injected == []


def test_skips_newyorker_with_nested_ny_header_div():
    """Regression for 2026-05-11 production defacement.

    The .newyorker block in production contains a child <div class="ny-header">.
    A non-greedy `<div ...>.*?</div>` regex terminates at the FIRST inner
    `</div>` (the ny-header close), leaving the TOTT `<p>` OUTSIDE the
    excluded span. The depth-counted matcher MUST treat the whole .newyorker
    container as exclusion.

    This is the exact structure used by `jeeves/prompts/email_scaffold.html`
    and the OR narrative editor's preserved TOTT block.
    """
    tott_text = _long_paragraph(
        "Ellen Burstyn recalls poetry decades old in her Upper West Side apartment "
        "with Edna St Vincent Millay and Hafez and Maya Angelou"
    )
    html = (
        '<div class="newyorker">'
        + '<div class="ny-header">The New Yorker · Talk of the Town</div>'
        + tott_text
        + "</div>"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    # TOTT paragraph MUST NOT receive an injection.
    assert injected == []
    # And the verbatim text must be byte-identical to the input.
    assert new_html == html


def test_skips_newyorker_with_nested_div_and_eligible_paragraph_before():
    """Eligible paragraph BEFORE the newyorker block gets the aside; the
    TOTT paragraph INSIDE the newyorker (after a nested ny-header div) does
    NOT — even though depth-naive regex would have included it."""
    eligible = _long_paragraph(
        "the council vote was delayed by the wastewater contractor decision"
    )
    tott = _long_paragraph(
        "Ellen Burstyn recalls poetry decades old in her Upper West Side apartment "
        "with Edna St Vincent Millay and Hafez and Maya Angelou"
    )
    html = (
        eligible
        + '<div class="newyorker">'
        + '<div class="ny-header">The New Yorker</div>'
        + tott
        + "</div>"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    # The aside must NOT appear in the newyorker block.
    ny_open = new_html.find('<div class="newyorker">')
    assert injected[0] not in new_html[ny_open:]


def test_skips_paragraphs_inside_signoff_block():
    html = (
        '<div class="signoff">'
        + _long_paragraph("Your reluctantly faithful Butler this is the signoff text")
        + "</div>"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert injected == []


def test_injects_outside_excluded_blocks_only():
    """Eligible paragraph BEFORE the newyorker block gets the aside; the
    paragraph INSIDE newyorker does not."""
    eligible = _long_paragraph("the deadline was missed by the contractor")
    html = (
        eligible
        + '<div class="newyorker">'
        + _long_paragraph("Burstyn recalls Edna St. Vincent Millay's sonnet")
        + "</div>"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    # The aside should appear in the eligible-paragraph region.
    pre_newyorker = new_html.split('<div class="newyorker">')[0]
    assert injected[0] in pre_newyorker


# ============================================================================
# Earned-position preference
# ============================================================================

def test_prefers_anchored_paragraph_over_neutral_paragraph():
    """Same length; anchored paragraph wins."""
    neutral = _long_paragraph("a quiet morning in the village by the bay")
    anchored = _long_paragraph(
        "the council decision was a failure with the budget missing"
    )
    html = neutral + anchored
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    # The aside should land in the anchored paragraph.
    parts = new_html.split("</p>")
    # Find the paragraph containing the aside.
    aside_para = next(p for p in parts if injected[0] in p)
    assert any(a in aside_para.lower() for a in _ASIDE_EARNED_ANCHORS)


def test_anchor_words_constant_is_lowercase():
    """All anchor strings MUST be lowercase since matching is substring-on-lower."""
    for a in _ASIDE_EARNED_ANCHORS:
        assert a == a.lower(), f"Anchor {a!r} is not lowercase"


# ============================================================================
# Output shape + idempotency
# ============================================================================

def test_returned_injected_list_matches_inserted_count():
    html = (
        _long_paragraph("a")
        + _long_paragraph("b")
        + _long_paragraph("c")
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=3,
    )
    assert len(injected) == 3
    # Each aside must appear EXACTLY ONCE in the new HTML.
    for aside in injected:
        assert new_html.count(aside) == 1


def test_idempotent_second_call_after_floor_reached():
    """Running the injector AGAIN with the new count at floor must no-op."""
    html = _long_paragraph("the deadline missed last week")
    once_html, once_injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    twice_html, twice_injected = _inject_asides_to_floor(
        once_html, pool=_TEST_POOL,
        recently_used=once_injected,  # treat what we injected as "recent"
        current_count=1, target_count=1,
    )
    # At floor; no further injection.
    assert twice_injected == []
    assert twice_html == once_html


def test_html_remains_well_formed_after_injection():
    """`<p>...</p>` count is preserved; no orphan tags introduced."""
    html = _long_paragraph("the budget vote was delayed")
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert new_html.count("<p>") == html.count("<p>")
    assert new_html.count("</p>") == html.count("</p>")


# ============================================================================
# 2026-05-11 PR — grammar smoothing + belt-and-suspenders exclusion
# ============================================================================

def test_verb_prefix_aside_uses_It_not_It_is_double_stutter():
    """Asides starting with 'is,', 'is ', 'has ', 'was ' MUST be spliced as
    "[paragraph]. It [aside]." NOT "[paragraph]. It is, is, [aside]."
    (which is the double-stutter regression mode)."""
    pool = ["is, to be blunt, a fucking train-wreck"]
    html = _long_paragraph("the budget vote was delayed")
    new_html, injected = _inject_asides_to_floor(
        html, pool=pool, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    # MUST contain "It is, to be blunt" — single "is".
    assert "It is, to be blunt, a fucking train-wreck." in new_html
    # MUST NOT contain the double stutter.
    assert "It is, is, " not in new_html
    assert "is, is," not in new_html


def test_verb_prefix_has_uses_It_has():
    pool = ["has become a screaming, sentient shit-sandwich"]
    html = _long_paragraph("the budget vote was delayed")
    new_html, injected = _inject_asides_to_floor(
        html, pool=pool, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    assert "It has become a screaming" in new_html
    assert "It is, has" not in new_html


def test_capitalized_noun_aside_stands_alone():
    """Aside starting with a capital letter (e.g. 'A collection of...')
    should be a standalone sentence, not prefixed with 'It is, '."""
    pool = ["A collection of high-functioning fuck-wits"]
    html = _long_paragraph("the budget decision was delayed")
    new_html, injected = _inject_asides_to_floor(
        html, pool=pool, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    assert "A collection of high-functioning fuck-wits." in new_html
    # MUST NOT use the awkward "It is, A" prefix.
    assert "It is, A " not in new_html


def test_lowercase_aside_keeps_It_is_prefix():
    pool = ["absolute bollocks today"]
    html = _long_paragraph("the budget decision was delayed")
    new_html, injected = _inject_asides_to_floor(
        html, pool=pool, current_count=0, target_count=1,
    )
    assert len(injected) == 1
    assert "It is, absolute bollocks today." in new_html


def test_aside_already_ending_with_period_not_double_punctuated():
    """An aside that brings its own period should not get a second one."""
    pool = ["A collection of high-functioning fuck-wits."]
    html = _long_paragraph("the budget decision was delayed")
    new_html, injected = _inject_asides_to_floor(
        html, pool=pool, current_count=0, target_count=1,
    )
    assert "fuck-wits.." not in new_html
    assert "fuck-wits." in new_html


def test_paragraph_ending_with_diamond_glyph_not_double_punctuated():
    """TOTT-style paragraphs end with ♦ — must not become '♦.' before splice."""
    inner = ("the budget decision " * 20).strip() + " ♦"
    html = f"<p>{inner}</p>"
    new_html, _ = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    assert "♦." not in new_html
    # Should still inject after ♦ without adding extra period.
    assert "♦ " in new_html


def test_excludes_marker_bracketed_newyorker_zone_without_div():
    """Belt-and-suspenders: a NEWYORKER_START/END comment-marker span MUST
    be excluded even when no surrounding `<div class="newyorker">` exists."""
    eligible = _long_paragraph("the budget vote was delayed by failure")
    tott_text = _long_paragraph(
        "Ellen Burstyn recalls Edna St Vincent Millay and Hafez and Maya Angelou"
    )
    html = (
        eligible
        + "<!-- NEWYORKER_START -->\n"
        + tott_text
        + "\n<!-- NEWYORKER_END -->"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=2,
    )
    # Eligible paragraph gets one or both injections; TOTT zone gets none.
    ny_start = new_html.find("<!-- NEWYORKER_START -->")
    ny_end = new_html.find("<!-- NEWYORKER_END -->")
    for aside in injected:
        idx = new_html.find(aside)
        assert idx < ny_start or idx > ny_end, (
            f"Aside {aside!r} landed inside NEWYORKER marker span"
        )


def test_excludes_multiple_marker_bracketed_zones():
    """Production briefings have TWO NEWYORKER zones (verbatim + read-link).
    Both must be excluded."""
    eligible = _long_paragraph("the budget decision was delayed by failure")
    html = (
        eligible
        + "<!-- NEWYORKER_START -->\n"
        + _long_paragraph("verbatim TOTT text about Burstyn")
        + "\n<!-- NEWYORKER_END -->\n"
        + "<!-- NEWYORKER_START -->\n"
        + _long_paragraph("read at link section text about more poetry")
        + "\n<!-- NEWYORKER_END -->"
    )
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=2,
    )
    # No injection inside either NEWYORKER zone.
    import re as _re
    for m in _re.finditer(
        r"<!--\s*NEWYORKER_START\s*-->(.*?)<!--\s*NEWYORKER_END\s*-->",
        new_html, _re.DOTALL,
    ):
        zone = m.group(1)
        for aside in injected:
            assert aside not in zone, (
                f"Aside {aside!r} landed inside NEWYORKER zone"
            )


def test_excludes_anchor_tag_body():
    """Injector must NOT splice an aside inside an `<a>` body (link text)."""
    eligible = _long_paragraph("the budget decision was delayed")
    # A paragraph that is JUST an anchor — and is long enough to qualify by
    # word count. The injector should skip it because the anchor span covers
    # the whole paragraph body.
    anchor_para = (
        '<p><a href="https://example.com/x">'
        + ("read this article about the failure of decisions " * 8)
        + "</a></p>"
    )
    html = eligible + anchor_para
    new_html, injected = _inject_asides_to_floor(
        html, pool=_TEST_POOL, current_count=0, target_count=1,
    )
    if injected:
        # Aside must not be inside an anchor body in the output.
        import re as _re
        for m in _re.finditer(r"<a\b[^>]*>(.*?)</a>", new_html, _re.DOTALL):
            body = m.group(1)
            for aside in injected:
                assert aside not in body, (
                    f"Aside {aside!r} landed inside an <a> body"
                )


def test_verb_prefix_constant_is_lowercase():
    """All entries in _ASIDE_VERB_PREFIXES MUST be lowercase since match
    runs against lower()."""
    from jeeves.write import _ASIDE_VERB_PREFIXES
    for p in _ASIDE_VERB_PREFIXES:
        assert p == p.lower(), f"_ASIDE_VERB_PREFIXES entry {p!r} not lowercase"
