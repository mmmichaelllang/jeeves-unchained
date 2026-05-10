"""B handlers (sprint-22 follow-up) — three deterministic / LLM-gated fixes.

1. fix_signoff_wrong (postprocess) — surgical inner-text replacement when
   signoff div has wrong text but the safety inject's "div absent" guard
   skips. Run-1 of 2026-05-09 hit this: div present, text "Your faithful
   Butler", safety inject did nothing because div existed.

2. fix_day_name_wrong (postprocess) — deterministic day-name validator.
   Run-1 of 2026-05-09 said "Tuesday, 09 May 2026" when the date was
   Saturday. Replaces wrong day-name token in greeting region (first
   ~2000 chars) when it sits near a date numeral.

3. fix_missing_sections ENHANCEMENT (audit_fix) — when missing-section
   defect fires for a section whose mapped sectors have data, render the
   real content via the F-001-validated LLM rescue path; fall back to
   empty-feed placeholder only on data-absence or LLM failure.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from jeeves.write import postprocess_html
from jeeves.testing.mocks import canned_session
from jeeves.schema import SessionModel


def _session(d: date = date(2026, 5, 9)) -> SessionModel:
    return SessionModel.model_validate(canned_session(d))


# =====================================================================
# fix_signoff_wrong — surgical inner-text replace
# =====================================================================

def test_signoff_div_with_wrong_inner_text_replaced():
    """2026-05-09 run-1 — signoff div present but inner text wrong.
    Safety-inject's 'div absent' guard skipped → wrong text shipped.
    Surgical replace closes the gap."""
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<p>Body content with at least some words for word count.</p>'
        '<div class="signoff"><p>Your faithful Butler,<br/>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session())
    assert "Your reluctantly faithful Butler" in result.html
    # The wrong adjective form must not survive.
    assert ">Your faithful Butler," not in result.html


def test_signoff_div_with_completely_alien_inner_replaced():
    """Pathological case — signoff div has totally non-canonical text.
    Surgical replace must still produce canonical output."""
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<p>Body content with at least some words for word count.</p>'
        '<div class="signoff"><p>Cheers from your AI assistant!</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session())
    assert "Your reluctantly faithful Butler" in result.html
    assert "AI assistant" not in result.html


# =====================================================================
# fix_day_name_wrong — postprocess greeting day-name validator
# =====================================================================

def test_wrong_day_name_in_greeting_corrected():
    """Run-1 — greeting said 'Tuesday, 09 May 2026' on a Saturday."""
    # 2026-05-09 is Saturday — verify before testing.
    assert date(2026, 5, 9).strftime("%A") == "Saturday"
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<h1>Tuesday, 09 May 2026</h1>'
        '<p>Good morning, Sir. Body content with at least some words.</p>'
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session(date(2026, 5, 9)))
    assert "Saturday" in result.html
    assert "Tuesday, 09 May 2026" not in result.html
    assert any(
        w.startswith("day_name_wrong:Tuesday->Saturday")
        for w in result.quality_warnings
    )


def test_correct_day_name_left_alone():
    """When greeting day-name matches session.date, no rewrite."""
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<h1>Saturday, 9 May 2026</h1>'
        '<p>Good morning, Sir. Body content with at least some words.</p>'
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session(date(2026, 5, 9)))
    assert "Saturday" in result.html
    assert not any(
        w.startswith("day_name_wrong:") for w in result.quality_warnings
    )


def test_body_day_mention_not_rewritten():
    """A day name in body prose ('on Tuesday last week') without nearby
    date numerals must NOT be rewritten as Saturday — only the greeting
    region is in scope."""
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<h1>Saturday, 9 May 2026</h1>'
        '<p>Good morning, Sir. Body content with at least some words. '
        'On Tuesday last week the council met to discuss a matter.</p>'
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session(date(2026, 5, 9)))
    # 'On Tuesday last week' must survive — no date numerals nearby.
    assert "On Tuesday last week" in result.html


# =====================================================================
# fix_missing_sections ENHANCEMENT — LLM rescue when data exists
# =====================================================================

def test_missing_section_with_data_attempts_llm_render():
    """When session has data for the missing section's sectors, the
    enhancement path calls _try_render_section_with_data which calls
    the audit LLM. On success the rendered body is spliced (not the
    empty-feed placeholder)."""
    from scripts.audit_fix import fix_missing_sections, FixAction

    html = (
        '<html><body>'
        '<h3>The Domestic Sphere</h3><p>local content</p>'
        # Reading Room is MISSING — defect will fire.
        '<h3>The Commercial Ledger</h3><p>commercial content</p>'
        '</body></html>'
    )
    session = {
        "intellectual_journals": [
            {"headline": "The Plough Lectures", "url": "https://aeon.co/x"},
        ],
        "enriched_articles": [
            {"title": "Iran in Winter", "url": "https://nybooks.com/y"},
        ],
    }
    defects = [
        {"type": "missing_section", "section": "The Reading Room"},
    ]
    actions: list[FixAction] = []

    rendered = (
        '<p>Aeon offers a fresh angle on the Plough Lectures, examining '
        'how late-Victorian agrarian scholarship managed to anticipate the '
        'soil-science debates that defined the next half century, while '
        'the New York Review of Books surveys Iran in Winter with '
        'characteristic depth, tracing the diplomatic chain from the '
        '1953 coup through the present cold-war revival in language that '
        'rewards a careful read on a Saturday morning, Sir.</p>'
    )
    with patch(
        "scripts.audit_fix._call_audit_model",
        return_value=(rendered, "qwen/qwen3-next-80b-a3b-instruct:free"),
    ):
        out = fix_missing_sections(html, defects, session, actions)

    assert "The Reading Room" in out
    assert "Plough Lectures" in out  # real-data prose spliced
    assert any(
        a.type == "rerender_missing_section" and a.status == "applied"
        for a in actions
    )


def test_missing_section_no_data_falls_back_to_empty_feed():
    """When session has NO data for the missing section's sectors, the
    LLM rescue is skipped and the empty-feed placeholder is injected
    (preserves prior behavior)."""
    from scripts.audit_fix import fix_missing_sections, FixAction

    html = (
        '<html><body>'
        '<h3>The Domestic Sphere</h3><p>local content</p>'
        '<h3>The Commercial Ledger</h3><p>commercial content</p>'
        '</body></html>'
    )
    session = {
        "intellectual_journals": [],
        "enriched_articles": [],
    }
    defects = [
        {"type": "missing_section", "section": "The Reading Room"},
    ]
    actions: list[FixAction] = []

    with patch("scripts.audit_fix._call_audit_model") as mock_llm:
        out = fix_missing_sections(html, defects, session, actions)
        # No LLM call when no data.
        mock_llm.assert_not_called()

    assert "The Reading Room" in out
    # Some empty-feed placeholder is present — exact text per registry.
    assert any(
        a.type == "rerender_missing_section" and a.status == "skipped"
        for a in actions
    )


def test_missing_section_validator_rejects_cot_falls_back():
    """When the LLM emits chain-of-thought (rejected by F-001 validator),
    rescue path falls back to empty-feed placeholder rather than splicing
    the CoT prose."""
    from scripts.audit_fix import fix_missing_sections, FixAction

    html = (
        '<html><body>'
        '<h3>The Domestic Sphere</h3><p>local content</p>'
        '<h3>The Commercial Ledger</h3><p>commercial content</p>'
        '</body></html>'
    )
    session = {
        "intellectual_journals": [
            {"headline": "x", "url": "https://aeon.co/x"},
        ],
    }
    defects = [
        {"type": "missing_section", "section": "The Reading Room"},
    ]
    actions: list[FixAction] = []

    cot_leak = "Let me think about this. We need to produce a paragraph..."
    with patch(
        "scripts.audit_fix._call_audit_model",
        return_value=(cot_leak, "fake-model"),
    ):
        out = fix_missing_sections(html, defects, session, actions)

    assert "The Reading Room" in out
    # CoT rejected; not spliced into output.
    assert "Let me think" not in out
    assert any(
        a.type == "rerender_missing_section"
        and a.status == "failed"
        and "validator rejected" in a.detail
        for a in actions
    )
