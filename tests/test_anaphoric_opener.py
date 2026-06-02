"""Item C (m8d): anaphoric-opener detector.

Briefing 2026-05-30 opened Domestic Sphere with "This swift regulatory move
contrasts..." with no antecedent. _detect_anaphoric_openers catches the
pattern post-stitch; CONTINUATION_RULES carries a prompt-level directive
discouraging it. This test pins the detector + the directive presence.
"""
from __future__ import annotations

import pytest

try:
    from jeeves.write import (
        _detect_anaphoric_openers,
        CONTINUATION_RULES,
    )
except Exception as exc:
    pytest.skip(f"jeeves.write import failed: {exc}", allow_module_level=True)


class TestAnaphoricOpenerDetector:
    """All known violating openers must be caught; clean openers must not fire."""

    def test_detects_this_in_h3_opener(self):
        html = (
            '<h3>The Domestic Sphere</h3>'
            '<p>This swift regulatory move contrasts with the more measured rollout.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert len(out) == 1
        assert "anaphoric_opener" in out[0]
        assert "The Domestic Sphere" in out[0]
        assert "This" in out[0]

    def test_detects_that_in_h2_opener(self):
        html = (
            '<h2>The Wider World</h2>'
            '<p>That announcement, made yesterday, sent markets reeling.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert len(out) == 1
        assert "That" in out[0]

    def test_detects_such_in_h3_opener(self):
        html = (
            '<h3>Section</h3>'
            '<p>Such a development was scarcely predictable.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert len(out) == 1
        assert "Such" in out[0]

    def test_detects_these_in_opener(self):
        html = (
            '<h3>Section</h3>'
            '<p>These items, taken together, suggest a pattern.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert len(out) == 1

    def test_detects_the_above(self):
        html = (
            '<h3>Section</h3>'
            '<p>The above events have reshaped expectations.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert len(out) == 1

    def test_clean_opener_does_not_fire(self):
        html = (
            '<h3>The Domestic Sphere</h3>'
            '<p>The Edmonds City Council voted yesterday to pass a new ordinance.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert out == []

    def test_multiple_violations_caught(self):
        html = (
            '<h2>Wider World</h2>'
            '<p>This announcement was unexpected.</p>'
            '<h3>Local</h3>'
            '<p>Such things rarely happen in Edmonds.</p>'
            '<h3>Talk of the Town</h3>'
            '<p>At the risk of...</p>'  # clean opener
        )
        out = _detect_anaphoric_openers(html)
        assert len(out) == 2

    def test_anaphoric_INSIDE_section_does_not_fire(self):
        """Only first-paragraph violations count. 'This' inside a
        non-first paragraph has its antecedent (the preceding paragraph)."""
        html = (
            '<h3>Section</h3>'
            '<p>The Edmonds City Council voted yesterday.</p>'
            '<p>This vote followed weeks of public consultation.</p>'
        )
        out = _detect_anaphoric_openers(html)
        assert out == []

    def test_empty_input_safe(self):
        assert _detect_anaphoric_openers("") == []
        assert _detect_anaphoric_openers(None) == []  # type: ignore[arg-type]

    def test_violation_string_format(self):
        """Violation string format is parseable for downstream tooling."""
        html = (
            '<h3>The Domestic Sphere</h3>'
            '<p>This swift regulatory move follows the latest council vote.</p>'
        )
        v = _detect_anaphoric_openers(html)[0]
        # Format: "anaphoric_opener:<heading>:<opener>"
        parts = v.split(":")
        assert parts[0] == "anaphoric_opener"
        assert len(parts) >= 3

    def test_case_insensitive_detection(self):
        html = '<h3>S</h3><p>this small word should still trip the detector.</p>'
        out = _detect_anaphoric_openers(html)
        assert len(out) == 1


class TestContinuationRulesDirective:
    """The prompt-level directive lives in CONTINUATION_RULES so it reaches
    every part that prepends it (parts 2-9 per PART_INSTRUCTIONS_BY_NAME)."""

    def test_directive_present(self):
        assert "SELF-CONTAINED SECTION OPENERS" in CONTINUATION_RULES

    def test_directive_lists_forbidden_openers(self):
        for w in ("This", "That", "Such", "Said", "These", "The above"):
            assert w in CONTINUATION_RULES, f"forbidden opener {w!r} missing from directive"

    def test_directive_gives_positive_example(self):
        """A 'do this instead' line so the model has something to copy."""
        assert "Edmonds" in CONTINUATION_RULES and "City Council" in CONTINUATION_RULES
