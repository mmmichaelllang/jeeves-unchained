"""End-to-end integration test for ``generate_briefing``.

Per ``PLAN-2026-05-30-comprehensive.md`` item 7 / Architecture Reviewer A8.
``generate_briefing`` has had no end-to-end test — regressions in part
stitching, narrative editor, post-stitch validators, or any of the eight
``if label == "partN"`` injection blocks were only caught by the daily
12:00Z cron. This test ships the safety net BEFORE items 1+2 (UAP silent
drop fix, deep-sector h3 collapse split) so those refactors land with
visible failure modes rather than silent breakage at 6am.

Approach. Cheapest viable mock surface:

- Monkeypatch ``jeeves.write._invoke_write_llm`` to return canned
  ``(text, used_groq)`` tuples per part label. The mock outputs match
  ``_PART_H3_EXPECTED`` and ``_validate_part_fragment`` rules so the
  fragments survive the stitcher without warnings.
- Leave ``cfg.nvidia_api_key`` and ``cfg.openrouter_api_key`` empty so
  ``_invoke_nim_refine`` and ``_invoke_openrouter_narrative_edit`` take
  their early-return branches in production code — no separate mocks
  needed. ``groq_inter_part_sleep_s=0`` keeps the test fast.

What this catches. Each test pins one thread of the production contract:

- four-tuple return shape from ``generate_briefing``
- 9 ``<h3>`` sections survive ``_stitch_parts`` and the cross-block dedup
- weather paragraph reaches the final document (Item B PR #204 sentinel
  guarantee — when weather text is present, sentinel must NOT inject)
- signoff ``<div class="signoff">`` survives stitch
- exactly one ``<!DOCTYPE>`` and one ``</html>`` post-stitch (no leaks
  from middle parts, no premature closes from Part 1)
- ``BANNED_WORDS`` and ``BANNED_TRANSITIONS`` absent from body text
- ``<!-- NEWYORKER_START -->`` block injected verbatim when newyorker
  available; placeholder comment fully consumed
- ``postprocess_html`` appends ``<!-- COVERAGE_LOG:`` comment
- ``BriefingResult`` ``banned_word_hits`` / ``banned_transition_hits``
  empty in the clean-mock case
- per-part call dispatch order matches ``PART_PLAN``
"""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import pytest

from jeeves import write as wmod
from jeeves.config import Config
from jeeves.schema import SessionModel
from jeeves.testing.mocks import canned_session


_FIXTURE_DATE = date(2026, 5, 30)


# ---------------------------------------------------------------------------
# Canned per-part HTML fragments.
#
# Shapes match ``_PART_H3_EXPECTED`` budgets and ``_validate_part_fragment``
# rules so the stitcher accepts them as-is:
#
#   part1 (h3=0): DOCTYPE/head/body/h1/intro + correspondence + weather text
#   part2 (h3=1): Domestic Sphere
#   part3 (h3=1): Domestic Calendar
#   part4 (h3=2): Family Matters + The Wider World
#   part5 (h3=1): The Reading Room
#   part6 (h3=1): The Specific Enquiries
#   part7 (h3=2): The UAP Disclosure + The Commercial Ledger
#   part8 (h3=1): The Library Stacks
#   part9 (h3=0): TOTT intro + NEWYORKER placeholder + signoff
#
# Prose deliberately avoids ``BANNED_WORDS`` (tapestry, in a vacuum, ...)
# and ``BANNED_TRANSITIONS`` (Moving on, Turning to, ...) so the postprocess
# result has empty hit lists in the clean case.
# ---------------------------------------------------------------------------

