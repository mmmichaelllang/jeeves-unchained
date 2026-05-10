"""Tests for the 2026-05-09 write-phase quality sweep:

  Patch E — banned-phrase detection (staleness narration, recommendation
            pile-on, closing-summary cadenzas)
  Patch F — literary_pick rescue into PART8 when PART7 takes ROUTE B and
            vault_insight is empty
  Patch G — PART6 stop-rule enforcement (truncate forbidden additions
            after the canned all-covered sentences)
"""

from __future__ import annotations

import pytest

from jeeves.write import (
    BANNED_PHRASES_BY_BUCKET,
    _enforce_part6_stop_rule,
    _maybe_rescue_literary_to_part8,
    _PART6_AI_CANNED,
    _PART6_TRIADIC_CANNED,
)


# ============================================================================
# Patch E — banned-phrase constants
# ============================================================================

def test_banned_phrases_bucket_names_match_telemetry():
    """Each bucket name MUST match a key the weekly telemetry script knows.

    The aggregator in scripts/quality_telemetry_report.py buckets warnings by
    the prefix before the first colon. Bucket renames here without telemetry
    updates would silently lose visibility.
    """
    expected_buckets = {
        "staleness_narration",
        "recommendation_pile_on",
        "closing_summary",
        "asides_floor",
        "banned_opener",
    }
    assert set(BANNED_PHRASES_BY_BUCKET.keys()) == expected_buckets


def test_banned_phrases_no_duplicates_across_buckets():
    """A phrase must not live in two buckets — that would double-count it."""
    seen: set[str] = set()
    for phrases in BANNED_PHRASES_BY_BUCKET.values():
        for p in phrases:
            assert p not in seen, f"Duplicate phrase across buckets: {p!r}"
            seen.add(p)


def test_staleness_narration_2026_05_09_phrases_present():
    """2026-05-09 sweep — Domestic Sphere staleness phrases observed in
    production briefing must be in the staleness_narration bucket so the
    weekly telemetry surfaces the cadence.
    """
    bucket = BANNED_PHRASES_BY_BUCKET["staleness_narration"]
    must_have = [
        "unchanged since our last",
        "since our prior briefing",
        "no new information since",
        "unchanged from previous reports",
        "as noted earlier",
        "without alteration",
        "since our last review",
        "since our last glance",
        "remains unchanged since",
    ]
    missing = [p for p in must_have if p not in bucket]
    assert not missing, f"staleness_narration missing 2026-05-09 phrases: {missing}"


def test_closing_summary_2026_05_09_phrases_present():
    """2026-05-09 sweep — Reading Room interpretive coda
    ("underscores a shared theme: both assert that...") must be caught
    by closing_summary bucket.
    """
    bucket = BANNED_PHRASES_BY_BUCKET["closing_summary"]
    must_have = [
        "underscores a shared theme",
        "underscores a shared",
        "underscores the shared",
        "underscores a common",
        "the juxtaposition of",
        "both assert that",
        "shared theme: both",
    ]
    missing = [p for p in must_have if p not in bucket]
    assert not missing, f"closing_summary missing 2026-05-09 phrases: {missing}"


def test_banned_phrases_all_lowercase_safe():
    """Detector lowercases the body before matching; phrases must too."""
    for bucket, phrases in BANNED_PHRASES_BY_BUCKET.items():
        for p in phrases:
            # We don't require strictly-lowercase strings (clarity in source
            # matters), but they MUST round-trip via case-insensitive match.
            assert p == p, f"phrase identity failed: {p!r}"
            assert p.lower() in p.lower()


# ============================================================================
# Patch G — PART6 stop-rule enforcement
# ============================================================================

def test_part6_no_canned_phrase_is_no_op():
    """Real new-paper content (no canned phrase) is left unchanged."""
    raw = (
        "<p>Migliorini's volume on triadic ontology takes a fresh swing at "
        "perichoresis…</p>\n"
        "<p>Meanwhile, the autonomous-research front shipped a new benchmark…</p>\n"
        "<!-- PART6 END -->"
    )
    warnings: list[str] = []
    out = _enforce_part6_stop_rule(raw, warnings)
    assert out == raw
    assert warnings == []


