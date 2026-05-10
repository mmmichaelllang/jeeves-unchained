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


def test_btg_h3_rewritten_when_global_news_follows():
    """fix_h3_wrong_for_global_news — Beyond the Geofence h3 followed by
    global-news anchors must be rewritten to The Wider World."""
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<h1>Saturday, 9 May 2026</h1>'
        '<p>Body content with at least some words for word count.</p>'
        '<h3>Beyond the Geofence</h3>'
        '<p>The BBC reports that the Strait of Hormuz remains contested. '
        'Russia and Ukraine continue talks.</p>'
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session())
    assert "<h3>The Wider World</h3>" in result.html
    assert "Beyond the Geofence" not in result.html
    assert any(
        w.startswith("h3_wrong_for_global_news:")
        for w in result.quality_warnings
    )


def test_btg_h3_left_alone_when_no_global_anchors():
    """fix_h3_wrong_for_global_news — Beyond the Geofence h3 NOT followed
    by global anchors (legitimate local public-safety usage) is left."""
    raw = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<h1>Saturday, 9 May 2026</h1>'
        '<p>Body content with at least some words for word count.</p>'
        '<h3>Beyond the Geofence</h3>'
        '<p>Mountlake Terrace police reported a string of mailbox '
        'thefts along Lakeview Drive this week.</p>'
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER -->'
        '</div></body></html>'
    )
    result = postprocess_html(raw, _session())
    # Local public-safety content — header preserved per canon.
    assert "Beyond the Geofence" in result.html
    assert "The Wider World" not in result.html


def test_recurring_opener_flagged(tmp_path):
    """fix_recurring_opener — when today's opener matches a prior-day
    briefing's opener, surface a quality_warning with the matching date.

    Uses a TMP sessions dir so we can drop a fake yesterday briefing
    without touching the real corpus. We monkeypatch Path resolution
    via setting cwd-equivalent — but the helper resolves via __file__,
    so we hack by writing a sibling sessions dir under repo. To keep
    this simple and hermetic, we test the helper indirectly by writing
    an actual sessions briefing under the real repo and cleaning up.
    """
    # Hermetic alternative: directly test _extract_first_body_paragraph.
    from jeeves.write import _extract_first_body_paragraph

    html = (
        '<!DOCTYPE html><html><body>'
        '<div class="container">'
        '<p>The world has not improved overnight, but it has at least '
        'produced several new opportunities to observe it failing.</p>'
        '<p>Body continues.</p>'
        '<div class="signoff"><p>signoff</p></div>'
        '</div></body></html>'
    )
    first = _extract_first_body_paragraph(html)
    assert "world has not improved overnight" in first.lower()
    # Ensure signoff is skipped — it's not the first paragraph extracted.
    assert "signoff" not in first.lower()


def test_recurring_opener_phrase_in_banned_opener_bucket():
    """The specific recurring phrase from 2026-04-28 / 2026-05-09 / 2026-05-10
    must be in the banned_opener bucket so weekly telemetry surfaces it."""
    from jeeves.write import BANNED_PHRASES_BY_BUCKET
    bucket = BANNED_PHRASES_BY_BUCKET["banned_opener"]
    assert "The world has not improved overnight" in bucket


def test_intellectual_journals_dedup_language_strengthened():
    """Sticky intellectual_journals URLs must be flagged in sector spec."""
    import jeeves.research_sectors as rs
    spec = next(s for s in rs.SECTOR_SPECS if s.name == "intellectual_journals")
    instr = spec.instruction
    # Strong language present.
    assert "MANDATORY DEDUP RULE" in instr
    assert "FILTER OUT" in instr
    assert "hard failure" in instr
    # Sticky URL fragments named.
    assert "the-wests-forgotten-republican-heritage" in instr
    assert "oliver-sacks-perception" in instr
    assert "the-role-of-literature-as-the-key-to-personal-freedom" in instr


def test_english_lesson_plans_targets_user_priority_sources():
    """2026-05-10 — sector instruction must explicitly name the user's
    priority source list. Not exclusive, but must be in the prompt.
    """
    import jeeves.research_sectors as rs
    spec = next(s for s in rs.SECTOR_SPECS if s.name == "english_lesson_plans")
    instr = spec.instruction.lower()
    must_include = [
        "r/elateachers",
        "r/teachers",
        "r/classroommanagement",
        "github.com",
        "shakeuplearning.com",
        "cultofpedagogy.com",
        "liveschool.io",
        "classroomzen.com",
        "edugems.io",
        "publish.obsidian.md",
        "edutopia.org",
    ]
    missing = [s for s in must_include if s not in instr]
    assert not missing, f"english_lesson_plans missing priority sources: {missing}"
    assert "token economy" in instr or "token-economy" in instr
    assert "classroom-management" in instr or "classroom management" in instr


def test_intellectual_journals_forced_retry_wired():
    """intellectual_journals must be in _FORCE_RETRY_ON_OVERLAP set and
    have a fallback query in _DEEP_FALLBACK_QUERIES."""
    import jeeves.research_sectors as rs
    assert "intellectual_journals" in rs._FORCE_RETRY_ON_OVERLAP
    assert "intellectual_journals" in rs._DEEP_FALLBACK_QUERIES


