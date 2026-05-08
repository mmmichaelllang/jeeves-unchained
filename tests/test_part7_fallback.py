"""Tests for the PART7 fallback injector (Patch C++).

Covers:
  - Detector flags missing UAP / literary_pick when session has data
  - Synthesizer renders Jeeves-voice paragraphs from session data
  - Injector splices fallbacks before <!-- PART7 END --> marker
  - Both UAP and literary fallbacks can fire in the same draft
  - Empty / unavailable session data is left alone
"""

from __future__ import annotations

import pytest

from jeeves.write import (
    _PART7_END_MARKER,
    _build_literary_fallback_html,
    _build_uap_fallback_html,
    _html_escape,
    _maybe_inject_part7_fallbacks,
    _truncate_to_sentence,
)


# =====================================================================
# helper: html escape + truncate
# =====================================================================

def test_html_escape_basic():
    assert _html_escape("<script>") == "&lt;script&gt;"
    assert _html_escape('"') == "&quot;"
    assert _html_escape("a & b") == "a &amp; b"
    assert _html_escape("") == ""
    assert _html_escape(None) == ""


def test_truncate_short_string_unchanged():
    assert _truncate_to_sentence("Short text.", max_chars=600) == "Short text."


def test_truncate_at_sentence_boundary():
    text = "First sentence. Second sentence. Third sentence."
    out = _truncate_to_sentence(text, max_chars=20)
    assert out == "First sentence."


def test_truncate_hard_cut_with_ellipsis():
    """No sentence boundary in range — falls back to hard cut + ellipsis."""
    text = "x" * 1000
    out = _truncate_to_sentence(text, max_chars=50)
    assert out.endswith("…")
    assert len(out) <= 51  # 50 + the ellipsis


# =====================================================================
# UAP fallback synthesis
# =====================================================================

def test_uap_fallback_empty_when_no_data():
    assert _build_uap_fallback_html({}) == ""
    assert _build_uap_fallback_html({"findings": "", "urls": []}) == ""


def test_uap_fallback_includes_findings_and_link():
    uap = {
        "findings": "Congressional UAP hearings continue with Rep. Anna Luna leading.",
        "urls": ["https://oversight.house.gov/release/luna-uap/"],
    }
    out = _build_uap_fallback_html(uap)
    assert out.startswith("<p>")
    assert out.endswith("</p>")
    assert "disclosure front" in out.lower()
    assert "Anna Luna" in out
    assert 'href="https://oversight.house.gov/release/luna-uap/"' in out
    assert "Source" in out


def test_uap_fallback_escapes_html_in_findings():
    uap = {"findings": "Body with <script>alert(1)</script> embedded.", "urls": []}
    out = _build_uap_fallback_html(uap)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_uap_fallback_url_only_no_findings():
    """When urls exist but findings empty, still render with a generic intro."""
    uap = {"findings": "", "urls": ["https://example.com/uap"]}
    out = _build_uap_fallback_html(uap)
    assert "https://example.com/uap" in out
    assert out.startswith("<p>")


# =====================================================================
# Literary pick fallback synthesis
# =====================================================================

def test_literary_fallback_empty_when_unavailable():
    lit = {"available": False, "title": "x"}
    assert _build_literary_fallback_html(lit) == ""


def test_literary_fallback_empty_when_no_title():
    lit = {"available": True, "title": "", "author": "x"}
    assert _build_literary_fallback_html(lit) == ""


def test_literary_fallback_full_render():
    lit = {
        "available": True,
        "title": "The Brief Wondrous Life of Oscar Wao",
        "author": "Junot Díaz",
        "year": 2007,
        "summary": "Pulitzer-winning novel about a Dominican-American teen.",
        "url": "https://example.com/oscar-wao",
    }
    out = _build_literary_fallback_html(lit)
    assert out.startswith("<p>")
    assert "The Brief Wondrous Life of Oscar Wao" in out
    assert 'href="https://example.com/oscar-wao"' in out
    assert "Junot Díaz" in out
    assert "(2007)" in out
    assert "Pulitzer" in out
    assert "library has placed" in out.lower()


def test_literary_fallback_no_url_uses_em_tag():
    lit = {"available": True, "title": "Book Title", "author": "Author"}
    out = _build_literary_fallback_html(lit)
    assert "<em>Book Title</em>" in out
    assert "href=" not in out


def test_literary_fallback_escapes_html():
    lit = {
        "available": True,
        "title": "Book <em>X</em>",
        "author": 'Author "Q"',
        "url": "https://example.com",
    }
    out = _build_literary_fallback_html(lit)
    assert "<em>X</em>" not in out  # title's <em> is escaped, not raw
    assert "&lt;em&gt;X&lt;/em&gt;" in out
    assert "&quot;Q&quot;" in out


# =====================================================================
# End-to-end injector
# =====================================================================

def _draft_without_uap_or_literary() -> str:
    """A part7 draft that wrote ONLY wearable_ai content."""
    return (
        '<h3>The Commercial Ledger</h3>\n'
        '<p>The wearable AI market shipped two new pendants this week...</p>\n'
        f'{_PART7_END_MARKER}'
    )


def test_injector_no_op_when_session_empty():
    payload = {"uap": {}, "literary_pick": {"available": False}}
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert out == _draft_without_uap_or_literary()
    assert warnings == []