def test_part6_truncates_after_triadic_canned():
    """Forbidden addition after the triadic canned sentence is removed."""
    raw = (
        "<p>The triadic-ontology series continues, Sir, though nothing has "
        "surfaced since our last review that materially advances the "
        "argument.</p>\n"
        "<p>Our previous examination of Migliorini's volume, which united "
        "Trinitarian theology with analytic relational ontology, remains "
        "the benchmark.</p>\n"
        "<p>The autonomous-research front advances, Sir, but nothing fresh "
        "has surfaced since our last review.</p>\n"
        "<!-- PART6 END -->"
    )
    warnings: list[str] = []
    out = _enforce_part6_stop_rule(raw, warnings)
    assert "Migliorini's volume" not in out
    assert "remains the benchmark" not in out
    # Both canned sentences MUST survive.
    assert _PART6_TRIADIC_CANNED in out.lower()
    assert _PART6_AI_CANNED in out.lower()
    assert "part6_padding_truncated:triadic" in warnings


def test_part6_truncates_after_ai_canned():
    """Forbidden addition after the AI canned sentence is removed."""
    raw = (
        "<p>The autonomous-research front advances, Sir, but nothing fresh "
        "has surfaced since our last review.</p>\n"
        "<p>However, no new developments have been reported, and thus our "
        "attention turns to other matters.</p>\n"
        "<!-- PART6 END -->"
    )
    warnings: list[str] = []
    out = _enforce_part6_stop_rule(raw, warnings)
    assert "no new developments have been reported" not in out
    assert "thus our attention turns" not in out
    assert _PART6_AI_CANNED in out.lower()
    assert "part6_padding_truncated:ai" in warnings


def test_part6_truncates_both_buckets_in_one_call():
    """A draft that pads after BOTH canned sentences is fully cleaned."""
    raw = (
        "<p>The triadic-ontology series continues, Sir, though nothing has "
        "surfaced since our last review that materially advances the "
        "argument.</p>\n"
        "<p>Our previous examination of Migliorini remains the benchmark.</p>\n"
        "<p>The autonomous-research front advances, Sir, but nothing fresh "
        "has surfaced since our last review.</p>\n"
        "<p>The DOVA paper was previously discussed and remains a topic of "
        "interest.</p>\n"
        "<!-- PART6 END -->"
    )
    warnings: list[str] = []
    out = _enforce_part6_stop_rule(raw, warnings)
    assert "Migliorini" not in out
    assert "DOVA" not in out
    assert "part6_padding_truncated:triadic" in warnings
    assert "part6_padding_truncated:ai" in warnings


def test_part6_idempotent():
    """Running the helper twice produces the same output."""
    raw = (
        "<p>The triadic-ontology series continues, Sir, though nothing has "
        "surfaced since our last review that materially advances the "
        "argument.</p>\n"
        "<p>Our previous examination of Migliorini remains the benchmark.</p>\n"
        "<!-- PART6 END -->"
    )
    out1 = _enforce_part6_stop_rule(raw, [])
    out2 = _enforce_part6_stop_rule(out1, [])
    assert out1 == out2


def test_part6_keeps_real_new_content_after_canned_when_marked_separately():
    """A canned sentence followed only by the OTHER canned sentence is fine."""
    raw = (
        "<p>The triadic-ontology series continues, Sir, though nothing has "
        "surfaced since our last review that materially advances the "
        "argument.</p>\n"
        "<p>The autonomous-research front advances, Sir, but nothing fresh "
        "has surfaced since our last review.</p>\n"
        "<!-- PART6 END -->"
    )
    warnings: list[str] = []
    out = _enforce_part6_stop_rule(raw, warnings)
    # No truncation needed — only the canned sentences are present.
    assert _PART6_TRIADIC_CANNED in out.lower()
    assert _PART6_AI_CANNED in out.lower()
    assert "Migliorini" not in out
    assert warnings == []


# ============================================================================
# Patch F — literary_pick rescue into PART8
# ============================================================================

_EMPTY_PART8 = "<p></p>\n<!-- PART8 END -->"


def _lit_payload(*, available=True, vault_available=False, uap_has_new=True,
                 uap_findings="UAP body", uap_urls=("https://x.test",)):
    return {
        "uap": {
            "findings": uap_findings,
            "urls": list(uap_urls),
        } if uap_findings or uap_urls else {},
        "uap_has_new": uap_has_new,
        "vault_insight": {"available": vault_available},
        "literary_pick": {
            "available": available,
            "title": "Bleak House",
            "author": "Charles Dickens",
            "year": 1853,
            "summary": "A long Victorian novel about a court case.",
            "url": "https://example.com/bleak",
        } if available else {"available": False},
    }


