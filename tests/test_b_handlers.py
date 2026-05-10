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


def test_url_quality_score_deterministic_fallback():
    """Without cfg+OpenRouter key, scoring falls back to the host-authority
    table. High-authority publications score above the default; unknown
    hosts fall to the default. Homepages and tag pages are penalised."""
    from jeeves.research_sectors import (
        _score_intellectual_journals_url,
        _INTELLECTUAL_JOURNAL_DEFAULT_SCORE,
    )
    s_nybooks = _score_intellectual_journals_url(
        "https://www.nybooks.com/articles/2026/05/28/irans-new-winter"
    )
    assert s_nybooks >= 0.9
    s_aeon = _score_intellectual_journals_url("https://aeon.co/essays/some-piece")
    assert s_aeon >= 0.85
    s_unknown = _score_intellectual_journals_url("https://random-blog.example.com/post")
    assert s_unknown == _INTELLECTUAL_JOURNAL_DEFAULT_SCORE
    s_homepage = _score_intellectual_journals_url("https://www.nybooks.com/")
    assert s_homepage < s_nybooks
    s_tag = _score_intellectual_journals_url("https://aeon.co/tag/philosophy/")
    assert s_tag < s_aeon
    assert _score_intellectual_journals_url("") == 0.0
    assert _score_intellectual_journals_url("not a url") == 0.0


def test_url_quality_llm_judge_used_when_cfg_has_key():
    """When cfg has openrouter_api_key, the LLM judge is consulted FIRST.
    Result is cached per URL within a single process."""
    import jeeves.research_sectors as rs
    from unittest.mock import MagicMock, patch

    cfg = MagicMock()
    cfg.openrouter_api_key = "sk-or-test"
    # Reset cache so this test is hermetic.
    rs._IJ_LLM_SCORE_CACHE.clear()

    call_count = {"n": 0}

    def fake_judge(url, finding, key):
        call_count["n"] += 1
        return 0.77

    with patch.object(rs, "_llm_score_intellectual_journal_url",
                      side_effect=fake_judge):
        s1 = rs._score_intellectual_journals_url(
            "https://aeon.co/essays/test", finding="Long-form essay text",
            cfg=cfg,
        )
        s2 = rs._score_intellectual_journals_url(
            "https://aeon.co/essays/test", finding="Long-form essay text",
            cfg=cfg,
        )
    assert s1 == 0.77
    assert s2 == 0.77
    # Score function called twice, but the LLM judge implementation handles
    # caching internally (caller doesn't re-mock between calls).
    assert call_count["n"] == 2  # outer mock — real cache test below


def test_llm_judge_caches_per_url():
    """LLM judge implementation caches per URL; second call hits cache."""
    import jeeves.research_sectors as rs
    from unittest.mock import MagicMock, patch

    rs._IJ_LLM_SCORE_CACHE.clear()
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "0.83"
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_resp

    with patch("openai.OpenAI", return_value=fake_client):
        s1 = rs._llm_score_intellectual_journal_url(
            "https://aeon.co/essays/x", "essay text", "sk-test",
        )
        s2 = rs._llm_score_intellectual_journal_url(
            "https://aeon.co/essays/x", "different text", "sk-test",
        )
    assert s1 == 0.83
    assert s2 == 0.83
    # Only ONE underlying API call — second hit was cached.
    assert fake_client.chat.completions.create.call_count == 1


def test_llm_judge_returns_none_on_failure():
    """LLM judge returns None on every failure mode (no key, http error,
    non-numeric response). Caller falls back to the deterministic table."""
    import jeeves.research_sectors as rs
    from unittest.mock import MagicMock, patch

    rs._IJ_LLM_SCORE_CACHE.clear()
    # No key.
    assert rs._llm_score_intellectual_journal_url(
        "https://aeon.co/essays/x", "text", "",
    ) is None
    # OpenAI client raises.
    rs._IJ_LLM_SCORE_CACHE.clear()
    with patch("openai.OpenAI", side_effect=RuntimeError("boom")):
        assert rs._llm_score_intellectual_journal_url(
            "https://aeon.co/essays/y", "text", "sk-test",
        ) is None
    # Model returns non-numeric response — all models exhausted.
    rs._IJ_LLM_SCORE_CACHE.clear()
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock()]
    fake_resp.choices[0].message.content = "I'm not sure how to score this."
    fake_client = MagicMock()
    fake_client.chat.completions.create.return_value = fake_resp
    with patch("openai.OpenAI", return_value=fake_client):
        assert rs._llm_score_intellectual_journal_url(
            "https://aeon.co/essays/z", "text", "sk-test",
        ) is None