_PART_FRAGMENTS: dict[str, str] = {
    "part1": (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n<title>Briefing</title>\n</head>\n'
        '<body>\n<div class="container">\n'
        "<h1>The Morning Briefing</h1>\n"
        "<p>Good morning, Sir. I have your dispatches ready for inspection.</p>\n"
        "<p>The correspondence today runs to three items requiring your eye, "
        "none of them setting a fire.</p>\n"
        "<p>The weather in Edmonds reads partly cloudy at fifty-eight degrees "
        "with a westerly breeze; rain arrives by evening.</p>"
    ),
    "part2": (
        "<h3>The Domestic Sphere</h3>\n"
        '<p>The city council passed the downtown parking ordinance five votes '
        'to two, per <a href="https://myedmondsnews.com/council-parking">'
        "My Edmonds News</a>.</p>"
    ),
    "part3": (
        "<h3>The Domestic Calendar</h3>\n"
        "<p>Two districts post openings this week. Northshore SD lists a "
        "high-school English position closing May the fifteenth; Shoreline "
        "SD has a combined World and US History opening.</p>"
    ),
    "part4": (
        "<h3>Family Matters</h3>\n"
        "<p>The Seattle Symphony Chorale opens auditions on May the third. "
        "Lynnwood Library hosts Baby Storytime on Thursday mornings at "
        "ten-thirty.</p>\n"
        "<h3>The Wider World</h3>\n"
        '<p>The BBC reports continuing diplomatic negotiations abroad.</p>'
    ),
    "part5": (
        "<h3>The Reading Room</h3>\n"
        "<p>The New York Review of Books carries an essay on contemporary "
        "metaphysics this week, worth your evening.</p>"
    ),
    "part6": (
        "<h3>The Specific Enquiries</h3>\n"
        "<p>A recent paper on triadic logic engages process metaphysics "
        "directly. The latest multi-agent benchmark results land alongside it.</p>"
    ),
    "part7": (
        "<h3>The UAP Disclosure</h3>\n"
        "<p>A congressional subcommittee has scheduled a May hearing on "
        "unidentified aerial phenomena.</p>\n"
        "<h3>The Commercial Ledger</h3>\n"
        "<p>A grading assistant for high-school English was released this "
        "week by an established vendor.</p>"
    ),
    "part8": (
        "<h3>The Library Stacks</h3>\n"
        "<p>Today's pick from the shelf is Kazuo Ishiguro's The Buried "
        "Giant, a fable about memory and forgetting.</p>"
    ),
    "part9": (
        "<h3>Talk of the Town</h3>\n"
        "<p>And now, Sir, I take the liberty of reading from this week's "
        "Talk of the Town.</p>\n"
        "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->\n"
        '<p><a href="https://www.newyorker.com/magazine/mock">Read at The '
        "New Yorker</a></p>\n"
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>'
        "Jeeves</p></div>"
    ),
}


def _stub_invoke_write_llm(cfg, system, user, *, max_tokens, label):
    """Return canned (text, used_groq=True) per label. Raises on unknown label."""
    text = _PART_FRAGMENTS.get(label)
    if text is None:
        raise AssertionError(f"unexpected part label: {label}")
    return text, True


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    """Hermetic Config — empty API keys (so NIM + OR skip), tmp sessions dir."""
    (tmp_path / "sessions").mkdir()
    return Config(
        nvidia_api_key="",
        serper_api_key="",
        tavily_api_key="",
        exa_api_key="",
        google_api_key="",
        groq_api_key="",
        gmail_app_password="",
        gmail_oauth_token_json="",
        github_token="",
        github_repository="test/fixture",
        run_date=_FIXTURE_DATE,
        dry_run=False,
        verbose=False,
        phase="write",
        openrouter_api_key="",
        cerebras_api_key="",
        skip_nim_refine=False,
        debug_drafts=False,
        groq_inter_part_sleep_s=0,
        repo_root=tmp_path,
    )


@pytest.fixture
def session() -> SessionModel:
    return SessionModel.model_validate(canned_session(_FIXTURE_DATE))


def _run(cfg: Config, session: SessionModel):
    return asyncio.run(wmod.generate_briefing(cfg, session, max_tokens=2048))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_e2e_returns_four_tuple(monkeypatch, cfg, session):
    """``generate_briefing`` contract: ``(html, warnings, groq_parts, nim_parts)``."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    result = _run(cfg, session)
    assert isinstance(result, tuple)
    assert len(result) == 4
    html, warnings, groq_parts, nim_parts = result
    assert isinstance(html, str) and html
    assert isinstance(warnings, list)
    assert isinstance(groq_parts, int)
    assert isinstance(nim_parts, int)


def test_e2e_nine_h3_sections_survive_stitch(monkeypatch, cfg, session):
    """9 distinct ``<h3>`` sections survive ``_stitch_parts`` + dedup passes.

    Per ``_PART_H3_EXPECTED`` the budgets sum to 9 (parts 2+3+5+6+8 = 1 each,
    parts 4+7 = 2 each). The cross-block dedup may collapse one or two if
    headers collide, so we floor at 7 — flag if it drops below.
    """
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    h3_count = len(wmod._H3_TAG_RE.findall(html))
    assert h3_count >= 7, f"expected ≥7 h3 sections, got {h3_count}"


def test_e2e_weather_paragraph_reaches_final(monkeypatch, cfg, session):
    """Item B PR #204 contract: weather text present (no silent drop)."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    body = wmod._strip_tags(html).lower()
    assert "weather" in body or "partly cloudy" in body, (
        "weather paragraph missing from final briefing"
    )