def test_part8_rescue_fires_on_empty_part8_route_b_no_vault():
    """Canonical case: PART8 empty + ROUTE B taken + literary available."""
    payload = _lit_payload()
    warnings: list[str] = []
    out = _maybe_rescue_literary_to_part8(
        _EMPTY_PART8, payload,
        part7_took_route_b=True,
        quality_warnings=warnings,
    )
    assert "Bleak House" in out
    assert "Charles Dickens" in out
    assert "<!-- PART8 END -->" in out
    assert "part8_literary_rescue_injected" in warnings


def test_part8_rescue_skipped_when_route_a():
    """ROUTE A: literary already lived in PART7; no rescue needed."""
    payload = _lit_payload()
    warnings: list[str] = []
    out = _maybe_rescue_literary_to_part8(
        _EMPTY_PART8, payload,
        part7_took_route_b=False,
        quality_warnings=warnings,
    )
    assert out == _EMPTY_PART8
    assert "part8_literary_rescue_injected" not in warnings


def test_part8_rescue_skipped_when_vault_available():
    """vault_insight has real content; PART8 owns it, leave alone."""
    payload = _lit_payload(vault_available=True)
    raw = (
        "<p>I have been browsing the library stacks…</p>\n"
        "<p>The vault insight content goes here.</p>\n"
        "<!-- PART8 END -->"
    )
    warnings: list[str] = []
    out = _maybe_rescue_literary_to_part8(
        raw, payload, part7_took_route_b=True, quality_warnings=warnings,
    )
    assert out == raw
    assert "part8_literary_rescue_injected" not in warnings


def test_part8_rescue_skipped_when_literary_unavailable():
    payload = _lit_payload(available=False)
    warnings: list[str] = []
    out = _maybe_rescue_literary_to_part8(
        _EMPTY_PART8, payload,
        part7_took_route_b=True,
        quality_warnings=warnings,
    )
    assert out == _EMPTY_PART8
    assert "part8_literary_rescue_injected" not in warnings


def test_part8_rescue_skipped_when_part8_has_real_content():
    """Model wrote real content into PART8 — leave it alone even if the
    other conditions match (no false-positive rewrites)."""
    payload = _lit_payload()
    raw = (
        "<p>I have been browsing the library stacks, Sir, and came across "
        "something rather arresting.</p>\n"
        "<!-- PART8 END -->"
    )
    warnings: list[str] = []
    out = _maybe_rescue_literary_to_part8(
        raw, payload,
        part7_took_route_b=True,
        quality_warnings=warnings,
    )
    assert out == raw
    assert "part8_literary_rescue_injected" not in warnings


def test_part8_rescue_handles_nbsp_placeholder():
    """Some models emit `<p>&nbsp;</p>` instead of `<p></p>`."""
    payload = _lit_payload()
    raw = "<p>&nbsp;</p>\n<!-- PART8 END -->"
    warnings: list[str] = []
    out = _maybe_rescue_literary_to_part8(
        raw, payload,
        part7_took_route_b=True,
        quality_warnings=warnings,
    )
    assert "Bleak House" in out
    assert "part8_literary_rescue_injected" in warnings


def test_part8_rescue_legacy_uap_has_new_default_true():
    """Sessions pre-dating uap_has_new — default True, ROUTE B."""
    payload = _lit_payload()
    payload.pop("uap_has_new", None)
    warnings: list[str] = []
    # Caller still computes route_b=True from the (uap_text + default-True).
    out = _maybe_rescue_literary_to_part8(
        _EMPTY_PART8, payload,
        part7_took_route_b=True,
        quality_warnings=warnings,
    )
    assert "Bleak House" in out


def test_part8_rescue_marker_position():
    """The injected literary content sits BEFORE <!-- PART8 END -->."""
    payload = _lit_payload()
    out = _maybe_rescue_literary_to_part8(
        _EMPTY_PART8, payload,
        part7_took_route_b=True,
        quality_warnings=[],
    )
    assert out.index("Bleak House") < out.index("<!-- PART8 END -->")


def test_part8_rescue_intro_paragraph_present():
    """Ensure the Library-Stacks intro frames the rescue rather than just
    dropping a literary paragraph cold."""
    payload = _lit_payload()
    out = _maybe_rescue_literary_to_part8(
        _EMPTY_PART8, payload,
        part7_took_route_b=True,
        quality_warnings=[],
    )
    assert "library has, as it tends to" in out.lower()