def test_score_falls_back_to_table_when_llm_returns_none():
    """When the LLM judge returns None, the deterministic host table fires
    (so unknown blogs still score 0.4, NYRB still scores 0.92, etc.)."""
    import jeeves.research_sectors as rs
    from unittest.mock import MagicMock, patch

    cfg = MagicMock()
    cfg.openrouter_api_key = "sk-or-test"
    rs._IJ_LLM_SCORE_CACHE.clear()

    with patch.object(rs, "_llm_score_intellectual_journal_url",
                      return_value=None):
        s_nybooks = rs._score_intellectual_journals_url(
            "https://www.nybooks.com/articles/2026/05/01/foo",
            finding="essay", cfg=cfg,
        )
        s_unknown = rs._score_intellectual_journals_url(
            "https://random.example.com/post",
            finding="text", cfg=cfg,
        )
    assert s_nybooks >= 0.9
    assert s_unknown == rs._INTELLECTUAL_JOURNAL_DEFAULT_SCORE


def test_extract_url_finding_pairs_lists_findings():
    """_extract_url_finding_pairs walks list-of-dicts shape (IJ) and pairs
    each URL with the item's findings prose."""
    from jeeves.research_sectors import _extract_url_finding_pairs
    parsed = [
        {
            "source": "Aeon",
            "findings": "An essay on republican heritage by Sean Irving.",
            "urls": ["https://aeon.co/essays/x", "https://aeon.co/essays/y"],
        },
        {
            "source": "NYRB",
            "findings": "Iran in winter, surveyed across forty years.",
            "urls": ["https://nybooks.com/z"],
        },
    ]
    pairs = _extract_url_finding_pairs(parsed)
    assert ("https://aeon.co/essays/x",
            "An essay on republican heritage by Sean Irving.") in pairs
    assert ("https://aeon.co/essays/y",
            "An essay on republican heritage by Sean Irving.") in pairs
    assert ("https://nybooks.com/z",
            "Iran in winter, surveyed across forty years.") in pairs


def test_url_quality_avg_empty_loses_gate():
    """Empty URL list scores 0.0 — fails the adoption gate vs any non-empty
    competitor by design (prevents adopting a retry that returned nothing)."""
    from jeeves.research_sectors import _avg_score_intellectual_journals
    assert _avg_score_intellectual_journals([]) == 0.0


def test_opener_jaccard_similarity():
    """Jaccard supplies the count-based recurrence signal. Threshold is
    0.30, picked low because heavy paraphrases preserve only a fraction
    of content tokens; the N-gram check catches the rest."""
    from scripts.audit import _opener_jaccard

    a = ("The world has not improved overnight, but it has at least "
         "produced several new opportunities to observe it failing.")
    # Near-paraphrase: swap a few content words.
    b = ("The world has not improved overnight, but it has at least "
         "furnished fresh opportunities to watch it fail.")
    sim_paraphrase = _opener_jaccard(a, b)
    assert sim_paraphrase >= 0.30, f"paraphrase sim={sim_paraphrase}"

    # Genuinely different opener — should fall well below threshold.
    c = ("A clear morning, Sir; sixty-five degrees and the wastewater "
         "plant remains a debacle, the council still arguing.")
    sim_diff = _opener_jaccard(a, c)
    assert sim_diff < 0.30, f"diff sim={sim_diff}"

    # Identical → 1.0.
    assert _opener_jaccard(a, a) == 1.0


def test_opener_shared_ngram_catches_paraphrase():
    """N-gram check catches paraphrases that preserve a distinctive run
    of content words but swap surrounding ones — the failure mode that
    Jaccard alone underweights."""
    from scripts.audit import _shared_ngram

    a = ("The world has not improved overnight, but it has at least "
         "produced several new opportunities to observe it failing.")
    b = ("The world has not improved overnight, but it has at least "
         "furnished fresh opportunities to watch it fail.")
    # Both openers share "world improved overnight ..." (4-gram of content
    # tokens). Helper returns the matched gram.
    matched = _shared_ngram(a, b, 4)
    assert matched is not None
    assert "world" in matched and "improved" in matched and "overnight" in matched

    # Genuinely different opener has no 4-gram in common.
    c = ("A clear morning, Sir; sixty-five degrees and the wastewater "
         "plant remains a debacle, the council still arguing.")
    assert _shared_ngram(a, c, 4) is None