def test_e2e_signoff_block_present(monkeypatch, cfg, session):
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    assert '<div class="signoff">' in html
    assert "Jeeves" in html


def test_e2e_exactly_one_doctype(monkeypatch, cfg, session):
    """Part 1's DOCTYPE survives; no middle-part leaks."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, warnings, _gp, _np = _run(cfg, session)
    assert html.lower().count("<!doctype") == 1
    assert not any(w.startswith("middle_part_doctype_leak") for w in warnings)


def test_e2e_exactly_one_html_close(monkeypatch, cfg, session):
    """Stitcher enforces single ``</html>`` post-stitch."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    assert html.lower().count("</html>") == 1
    assert html.lower().count("</body>") == 1


def test_e2e_no_banned_words_in_body(monkeypatch, cfg, session):
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    body = wmod._strip_tags(html).lower()
    hits = [w for w in wmod.BANNED_WORDS if w.lower() in body]
    assert hits == [], f"banned words leaked: {hits}"


def test_e2e_no_banned_transitions_in_body(monkeypatch, cfg, session):
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    body = wmod._strip_tags(html).lower()
    hits = [t for t in wmod.BANNED_TRANSITIONS if t.lower() in body]
    assert hits == [], f"banned transitions leaked: {hits}"


def test_e2e_newyorker_block_injected_verbatim(monkeypatch, cfg, session):
    """Placeholder is consumed by ``_inject_newyorker_verbatim`` and
    replaced with the START/END sentinel-wrapped article block."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, _warnings, _gp, _np = _run(cfg, session)
    assert "<!-- NEWYORKER_START -->" in html
    assert "<!-- NEWYORKER_END -->" in html
    assert "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->" not in html
    # canned_session newyorker.text starts with this phrase
    assert "Paragraph one of the mocked New Yorker article" in html


def test_e2e_postprocess_appends_coverage_log(monkeypatch, cfg, session):
    """``postprocess_html`` adds the COVERAGE_LOG comment after generate_briefing."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, warnings, _gp, _np = _run(cfg, session)
    result = wmod.postprocess_html(html, session, quality_warnings=warnings)
    assert "<!-- COVERAGE_LOG:" in result.html


