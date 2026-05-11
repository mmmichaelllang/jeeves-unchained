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