def test_recurring_opener_detector_catches_paraphrase(tmp_path):
    """D10 must catch a paraphrased recurrence (was missed by exact match)."""
    from scripts.audit import detect_recurring_opener, Defect

    sessions = tmp_path
    (sessions / "briefing-2026-05-09.html").write_text(
        '<html><body><p>The world has not improved overnight, but it has '
        'at least produced several new opportunities to observe it '
        'failing.</p></body></html>',
        encoding="utf-8",
    )
    today_html = (
        '<html><body><p>The world has not improved overnight, but it has '
        'at least furnished fresh opportunities to watch it fail.</p>'
        '<p>body</p></body></html>'
    )
    session = {"date": "2026-05-10"}
    defects: list[Defect] = []
    flagged = detect_recurring_opener(today_html, session, sessions, defects)
    assert flagged == 1
    assert defects[0].type == "recurring_opener"
    assert "jaccard_similarity" in defects[0].evidence
    # The evidence must include either a Jaccard >= 0.30 OR a shared 4-gram —
    # both signals are valid recurrence triggers.
    ev = defects[0].evidence
    assert ev["jaccard_similarity"] >= 0.30 or ev.get("shared_ngram")


def test_extract_body_excerpts_skips_greeting_and_signoff():
    """F7 helper pulls excerpts from BODY (post-greeting, pre-signoff)."""
    from scripts.audit_fix import _extract_body_excerpts

    html = (
        '<html><body>'
        '<p>The greeting paragraph the auditor wants to replace.</p>'
        '<h3>The Domestic Sphere</h3>'
        '<p>The Edmonds City Council postponed the vote on Stephanie '
        'Lucash as city administrator until next week.</p>'
        '<p>A new statewide housing law takes effect June 11 requiring '
        'cities to expand homeless housing zones.</p>'
        '<p>Edmonds Police are searching for suspects who assaulted two '
        'women on a local trail.</p>'
        '<p>Fourth body paragraph that should not appear (cap=3).</p>'
        '<div class="signoff"><p>signoff text</p></div>'
        '</body></html>'
    )
    out = _extract_body_excerpts(html, max_paragraphs=3, max_chars=2000)
    assert "greeting paragraph" not in out
    assert "Stephanie Lucash" in out
    assert "housing law" in out
    assert "trail" in out
    # Cap respected.
    assert "Fourth body paragraph" not in out
    # Signoff skipped.
    assert "signoff text" not in out


def test_f7_recurring_opener_prompt_includes_body_excerpts():
    """F7 prompt for a recurring_opener defect must include actual body
    excerpts so the model anchors the rewrite on today's content."""
    from scripts.audit_fix import fix_greeting_incomplete, FixAction
    from unittest.mock import patch

    html = (
        '<html><body>'
        '<p>The world has not improved overnight, but it has at least '
        'furnished fresh opportunities to watch it fail.</p>'
        '<h3>The Domestic Sphere</h3>'
        '<p>Edmonds City Council postponed Stephanie Lucash confirmation.</p>'
        '<p>A statewide housing law takes effect June 11.</p>'
        '<p>Edmonds Police investigating trail assault.</p>'
        '</body></html>'
    )
    defects = [{
        "type": "recurring_opener",
        "severity": "medium",
        "section": "(greeting)",
        "detail": "65% Jaccard similar",
        "evidence": {
            "matches_date": "2026-05-09",
            "opener_preview": "the world has not improved overnight ...",
            "prior_opener_preview": "the world has not improved overnight ...",
            "jaccard_similarity": 0.65,
        },
    }]
    session = {
        "date": "2026-05-10",
        "weather": "65°F partly cloudy",
        "correspondence": {"text": "- [escalation] Andrew Lang: invitation"},
    }
    actions: list[FixAction] = []
    captured: list[str] = []

    def fake_call(prompt, system="", max_tokens=400):
        captured.append(prompt)
        return ('<p>Stephanie Lucash, Sir, has had her confirmation '
                'postponed by the council — a small mercy, given the '
                'wastewater plant continues to leak news as reliably as '
                'effluent. The statewide housing law looms June 11, and '
                'Andrew Lang has extended yet another invitation.</p>',
                "fake-model")

    with patch("scripts.audit_fix._call_audit_model", side_effect=fake_call):
        fix_greeting_incomplete(html, defects, session, actions)

    prompt = captured[0]
    # Body excerpts threaded through.
    assert "Stephanie Lucash" in prompt
    assert "housing law" in prompt
    # Banned-phrase block present.
    assert "FRESHNESS REQUIREMENT" in prompt
    assert "world has not improved overnight" in prompt.lower()
    # Anchor instruction present.
    assert "ANCHOR ON SPECIFIC CONTENT" in prompt
    # Similarity percent communicated.
    assert "65%" in prompt or "Jaccard" in prompt


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