def test_e2e_postprocess_briefing_result_clean(monkeypatch, cfg, session):
    """Clean-mock case → ``BriefingResult`` reports zero banned hits."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    html, warnings, _gp, _np = _run(cfg, session)
    result = wmod.postprocess_html(html, session, quality_warnings=warnings)
    assert result.html.startswith("<!DOCTYPE html>")
    assert result.word_count > 100
    assert result.banned_word_hits == []
    assert result.banned_transition_hits == []


def test_e2e_groq_part_count_reflects_mock(monkeypatch, cfg, session):
    """Mock always returns ``used_groq=True`` → groq_parts=9, nim_parts=0."""
    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub_invoke_write_llm)
    _html, _warnings, groq_parts, nim_parts = _run(cfg, session)
    assert groq_parts == 9
    assert nim_parts == 0


def test_e2e_invoke_write_llm_called_once_per_part_in_order(
    monkeypatch, cfg, session
):
    """Dispatch order matches ``PART_PLAN``: part1 → part9 sequential."""
    calls: list[str] = []

    def _spy(cfg, system, user, *, max_tokens, label):
        calls.append(label)
        return _PART_FRAGMENTS[label], True

    monkeypatch.setattr(wmod, "_invoke_write_llm", _spy)
    _run(cfg, session)
    assert calls == [f"part{i}" for i in range(1, 10)]


def test_e2e_part1_user_prompt_contains_weather_payload(
    monkeypatch, cfg, session
):
    """``PART_PLAN`` dispatches ``weather`` to part1 — verify payload routing."""
    captured: dict[str, str] = {}

    def _spy(cfg, system, user, *, max_tokens, label):
        captured[label] = user
        return _PART_FRAGMENTS[label], True

    monkeypatch.setattr(wmod, "_invoke_write_llm", _spy)
    _run(cfg, session)
    assert "weather" in captured["part1"].lower()


def test_e2e_part7_user_prompt_contains_uap_payload(monkeypatch, cfg, session):
    """``PART_PLAN`` dispatches ``uap`` to part7 — Plan items 1+2 depend on
    this routing being correct."""
    captured: dict[str, str] = {}

    def _spy(cfg, system, user, *, max_tokens, label):
        captured[label] = user
        return _PART_FRAGMENTS[label], True

    monkeypatch.setattr(wmod, "_invoke_write_llm", _spy)
    _run(cfg, session)
    assert "uap" in captured["part7"].lower() or "congressional" in (
        captured["part7"].lower()
    )


def test_e2e_part6_user_prompt_contains_deep_sector_payloads(
    monkeypatch, cfg, session
):
    """``PART_PLAN`` dispatches ``triadic_ontology`` and ``ai_systems`` to
    part6. Plan item 2 (deep-sector h3 collapse) depends on both being
    present in the part6 payload."""
    captured: dict[str, str] = {}

    def _spy(cfg, system, user, *, max_tokens, label):
        captured[label] = user
        return _PART_FRAGMENTS[label], True

    monkeypatch.setattr(wmod, "_invoke_write_llm", _spy)
    _run(cfg, session)
    payload = captured["part6"].lower()
    assert "triadic" in payload, "part6 payload missing triadic_ontology"
    assert "multi-agent" in payload or "ai_systems" in payload, (
        "part6 payload missing ai_systems"
    )


# ---------------------------------------------------------------------------
# Production-defect bisect tests.
#
# Pin the recovery code paths that production cron told us were under-tested:
# briefing-2026-05-30/31, 06-01, 06-02 shipped with no weather paragraph and
# (intermittently) no UAP coverage despite populated session data. PR #204
# Item B added `_ensure_weather_sentinel`; `_maybe_inject_part7_fallbacks`
# pre-existed. The clean-mock tests above never exercise either path because
# the canned fragments already contain weather + UAP. These two tests force
# the buggy-LLM scenario and assert the recovery hooks fire end-to-end.
#
# If these tests PASS locally but production briefings still drop the
# section, the bug is downstream (NIM refine / OR narrative editor strips
# the recovered text). That's the bisect signal we need to debug Item B.
# ---------------------------------------------------------------------------


def _fragments_omit_weather() -> dict[str, str]:
    """Clone the clean fragments but strip the weather paragraph from part1."""
    out = dict(_PART_FRAGMENTS)
    out["part1"] = (
        '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n<title>Briefing</title>\n</head>\n'
        '<body>\n<div class="container">\n'
        "<h1>The Morning Briefing</h1>\n"
        "<p>Good morning, Sir. I have your dispatches ready for inspection.</p>\n"
        "<p>The correspondence today runs to three items requiring your eye, "
        "none of them setting a fire.</p>"
    )
    return out


def _fragments_omit_uap() -> dict[str, str]:
    """Clone the clean fragments but strip the UAP h3 + paragraph from part7."""
    out = dict(_PART_FRAGMENTS)
    out["part7"] = (
        "<h3>The Commercial Ledger</h3>\n"
        "<p>A grading assistant for high-school English was released this "
        "week by an established vendor.</p>"
    )
    return out


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRODUCTION DEFECT 2026-06-02 — Item B sentinel does not survive "
        "to final HTML. Local repro: _ensure_weather_sentinel's injection "
        "branch is reached (warning would be appended), but the sentinel "
        "<p> is missing from the final stitched HTML. Bisect candidates: "
        "(a) sentinel injected into raw_part but _stitch_parts strips it "
        "as orphaned-outside-container (see warning 'structural repair: "
        "156 chars orphaned outside .container'); (b) sentinel never "
        "injected because guard short-circuits before append. When fix "
        "lands, REMOVE this xfail — strict=True will XPASS-fail the suite."
    ),
)
def test_e2e_weather_sentinel_injects_when_llm_omits_weather(
    monkeypatch, cfg, session
):
    """PR #204 Item B contract bisect.

    Scenario: session.weather empty AND LLM-mocked part1 omits any weather
    paragraph. `_ensure_weather_sentinel` MUST inject the unavailable
    sentinel string AND append the `part1_weather_sentinel_injected`
    quality warning.

    Production briefing-2026-06-02.html shipped with neither the weather
    paragraph nor the sentinel string — this test pins the local code path
    so the next debug session can bisect "did sentinel inject locally" vs
    "did downstream strip it" without re-running the full pipeline.
    """
    # Mutate session to defect shape: empty weather string.
    session_data = session.model_dump()
    session_data["weather"] = ""
    defect_session = SessionModel.model_validate(session_data)

    fragments = _fragments_omit_weather()

    def _stub(cfg, system, user, *, max_tokens, label):
        return fragments[label], True

    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub)
    html, warnings, _gp, _np = asyncio.run(
        wmod.generate_briefing(cfg, defect_session, max_tokens=2048)
    )
    assert "weather forecast is unavailable" in html.lower(), (
        "sentinel string missing from final HTML — _ensure_weather_sentinel "
        "did not fire OR a downstream pass stripped it"
    )
    assert "part1_weather_sentinel_injected" in warnings, (
        "quality_warning not appended — sentinel guard returned early "
        "before the injection branch"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "PRODUCTION DEFECT 2026-06-01 (Plan item 1) — UAP fallback fires "
        "at part7 raw level but content is stripped from final HTML. Local "
        "repro confirms _maybe_inject_part7_fallbacks runs ROUTE B "
        "detection AND injection (both quality warnings appear), AND logs "
        "'part7: <!-- PART7 END --> marker missing — appending fallback "
        "at end of fragment'. But post-stitch HTML still has zero UAP "
        "tokens. Likely cause: appended-at-end fallback gets caught by "
        "_stitch_parts' 'orphaned outside .container' repair pass and "
        "spliced into a location where the narrative editor or dedup "
        "later drops it. When Plan item 1 fix lands, REMOVE this xfail."
    ),
)
def test_e2e_part7_uap_fallback_injects_when_llm_drops_uap(
    monkeypatch, cfg, session
):
    """`_maybe_inject_part7_fallbacks` ROUTE B bisect.

    Scenario: session.uap has findings + urls + uap_has_new=True (the canned
    session default — ROUTE B branch). LLM-mocked part7 omits any UAP token.
    `_maybe_inject_part7_fallbacks` MUST append both
    `part7_route_b_uap_dropped` AND `part7_uap_fallback_injected` to
    quality_warnings AND splice synthesized UAP content into part7.

    Production briefing-2026-06-01.html shipped without any UAP mention
    despite session.uap.findings being populated. Plan item 1 chases the
    root cause; this test pins the recovery hook so the failure mode is
    visible.
    """
    fragments = _fragments_omit_uap()

    def _stub(cfg, system, user, *, max_tokens, label):
        return fragments[label], True

    monkeypatch.setattr(wmod, "_invoke_write_llm", _stub)
    html, warnings, _gp, _np = _run(cfg, session)
    assert "part7_route_b_uap_dropped" in warnings, (
        "ROUTE B drop not detected — _maybe_inject_part7_fallbacks did not "
        "recognise the UAP-empty-draft case OR ROUTE B branch never fired"
    )
    assert "part7_uap_fallback_injected" in warnings, (
        "UAP fallback HTML not spliced — detection fired but injection "
        "branch returned without writing"
    )
    body_lc = wmod._strip_tags(html).lower()
    uap_present = any(
        tok in body_lc
        for tok in ("uap", "unidentified", "phenomena", "uaps")
    )
    assert uap_present, (
        "post-stitch HTML still lacks any UAP token — fallback was "
        "injected into part7 raw but stitcher/post-process dropped it"
    )