def test_injector_route_b_uap_dropped():
    """ROUTE B: uap has data, draft missing → inject UAP fallback."""
    payload = {
        "uap": {
            "findings": "Congressional UAP hearings continue.",
            "urls": ["https://oversight.house.gov/release/luna/"],
        },
        "uap_has_new": True,
        "literary_pick": {"available": False},
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert "part7_route_b_uap_dropped" in warnings
    assert "part7_uap_fallback_injected" in warnings
    assert "part7_route_a_literary_dropped" not in warnings
    assert "Congressional UAP hearings" in out
    assert out.index("Congressional UAP") < out.index(_PART7_END_MARKER)


def test_injector_route_a_literary_dropped():
    """ROUTE A: uap empty, literary has data, draft missing → inject literary."""
    payload = {
        "uap": {},
        "uap_has_new": False,
        "literary_pick": {
            "available": True,
            "title": "Oscar Wao",
            "author": "Junot Díaz",
            "year": 2007,
            "summary": "Pulitzer winner.",
            "url": "https://example.com/wao",
        },
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert "part7_route_a_literary_dropped" in warnings
    assert "part7_literary_fallback_injected" in warnings
    assert "part7_route_b_uap_dropped" not in warnings
    assert "Oscar Wao" in out
    assert out.index("Oscar Wao") < out.index(_PART7_END_MARKER)


def test_injector_route_b_suppresses_literary_fallback():
    """Critical regression — under ROUTE B, dropping BOTH must NOT inject literary.

    The original "both fallbacks together" behavior produced tonally jarring
    paragraphs. Under ROUTE B (uap has data and uap_has_new=True), only the
    UAP fallback fires; the literary suppression is logged for the weekly
    telemetry report.
    """
    payload = {
        "uap": {
            "findings": "Disclosure debate ongoing in 2026.",
            "urls": ["https://oversight.house.gov/x"],
        },
        "uap_has_new": True,
        "literary_pick": {
            "available": True,
            "title": "Bleak House",
            "author": "Charles Dickens",
            "url": "https://example.com/bleak",
        },
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert "part7_uap_fallback_injected" in warnings
    assert "part7_literary_fallback_injected" not in warnings
    # Suppression flag MUST be logged so telemetry can flag chronic mis-routing.
    assert "part7_route_b_literary_suppressed" in warnings
    assert "Disclosure debate" in out
    assert "Bleak House" not in out


def test_injector_route_a_no_literary_when_unavailable():
    """ROUTE A but literary_pick.available=False → nothing to inject."""
    payload = {
        "uap": {},
        "uap_has_new": False,
        "literary_pick": {"available": False},
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert out == _draft_without_uap_or_literary()
    assert "part7_literary_fallback_injected" not in warnings


def test_injector_uap_has_new_false_routes_to_a_even_with_uap_data():
    """If uap_has_new=False, the prompt expects ROUTE A (literary). The
    injector must follow that even when uap data is present."""
    payload = {
        "uap": {"findings": "Old stale uap.", "urls": ["https://x.test"]},
        "uap_has_new": False,
        "literary_pick": {
            "available": True,
            "title": "The Power Broker",
            "author": "Robert Caro",
            "url": "https://example.com/caro",
        },
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert "part7_route_b_uap_dropped" not in warnings
    assert "part7_uap_fallback_injected" not in warnings
    assert "part7_literary_fallback_injected" in warnings
    assert "The Power Broker" in out


def test_injector_skips_when_uap_already_in_draft():
    draft = (
        '<p>On the disclosure front, Sir, congressional hearings continue.</p>\n'
        f'{_PART7_END_MARKER}'
    )
    payload = {
        "uap": {
            "findings": "More UAP news.",
            "urls": ["https://example.com/uap"],
        },
        "uap_has_new": True,
        "literary_pick": {"available": False},
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(draft, payload, warnings)
    assert out == draft
    assert "part7_route_b_uap_dropped" not in warnings


def test_injector_skips_when_literary_title_in_draft():
    draft = (
        '<p>The library this morning offers Bleak House by Dickens.</p>\n'
        f'{_PART7_END_MARKER}'
    )
    payload = {
        "uap": {},
        "uap_has_new": False,
        "literary_pick": {
            "available": True,
            "title": "Bleak House",
            "author": "Dickens",
        },
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(draft, payload, warnings)
    assert out == draft
    assert "part7_route_a_literary_dropped" not in warnings


def test_injector_appends_when_marker_missing():
    """Without the marker, fallback is appended at end of fragment."""
    draft = '<p>Wearables only.</p>'  # NO end marker
    payload = {
        "uap": {"findings": "UAP body.", "urls": []},
        "uap_has_new": True,
        "literary_pick": {"available": False},
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(draft, payload, warnings)
    assert "UAP body" in out
    assert out.startswith("<p>Wearables only.</p>")
    assert "part7_uap_fallback_injected" in warnings


def test_injector_handles_non_dict_uap_gracefully():
    """Schema regression — uap might be a list or string in malformed sessions."""
    payload = {
        "uap": ["not", "a", "dict"],
        "literary_pick": "also wrong shape",
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    assert out == _draft_without_uap_or_literary()
    assert warnings == []


def test_injector_legacy_session_defaults_uap_has_new_true():
    """Sessions pre-dating uap_has_new field — default to True (ROUTE B).

    This mirrors the behavior in _session_subset which uses .get("uap_has_new", True).
    """
    payload = {
        # NO uap_has_new key at all
        "uap": {
            "findings": "Recent disclosure update.",
            "urls": ["https://example.com/u"],
        },
        "literary_pick": {
            "available": True,
            "title": "Some Book",
            "author": "Some Author",
        },
    }
    warnings: list[str] = []
    out = _maybe_inject_part7_fallbacks(
        _draft_without_uap_or_literary(), payload, warnings,
    )
    # Default ROUTE B: UAP wins, literary suppressed.
    assert "part7_uap_fallback_injected" in warnings
    assert "part7_literary_fallback_injected" not in warnings
    assert "part7_route_b_literary_suppressed" in warnings