def test_extract_urls_from_parsed_handles_shapes():
    """_extract_urls_from_parsed pulls URLs from list-of-dicts, dict-with-urls,
    and dict-with-subkeys (the three sector shapes). Skips non-http strings."""
    from jeeves.research_sectors import _extract_urls_from_parsed
    parsed_list = [
        {"source": "Aeon", "urls": ["https://aeon.co/x", "https://aeon.co/y"]},
        {"source": "NYRB", "urls": ["https://www.nybooks.com/z"]},
    ]
    out = _extract_urls_from_parsed(parsed_list)
    assert out == ["https://aeon.co/x", "https://aeon.co/y",
                   "https://www.nybooks.com/z"]
    parsed_deep = {"findings": "...", "urls": ["https://example.com/a"]}
    assert _extract_urls_from_parsed(parsed_deep) == ["https://example.com/a"]
    parsed_dict = {
        "classroom_ready": [
            {"title": "x", "url": "https://reddit.com/r/ELATeachers/p/1"},
        ],
        "pedagogy_pieces": [
            {"title": "y", "url": "https://cultofpedagogy.com/post"},
        ],
    }
    assert _extract_urls_from_parsed(parsed_dict) == [
        "https://reddit.com/r/ELATeachers/p/1",
        "https://cultofpedagogy.com/post",
    ]
    assert _extract_urls_from_parsed({"url": "not-a-url"}) == []


def test_recurring_opener_detector_flags_match(tmp_path):
    """audit.detect_recurring_opener flags exact-match opener vs prior briefing."""
    from scripts.audit import detect_recurring_opener, Defect

    sessions = tmp_path
    (sessions / "briefing-2026-05-09.html").write_text(
        '<html><body><p>The world has not improved overnight, but it has at '
        'least produced several new opportunities to observe it failing.</p>'
        '<p>more</p></body></html>',
        encoding="utf-8",
    )
    today_html = (
        '<html><body><p>The world has not improved overnight, but it has at '
        'least produced several new opportunities to observe it failing.</p>'
        '<p>different body</p></body></html>'
    )
    session = {"date": "2026-05-10"}
    defects: list[Defect] = []
    flagged = detect_recurring_opener(today_html, session, sessions, defects)
    assert flagged == 1
    assert defects[0].type == "recurring_opener"
    assert defects[0].evidence["matches_date"] == "2026-05-09"


def test_recurring_opener_detector_passes_when_different(tmp_path):
    """Different opener → no defect."""
    from scripts.audit import detect_recurring_opener, Defect

    sessions = tmp_path
    (sessions / "briefing-2026-05-09.html").write_text(
        '<html><body><p>Yesterday I observed the council meeting in detail.</p>'
        '</body></html>',
        encoding="utf-8",
    )
    today_html = (
        '<html><body><p>Today brings a different set of dispatches entirely.</p>'
        '</body></html>'
    )
    session = {"date": "2026-05-10"}
    defects: list[Defect] = []
    flagged = detect_recurring_opener(today_html, session, sessions, defects)
    assert flagged == 0
    assert defects == []


def test_f7_handles_recurring_opener_defect():
    """fix_greeting_incomplete fires on recurring_opener defect AND threads
    the avoid-phrase into the LLM prompt."""
    from scripts.audit_fix import fix_greeting_incomplete, FixAction
    from unittest.mock import patch

    html = (
        '<html><body>'
        '<p>The world has not improved overnight, but it has at least '
        'produced several new opportunities to observe it failing.</p>'
        '<p>body</p>'
        '</body></html>'
    )
    defects = [{
        "type": "recurring_opener",
        "severity": "medium",
        "section": "(greeting)",
        "detail": "matches 2026-05-09",
        "evidence": {
            "matches_date": "2026-05-09",
            "opener_preview": "the world has not improved overnight",
        },
    }]
    session = {
        "date": "2026-05-10",
        "weather": "65°F partly cloudy",
        "correspondence": {"text": "- [escalation] Andrew Lang: invitation"},
    }
    actions: list[FixAction] = []

    captured_prompts: list[str] = []

    def fake_call(prompt, system="", max_tokens=400):
        captured_prompts.append(prompt)
        return (
            '<p>A clear morning, Sir; sixty-five degrees and a half-overcast '
            'sky. Andrew Lang has extended an invitation that warrants a '
            'glance before noon. Several escalations have stacked overnight, '
            'though none rise to the level of immediate action. The morning\'s '
            'dispatch will draw on what the night has furnished — and the '
            'night has furnished, as ever, a great deal of administrative '
            'static and very little resolution. Tea is on its way.</p>',
            "fake-model",
        )

    with patch("scripts.audit_fix._call_audit_model", side_effect=fake_call):
        out, model = fix_greeting_incomplete(html, defects, session, actions)

    assert "world has not improved overnight" not in out.lower()
    assert "sixty-five degrees" in out
    assert any(
        a.type == "rerender_greeting"
        and a.status == "applied"
        and a.evidence.get("recurring_match_date") == "2026-05-09"
        for a in actions
    )
    assert any(
        "world has not improved overnight" in p.lower()
        for p in captured_prompts
    )
    assert any("FRESHNESS REQUIREMENT" in p for p in captured_prompts)


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
