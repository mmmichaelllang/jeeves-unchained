"""Unit tests for jeeves.write post-processing."""

from __future__ import annotations

from datetime import date

from jeeves.schema import SessionModel
from jeeves.testing.mocks import canned_session
from jeeves.write import (
    PART1_SECTORS,
    PART2_SECTORS,
    PART3_SECTORS,
    _safe_json_for_comment,
    _session_subset,
    _stitch_parts,
    _system_prompt_for_parts,
    load_write_system_prompt,
    postprocess_html,
    render_mock_briefing,
)


def _session() -> SessionModel:
    return SessionModel.model_validate(canned_session(date(2026, 4, 23)))


def test_write_prompt_loads_and_has_persona():
    prompt = load_write_system_prompt()
    assert prompt.strip().startswith("# Jeeves Write")
    assert "Mister Lang" in prompt
    assert "clusterfuck" in prompt
    assert "Sector 7" in prompt


def test_render_mock_briefing_validates():
    session = _session()
    html = render_mock_briefing(session)
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "COVERAGE_LOG" in html


def test_postprocess_strips_markdown_fence():
    session = _session()
    fenced = "```html\n<!DOCTYPE html><html><body>hi</body></html>\n```"
    result = postprocess_html(fenced, session)
    assert result.html.startswith("<!DOCTYPE html>")
    assert "```" not in result.html


def test_postprocess_wraps_missing_doctype():
    session = _session()
    raw = "<html><body><p>oops no doctype</p></body></html>"
    result = postprocess_html(raw, session)
    assert result.html.startswith("<!DOCTYPE html>")


def test_postprocess_builds_coverage_log_from_anchors():
    session = _session()
    raw = """<!DOCTYPE html><html><body>
    <p><a href="https://www.example.com/story-1">Example story</a> happened.</p>
    <p><a href="https://www.nybooks.com/mock">NYRB essay</a> appeared.</p>
    <!-- COVERAGE_LOG_PLACEHOLDER -->
    </body></html>"""
    result = postprocess_html(raw, session)
    urls = {entry["url"] for entry in result.coverage_log}
    assert "https://www.example.com/story-1" in urls
    assert "https://www.nybooks.com/mock" in urls


def test_postprocess_falls_back_to_model_log_when_no_anchors():
    """When the HTML has no <a href> anchors, the model-written COVERAGE_LOG is used."""
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body><p>content (no anchors here)</p>"
        '<!-- COVERAGE_LOG: [{"headline":"h","url":"https://x.example.com","sector":"Sector 3"}] -->'
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    assert len(result.coverage_log) == 1
    assert result.coverage_log[0]["url"] == "https://x.example.com"
    # Exactly one COVERAGE_LOG comment in output.
    assert result.html.count("COVERAGE_LOG:") == 1


def test_postprocess_synthesized_log_wins_over_model_log():
    """When <a href> anchors exist, synthesis wins over any model-written COVERAGE_LOG."""
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body>"
        "<p><a href=\"https://real.example.com/article\">Real article</a></p>"
        '<!-- COVERAGE_LOG: [{"headline":"model","url":"https://fabricated.example.com","sector":"S1"}] -->'
        "<!-- COVERAGE_LOG_PLACEHOLDER -->"
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    urls = {e["url"] for e in result.coverage_log}
    assert "https://real.example.com/article" in urls
    # Model-written fabricated URL discarded in favour of synthesis.
    assert "https://fabricated.example.com" not in urls
    # Exactly one COVERAGE_LOG comment in output.
    assert result.html.count("COVERAGE_LOG:") == 1
    assert "<!-- COVERAGE_LOG_PLACEHOLDER -->" not in result.html


def test_postprocess_two_model_coverage_logs_collapsed_to_one():
    """When the model writes two COVERAGE_LOG comments, exactly one survives."""
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body>"
        "<p><a href=\"https://article.example.com/story\">Story</a></p>"
        '<!-- COVERAGE_LOG: [{"headline":"partial","url":"https://partial.example.com","sector":"S5"}] -->'
        '<!-- COVERAGE_LOG: [{"headline":"also","url":"https://also.example.com","sector":"S7"}] -->'
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    # Synthesized from the real anchor wins; both model logs discarded.
    assert result.html.count("COVERAGE_LOG:") == 1
    urls = {e["url"] for e in result.coverage_log}
    assert "https://article.example.com/story" in urls


def test_postprocess_coverage_log_placeholder_never_survives():
    """COVERAGE_LOG_PLACEHOLDER must always be consumed (replaced or removed)."""
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body><p>hi</p>"
        "<!-- COVERAGE_LOG_PLACEHOLDER -->"
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    assert "<!-- COVERAGE_LOG_PLACEHOLDER -->" not in result.html
    assert "COVERAGE_LOG:" in result.html


def test_postprocess_flags_banned_words():
    session = _session()
    raw = """<!DOCTYPE html><html><body>
    <p>This happens in a vacuum and forms a rich tapestry.</p>
    </body></html>"""
    result = postprocess_html(raw, session)
    assert "in a vacuum" in result.banned_word_hits
    assert "tapestry" in result.banned_word_hits


def test_postprocess_flags_banned_transitions():
    session = _session()
    raw = """<!DOCTYPE html><html><body>
    <p>Moving on, Sir. Next, the weather.</p>
    </body></html>"""
    result = postprocess_html(raw, session)
    assert "Moving on," in result.banned_transition_hits
    assert "Next," in result.banned_transition_hits


def test_postprocess_flags_all_banned_transitions():
    """All eleven banned transitions must be caught by the QA check."""
    session = _session()
    body = (
        "Moving on, the weather. "
        "Next, local news. "
        "Turning to global affairs. "
        "Turning now to the economy. "
        "As we turn to the journals. "
        "Turning our attention to AI. "
        "In other news, a scandal. "
        "Closer to home, the council. "
        "Meanwhile, in Tokyo. "
        "Sir, you may wish to know, that. "
        "I note with interest, the report."
    )
    raw = f"<!DOCTYPE html><html><body><p>{body}</p></body></html>"
    result = postprocess_html(raw, session)
    for phrase in [
        "Moving on,", "Next,", "Turning to", "Turning now to",
        "As we turn to", "Turning our attention to", "In other news,",
        "Closer to home,", "Meanwhile,", "Sir, you may wish to know,",
        "I note with interest,",
    ]:
        assert phrase in result.banned_transition_hits, f"missed: {phrase!r}"


def test_preapproved_aside_does_not_trigger_banned_word_hit():
    """'is, if you'll excuse the expression, ass-backward' must not flag banned words."""
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body>"
        "<p>The decision is, if you'll excuse the expression, ass-backward.</p>"
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    assert not any("if you'll excuse" in hit for hit in result.banned_word_hits)


def test_mock_briefing_has_enough_profane_asides():
    session = _session()
    html = render_mock_briefing(session)
    result = postprocess_html(html, session)
    assert result.profane_aside_count >= 5


def test_session_subset_only_keeps_requested_fields_plus_housekeeping():
    payload = {
        "date": "2026-04-24",
        "status": "complete",
        "dedup": {"covered_headlines": ["a"]},
        "weather": "W",
        "career": {"openings": []},
        "family": {"choir": "..."},
        "triadic_ontology": {"findings": "..."},
    }
    out = _session_subset(payload, ["weather", "career"])
    assert set(out.keys()) == {"date", "status", "dedup", "weather", "career"}
    # Housekeeping keys always present even if not listed.
    assert out["dedup"] == {"covered_headlines": ["a"]}
    # Non-listed sector dropped.
    assert "triadic_ontology" not in out


def test_stitch_parts_three_way_preserves_structure():
    p1 = (
        '<!DOCTYPE html><html><head></head><body><div class="container">'
        '<h1>Header</h1><p>Sector 1.</p><!-- PART1 END -->'
    )
    p2 = '<p>Sector 3.</p><!-- PART2 END -->'
    p3 = (
        '<p>Sector 4.</p><div class="closing"><p>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER --></div></body></html>'
    )
    out = _stitch_parts(p1, p2, p3)
    assert out.count("<!DOCTYPE") == 1
    assert out.lower().count("<h1>") == 1
    assert "<!-- PART1 END" not in out
    assert "<!-- PART2 END" not in out
    assert "<!-- COVERAGE_LOG_PLACEHOLDER -->" in out
    assert "</body>" in out and "</html>" in out


def test_stitch_strips_continuation_wrapper_if_model_leaks_it():
    p1 = '<!DOCTYPE html><html><body><div><h1>X</h1><p>1</p><!-- PART1 END -->'
    # p2 wrongly includes DOCTYPE/h1 — must be stripped
    p2 = '<!DOCTYPE html><html><body><h1>X</h1><p>2</p><!-- PART2 END -->'
    p3 = '<p>3</p></div></body></html>'
    out = _stitch_parts(p1, p2, p3)
    assert out.count("<!DOCTYPE") == 1
    assert out.lower().count("<h1>") == 1
    assert "<p>1</p>" in out and "<p>2</p>" in out and "<p>3</p>" in out


def test_nim_refine_is_called_for_each_part(monkeypatch):
    """generate_briefing fires a NIM refine pass for every PART_PLAN slot."""
    from jeeves.config import Config
    from jeeves.write import generate_briefing

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "nvidia_api_key", "test-nim-key")

    session = _session()
    refined_labels: list[str] = []

    def fake_write_llm(c, sys, user, *, max_tokens, label):
        return f"<p>draft-{label}</p><!-- PART_SENTINEL -->", True

    def fake_nim_refine(c, draft, *, label):
        refined_labels.append(label)
        return draft.replace("draft", "refined")

    import jeeves.write as wmod
    monkeypatch.setattr(wmod, "_invoke_write_llm", fake_write_llm)
    monkeypatch.setattr(wmod, "_invoke_nim_refine", fake_nim_refine)
    # Suppress sleeps
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    html = generate_briefing(cfg, session)
    assert set(refined_labels) == {name for name, _ in wmod.PART_PLAN}
    assert "refined" in html


def test_nim_refine_failure_falls_back_to_raw_draft(monkeypatch):
    """If NIM refine raises, generate_briefing uses the raw Groq draft."""
    from jeeves.config import Config
    from jeeves.write import generate_briefing

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "nvidia_api_key", "test-nim-key")

    session = _session()

    def fake_write_llm(c, sys, user, *, max_tokens, label):
        return f"<p>raw-{label}</p>", True

    def fake_nim_refine(c, draft, *, label):
        raise RuntimeError("NIM is down")

    import jeeves.write as wmod
    import time
    monkeypatch.setattr(wmod, "_invoke_write_llm", fake_write_llm)
    monkeypatch.setattr(wmod, "_invoke_nim_refine", fake_nim_refine)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    html = generate_briefing(cfg, session)
    # Raw drafts must be in the output even though refine failed.
    assert "raw-part1" in html


def test_nim_write_fallback_triggers_on_tpd_error(monkeypatch):
    """_invoke_write_llm falls back to NIM when Groq raises a TPD error."""
    from jeeves.config import Config
    from jeeves.write import _invoke_write_llm

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "nvidia_api_key", "test-nim-key")
    object.__setattr__(cfg, "nim_write_model_id", "meta/llama-3.3-70b-instruct")

    nim_calls: list[str] = []

    def fake_groq(c, s, u, *, max_tokens, label):
        raise RuntimeError("Rate limit reached ... tokens per day (TPD): Limit 100000")

    def fake_nim(c, s, u, *, max_tokens, label):
        nim_calls.append(label)
        return "<p>NIM output</p>"

    import jeeves.write as wmod
    monkeypatch.setattr(wmod, "_invoke_groq", fake_groq)
    monkeypatch.setattr(wmod, "_invoke_nim_write", fake_nim)

    text, used_groq = _invoke_write_llm(cfg, "sys", "user", max_tokens=3000, label="part2")
    assert text == "<p>NIM output</p>"
    assert not used_groq
    assert nim_calls == ["part2"]


def test_nim_write_fallback_does_not_trigger_on_tpm_error(monkeypatch):
    """_invoke_write_llm re-raises non-TPD rate limit errors without falling back."""
    from jeeves.config import Config
    from jeeves.write import _invoke_write_llm

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")

    def fake_groq(c, s, u, *, max_tokens, label):
        raise RuntimeError("Rate limit reached ... tokens per minute (TPM): Limit 12000")

    import jeeves.write as wmod
    monkeypatch.setattr(wmod, "_invoke_groq", fake_groq)

    import pytest
    with pytest.raises(RuntimeError, match="tokens per minute"):
        _invoke_write_llm(cfg, "sys", "user", max_tokens=3000, label="part1")


def test_nim_fallback_skips_groq_tpm_sleep(monkeypatch):
    """When NIM handles a draft (Groq TPD exhausted), the 65s sleep is skipped."""
    from jeeves.config import Config
    from jeeves.write import generate_briefing

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "nvidia_api_key", "test-nim-key")

    session = _session()
    sleep_calls: list[float] = []

    def fake_write_llm(c, sys, user, *, max_tokens, label):
        # Simulate: part1 uses Groq, all subsequent parts fall back to NIM.
        used_groq = label == "part1"
        return f"<p>{label}</p>", used_groq

    def fake_nim_refine(c, draft, *, label):
        return draft

    import jeeves.write as wmod
    import time
    monkeypatch.setattr(wmod, "_invoke_write_llm", fake_write_llm)
    monkeypatch.setattr(wmod, "_invoke_nim_refine", fake_nim_refine)
    monkeypatch.setattr(time, "sleep", lambda s: sleep_calls.append(s))

    generate_briefing(cfg, session)

    # Only one sleep should have fired: the one between part1 (Groq) and part2 (NIM).
    # Parts 3–9 see last_used_groq=False and skip the sleep.
    assert sleep_calls == [65], f"expected exactly one 65s sleep, got {sleep_calls}"


def test_invoke_write_llm_returns_true_when_groq_succeeds(monkeypatch):
    """_invoke_write_llm returns used_groq=True when Groq succeeds."""
    from jeeves.config import Config
    from jeeves.write import _invoke_write_llm

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")

    def fake_groq(c, s, u, *, max_tokens, label):
        return "<p>Groq output</p>"

    import jeeves.write as wmod
    monkeypatch.setattr(wmod, "_invoke_groq", fake_groq)

    text, used_groq = _invoke_write_llm(cfg, "sys", "user", max_tokens=3000, label="part1")
    assert text == "<p>Groq output</p>"
    assert used_groq


def test_inject_newyorker_verbatim_replaces_placeholder():
    """_inject_newyorker_verbatim swaps the placeholder for real article paragraphs."""
    from jeeves.write import _inject_newyorker_verbatim

    session = _session()
    # Ensure the fixture has New Yorker content.
    assert session.newyorker.available
    assert session.newyorker.text

    html = "<div><!-- NEWYORKER_CONTENT_PLACEHOLDER --></div>"
    result = _inject_newyorker_verbatim(html, session)

    assert "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->" not in result
    assert "<!-- NEWYORKER_START -->" in result
    assert "<!-- NEWYORKER_END -->" in result
    # At least the first paragraph of the article text appears verbatim.
    first_para = session.newyorker.text.split("\n\n")[0].strip()
    assert first_para in result


def test_inject_newyorker_verbatim_noop_when_no_placeholder_and_no_intro():
    """_inject_newyorker_verbatim returns html unchanged when neither placeholder
    nor intro sentence is present (no hallucinated TOTT to excise)."""
    from jeeves.write import _inject_newyorker_verbatim

    session = _session()
    html = "<p>no placeholder here</p>"
    assert _inject_newyorker_verbatim(html, session) == html


def test_inject_newyorker_verbatim_excises_hallucinated_content():
    """When placeholder is absent but intro sentence is present, the fallback
    replaces everything between the intro </p> and the sign-off with real text,
    removing any hallucinated TOTT content (including [TRUNCATED] artefacts)."""
    from jeeves.write import _inject_newyorker_verbatim

    session = _session()
    hallucinated = (
        "<p>And now, Sir, I take the liberty of reading from this week's "
        "Talk of the Town in The New Yorker.</p>\n"
        "<p>The White House Correspondents Dinner was absolutely tragic "
        "publi [TRUNCATED]</p>\n"
        '<div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>\n'
        "<!-- COVERAGE_LOG: [] -->\n</div></body></html>"
    )
    result = _inject_newyorker_verbatim(hallucinated, session)

    assert "[TRUNCATED]" not in result
    assert "White House Correspondents" not in result
    assert "<!-- NEWYORKER_START -->" in result
    assert "<!-- NEWYORKER_END -->" in result
    assert "Your reluctantly faithful Butler" in result
    first_para = session.newyorker.text.split("\n\n")[0].strip()
    assert first_para in result


def test_inject_newyorker_verbatim_fallback_includes_read_link():
    """Fallback injection includes the Read at The New Yorker link."""
    from jeeves.write import _inject_newyorker_verbatim

    session = _session()
    hallucinated = (
        "<p>And now, Sir, I take the liberty of reading from this week's "
        "Talk of the Town in The New Yorker.</p>\n"
        "<p>Hallucinated content here.</p>\n"
        '<div class="signoff"><p>Jeeves</p></div>'
    )
    result = _inject_newyorker_verbatim(hallucinated, session)
    assert "Read at The New Yorker" in result
    assert session.newyorker.url in result


def test_inject_newyorker_verbatim_removes_placeholder_when_unavailable():
    """When newyorker.available=False the placeholder is removed, not filled."""
    from jeeves.write import _inject_newyorker_verbatim
    from jeeves.schema import NewYorker

    session = _session()
    object.__setattr__(session, "newyorker", NewYorker(available=False))
    html = "<div><!-- NEWYORKER_CONTENT_PLACEHOLDER --></div>"
    result = _inject_newyorker_verbatim(html, session)
    assert "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->" not in result
    assert "<!-- NEWYORKER_START -->" not in result


def test_build_source_url_map_extracts_sector_sources():
    """_build_source_url_map returns source→url pairs from all session sectors."""
    from jeeves.write import _build_source_url_map

    session = _session()
    m = _build_source_url_map(session)

    # local_news source
    assert "myedmondsnews.com" in m
    assert m["myedmondsnews.com"] == "https://myedmondsnews.com/council-parking"
    # global_news source
    assert "BBC" in m
    assert m["BBC"] == "https://www.bbc.com/news/mock"
    # intellectual_journals — previously broken (read item.url instead of urls[0])
    assert "NYRB" in m
    assert m["NYRB"] == "https://www.nybooks.com/mock"
    # enriched_articles: title mapping
    assert "Council passes parking ordinance" in m
    assert m["Council passes parking ordinance"] == "https://myedmondsnews.com/council-parking"


def test_inject_source_links_wraps_first_unlinked_occurrence():
    """_inject_source_links anchors the first unlinked mention of a source name."""
    from jeeves.write import _inject_source_links

    html = "<p>The BBC reports something. The BBC also noted this.</p>"
    result = _inject_source_links(html, {"BBC": "https://bbc.example/mock"})

    assert '<a href="https://bbc.example/mock">BBC</a>' in result
    # Only the first occurrence is linked.
    assert result.count('<a href="https://bbc.example/mock">') == 1
    # Second occurrence is plain text.
    assert "The BBC also noted" in result


def test_inject_source_links_skips_already_linked_url():
    """_inject_source_links skips a source whose URL is already present in the HTML."""
    from jeeves.write import _inject_source_links

    html = '<p>The <a href="https://bbc.example/mock">BBC</a> reports. BBC again.</p>'
    result = _inject_source_links(html, {"BBC": "https://bbc.example/mock"})

    # URL already present → no additional injection.
    assert result.count('<a href="https://bbc.example/mock">') == 1
    assert result == html


def test_inject_source_links_does_not_nest_anchors():
    """_inject_source_links never injects inside an existing <a> tag."""
    from jeeves.write import _inject_source_links

    html = '<p><a href="https://other.example">BBC latest</a> and BBC.</p>'
    result = _inject_source_links(html, {"BBC": "https://bbc.example/mock"})

    # "BBC latest" is inside an anchor — must not be wrapped.
    assert '<a href="https://other.example">BBC latest</a>' in result


def test_inject_source_links_noop_on_empty_map():
    """_inject_source_links returns html unchanged when source_url_map is empty."""
    from jeeves.write import _inject_source_links

    html = "<p>The BBC reports. Reuters confirms.</p>"
    assert _inject_source_links(html, {}) == html


def test_session_subset_includes_uap_has_new():
    """_session_subset passes uap_has_new to part7 payload, defaulting True."""
    from jeeves.write import _session_subset

    payload_with_flag = {"date": "2026-04-29", "uap_has_new": False, "uap": {}}
    subset = _session_subset(payload_with_flag, ["uap", "uap_has_new"])
    assert subset["uap_has_new"] is False

    payload_no_flag = {"date": "2026-04-29", "uap": {}}
    subset2 = _session_subset(payload_no_flag, ["uap", "uap_has_new"])
    assert subset2["uap_has_new"] is True  # default for old sessions


def test_narrative_edit_skipped_when_no_key(monkeypatch):
    """_invoke_openrouter_narrative_edit returns html unchanged when key is absent."""
    from jeeves.config import Config
    from jeeves.write import _invoke_openrouter_narrative_edit

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    # openrouter_api_key defaults to "" — no key set.
    assert cfg.openrouter_api_key == ""

    html = "<p>some html</p>"
    assert _invoke_openrouter_narrative_edit(cfg, html) == html


def test_narrative_edit_called_in_generate_briefing(monkeypatch):
    """generate_briefing invokes the OpenRouter narrative editor when key is set."""
    from jeeves.config import Config
    from jeeves.write import generate_briefing

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "openrouter_api_key", "test-or-key")

    session = _session()
    edit_calls: list[str] = []

    def fake_write_llm(c, sys, user, *, max_tokens, label):
        return f"<p>{label}</p>", True

    def fake_nim_refine(c, draft, *, label):
        return draft

    def fake_narrative_edit(c, html, *, recently_used_asides=None):
        edit_calls.append(html)
        return html.replace("<p>", "<p data-edited='true'>")

    import jeeves.write as wmod
    import time
    monkeypatch.setattr(wmod, "_invoke_write_llm", fake_write_llm)
    monkeypatch.setattr(wmod, "_invoke_nim_refine", fake_nim_refine)
    monkeypatch.setattr(wmod, "_invoke_openrouter_narrative_edit", fake_narrative_edit)
    monkeypatch.setattr(time, "sleep", lambda s: None)

    html = generate_briefing(cfg, session)
    assert len(edit_calls) == 1, "narrative editor should be called exactly once"
    assert "data-edited='true'" in html


def test_narrative_edit_fallback_on_api_failure(monkeypatch):
    """_invoke_openrouter_narrative_edit falls back to original html on API failure."""
    import sys
    import types
    from jeeves.config import Config
    from jeeves.write import _invoke_openrouter_narrative_edit

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "openrouter_api_key", "test-or-key")

    # Inject a fake openai module whose OpenAI constructor raises.
    class FakeClient:
        def __init__(self, *a, **kw):
            raise RuntimeError("simulated network error")

    fake_openai = types.ModuleType("openai")
    fake_openai.OpenAI = FakeClient
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    html = "<!DOCTYPE html><html><body><p>original</p></body></html>"
    result = _invoke_openrouter_narrative_edit(cfg, html)
    assert result == html


def test_part9_instructions_use_placeholder():
    """PART9_INSTRUCTIONS tells the model to output the placeholder, not copy text."""
    from jeeves.write import PART9_INSTRUCTIONS
    assert "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->" in PART9_INSTRUCTIONS
    assert "COPY THE TEXT CHARACTER-FOR-CHARACTER" not in PART9_INSTRUCTIONS
    assert "VERBATIM RULES" not in PART9_INSTRUCTIONS


def test_sector_groups_partition_writable_fields_without_overlap():
    all_parts = PART1_SECTORS + PART2_SECTORS + PART3_SECTORS
    assert len(all_parts) == len(set(all_parts)), "sector appears in multiple parts"


def test_system_prompt_for_parts_strips_html_scaffold_block():
    base = load_write_system_prompt()
    trimmed = _system_prompt_for_parts()
    assert "## HTML scaffold" in base
    assert "## HTML scaffold" not in trimmed
    # Sector descriptions block also stripped (covered by per-part instructions).
    assert "## Briefing structure" in base
    assert "## Briefing structure" not in trimmed
    # Persona and mandatory rules still present.
    assert "You are **Jeeves**" in trimmed
    assert "Synthesis protocol" in trimmed
    # The asides pool stays in by default (content-generation parts need it).
    assert "Pre-approved profane butler asides" in trimmed
    assert "clusterfuck of biblical proportions" in trimmed


def test_system_prompt_for_part9_strips_asides_pool():
    """Part 9 is a verbatim pass-through of the New Yorker article — it
    generates no profane asides of its own. Keeping the ~3000-char asides
    pool in its system prompt would crowd out the 4000-char article's token
    budget. Scoping: rules that don't apply are omitted, not compressed."""
    part9 = _system_prompt_for_parts(part_label="part9")
    # Asides pool and Horrific Slips directive both gone.
    assert "Pre-approved profane butler asides" not in part9
    assert "Horrific Slips" not in part9
    # But core rules like zero-fabrication, banned-words, etc. remain.
    assert "Zero fabrication" in part9
    assert "Banned words" in part9
    assert "You are **Jeeves**" in part9

    # Content-generation parts still get the full pool (draft instruction says zero asides,
    # but the pool is present so the final OpenRouter editor can reference it).
    part2 = _system_prompt_for_parts(part_label="part2")
    assert "Pre-approved profane butler asides" in part2
    assert "Horrific Slips" in part2
    assert "DRAFT ZERO" in part2


def test_part1_instructions_embed_css_scaffold():
    """PART1_INSTRUCTIONS must carry the CSS scaffold since _system_prompt_for_parts strips the scaffold block."""
    from jeeves.write import PART1_INSTRUCTIONS
    # Scaffold is fully self-contained — no dependency on write_system.md CSS.
    assert "font-family: Georgia" in PART1_INSTRUCTIONS
    assert "background: #fdfaf5" in PART1_INSTRUCTIONS
    assert "max-width: 660px" in PART1_INSTRUCTIONS


def test_continuation_rules_mandate_linking_and_ban_fabrication():
    """CONTINUATION_RULES rule 14 must mandate proactive linking and ban URL fabrication."""
    from jeeves.write import CONTINUATION_RULES
    assert "LINKING IS MANDATORY" in CONTINUATION_RULES
    assert "Never invent a URL" in CONTINUATION_RULES


def test_part3_instructions_have_empty_career_rule():
    """PART3_INSTRUCTIONS must have a hard empty-career sentinel rule."""
    from jeeves.write import PART3_INSTRUCTIONS
    assert "EMPTY CAREER FEED" in PART3_INSTRUCTIONS
    # Ensure the exact fallback sentence is prescribed.
    assert "quiet this morning" in PART3_INSTRUCTIONS


def test_part9_instructions_have_strict_branch_separation():
    """PART9_INSTRUCTIONS must use explicit BRANCH A / BRANCH B language."""
    from jeeves.write import PART9_INSTRUCTIONS
    assert "BRANCH A" in PART9_INSTRUCTIONS
    assert "BRANCH B" in PART9_INSTRUCTIONS
    assert "WRITE BRANCH A AND NOTHING ELSE" in PART9_INSTRUCTIONS
    assert "WRITE BRANCH B AND NOTHING ELSE" in PART9_INSTRUCTIONS


def test_parse_all_asides_returns_full_original_pool():
    from jeeves.write import _parse_all_asides

    asides = _parse_all_asides()
    # Sanity: original 2026-04-23 list had ~55 phrases. We must not be
    # silently trimming.
    assert len(asides) >= 50
    assert "clusterfuck of biblical proportions, Sir" in asides
    assert "gold-plated shit-tornado" in asides
    # Thematic markers across categories we promise in the prompt.
    assert any("abysmal" in a for a in asides)       # weather
    assert any("fuck-wits" in a for a in asides)      # institutional
    assert any("cock-womble" in a for a in asides)    # trivial


def test_recently_used_asides_flags_phrases_from_prior_briefings(tmp_path, monkeypatch):
    from jeeves.config import Config
    from jeeves.write import _recently_used_asides

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "repo_root", tmp_path)
    (tmp_path / "sessions").mkdir()

    # Yesterday's briefing used two phrases; day before used one more.
    (tmp_path / "sessions" / "briefing-2026-04-23.local.html").write_text(
        '<p>It was, Sir, a clusterfuck of biblical proportions, Sir. '
        'The weather is, to use a rather strong term, fucking abysmal.</p>'
    )
    (tmp_path / "sessions" / "briefing-2026-04-22.local.html").write_text(
        '<p>A massive, throbbing cock-up, I\'m afraid.</p>'
    )

    used = _recently_used_asides(cfg, days=3)
    assert "clusterfuck of biblical proportions, Sir" in used
    assert "The weather is, to use a rather strong term, fucking abysmal" in used
    assert "A massive, throbbing cock-up, I'm afraid" in used
    # Phrases we did NOT use in the prior briefings should NOT be flagged.
    assert "pulsating knob-rot" not in used


def test_recently_used_asides_empty_when_no_history(tmp_path, monkeypatch):
    from jeeves.config import Config
    from jeeves.write import _recently_used_asides

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "repo_root", tmp_path)
    (tmp_path / "sessions").mkdir()
    assert _recently_used_asides(cfg) == []


def test_system_prompt_injects_avoid_list_when_cfg_has_history(tmp_path, monkeypatch):
    from jeeves.config import Config
    from jeeves.write import _system_prompt_for_parts

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "repo_root", tmp_path)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "briefing-2026-04-23.local.html").write_text(
        '<p>A symphony of screaming shit-weasels today, Sir.</p>'
    )
    prompt = _system_prompt_for_parts(cfg)
    assert "Recently used asides" in prompt
    assert "A symphony of screaming shit-weasels" in prompt


def test_system_prompt_has_no_avoid_list_without_history(tmp_path, monkeypatch):
    from jeeves.config import Config
    from jeeves.write import _system_prompt_for_parts

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    object.__setattr__(cfg, "repo_root", tmp_path)
    (tmp_path / "sessions").mkdir()
    prompt = _system_prompt_for_parts(cfg)
    assert "Recently used asides" not in prompt
    # And bare call (no cfg) should also omit it.
    assert "Recently used asides" not in _system_prompt_for_parts()


def test_system_prompt_injects_run_used_asides_without_cfg():
    """run_used_asides param populates the avoid list even with no cfg/history."""
    from jeeves.write import _system_prompt_for_parts

    prompt = _system_prompt_for_parts(
        run_used_asides=[
            "clusterfuck of biblical proportions, Sir",
            "absolute bollocks today",
        ]
    )
    assert "Recently used asides" in prompt
    assert "clusterfuck of biblical proportions, Sir" in prompt
    assert "absolute bollocks today" in prompt


def test_system_prompt_run_used_asides_excluded_for_no_aside_parts():
    """part9 never gets the avoid list regardless of run_used_asides."""
    from jeeves.write import _system_prompt_for_parts

    prompt = _system_prompt_for_parts(
        part_label="part9",
        run_used_asides=["clusterfuck of biblical proportions, Sir"],
    )
    assert "Recently used asides" not in prompt
    assert "Pre-approved profane butler asides" not in prompt


def test_part_plan_has_nine_slots_covering_all_session_fields():
    from jeeves.schema import SessionModel
    from jeeves.write import PART_PLAN

    assert len(PART_PLAN) == 9
    # newyorker_hint is a synthetic derived field (not a real SessionModel field)
    # injected into part7 so it can avoid duplicating New Yorker content.
    covered = {f for _, fields in PART_PLAN for f in fields if f != "newyorker_hint"}
    assert covered == set(SessionModel.model_fields.keys()) - {
        "date", "status", "dedup", "schema_version",
    }, f"PART_PLAN should cover every researched + correspondence field; got {covered}"


def test_part_plan_gives_newyorker_its_own_slot():
    """Sector 7 (Talk of the Town) must have its own call so the verbatim
    article pass-through gets the full TPM budget. Previously it shared a
    slot with vault_insight and the model paraphrased the article to fit."""
    from jeeves.write import PART_PLAN

    newyorker_slots = [name for name, fields in PART_PLAN if "newyorker" in fields]
    assert len(newyorker_slots) == 1
    name = newyorker_slots[0]
    # newyorker should be alone in its slot.
    fields = dict(PART_PLAN)[name]
    assert fields == ["newyorker"], f"newyorker must ride alone; got {fields}"


def test_part4_carries_newyorker_hint():
    """part4 must include newyorker_hint so the New Yorker overlap check can fire."""
    from jeeves.write import PART_PLAN

    part4_fields = dict(PART_PLAN)["part4"]
    assert "newyorker_hint" in part4_fields, f"part4 fields: {part4_fields}"


def test_sector_url_index_labels_career_openings():
    """Career openings URLs must be labelled Sector 2 in _sector_url_index."""
    from jeeves.write import _sector_url_index

    sess = SessionModel.model_validate({
        "date": "2026-04-23",
        "career": {
            "openings": [
                {"district": "Northshore SD", "role": "HS English",
                 "url": "https://northshoresd.org/jobs/123", "summary": "x"},
            ],
            "notes": "",
        },
    })
    idx = _sector_url_index(sess)
    assert idx.get("https://northshoresd.org/jobs/123") == "Sector 2"


def test_sector_url_index_labels_family_urls():
    """Family URLs must be labelled Sector 2 in _sector_url_index."""
    from jeeves.write import _sector_url_index

    sess = SessionModel.model_validate({
        "date": "2026-04-23",
        "family": {
            "choir": "Audition info.",
            "toddler": "Library storytime.",
            "urls": ["https://seattlesymphony.org/auditions"],
        },
    })
    idx = _sector_url_index(sess)
    assert idx.get("https://seattlesymphony.org/auditions") == "Sector 2"


def test_nim_rate_limit_detection():
    """_is_nim_rate_limit should detect 429, 'rate limit', 'too many requests'."""
    from jeeves.write import _is_nim_rate_limit

    assert _is_nim_rate_limit(Exception("HTTP 429 Too Many Requests"))
    assert _is_nim_rate_limit(Exception("rate limit exceeded"))
    assert _is_nim_rate_limit(Exception("too many requests"))
    assert not _is_nim_rate_limit(Exception("HTTP 500 Internal Server Error"))
    assert not _is_nim_rate_limit(Exception("connection refused"))


def test_schema_version_field_present():
    """SessionModel must carry schema_version = '1' by default."""
    sess = SessionModel(date="2026-04-23")
    assert sess.schema_version == "1"


def test_intellectual_journals_cap_raised():
    """intellectual_journals.findings cap must be at least 600 chars."""
    from jeeves.schema import FIELD_CAPS

    assert FIELD_CAPS["intellectual_journals.findings"] >= 600


def test_part4_instructions_contain_newyorker_overlap_check():
    """PART4_INSTRUCTIONS must include the newyorker overlap check."""
    from jeeves.write import PART4_INSTRUCTIONS

    assert "newyorker_hint" in PART4_INSTRUCTIONS, (
        "PART4_INSTRUCTIONS should reference newyorker_hint for the overlap check"
    )


def test_safe_json_for_comment_escapes_html_comment_close():
    """_safe_json_for_comment must prevent --> from closing an HTML comment."""
    data = [{"headline": "FDA ruling-->key decision", "url": "https://example.com"}]
    result = _safe_json_for_comment(data)
    assert "-->" not in result
    assert "--\\u003e" in result
    # Must still be valid JSON after the replacement.
    import json
    parsed = json.loads(result)
    assert parsed[0]["headline"] == "FDA ruling-->key decision"


def test_render_mock_briefing_escapes_html_in_session_fields():
    """render_mock_briefing must html.escape weather and newyorker text/url."""
    session = _session()
    # Inject HTML-dangerous content into session fields.
    object.__setattr__(session, "weather", '<script>alert("xss")</script>')
    ny = session.newyorker
    object.__setattr__(ny, "text", 'Article text with </p><b>bold</b> and "quotes"')
    object.__setattr__(ny, "url", 'https://example.com?a=1&b=2"onload=evil()')
    object.__setattr__(ny, "available", True)
    object.__setattr__(session, "newyorker", ny)

    html = render_mock_briefing(session)
    # Raw executable HTML tags must not appear outside of HTML comments/attributes.
    assert '<script>' not in html          # script tag must be escaped
    assert '</p><b>' not in html           # tag injection in body text must be escaped
    # The href attribute must escape " so it cannot break out of the attribute context.
    assert '&quot;onload=evil' in html or 'onload=evil' not in html.split('href=')[0]
    # Escaped forms must appear (proves escaping ran, not just omission).
    assert '&lt;script&gt;' in html
    assert '&lt;/p&gt;' in html or '&lt;b&gt;' in html


def test_newyorker_schema_declares_byline_and_date():
    """NewYorker model must have byline and date as declared fields (not extras)."""
    from jeeves.schema import NewYorker

    ny = NewYorker(available=True, title="Test", byline="By Jane Doe", date="2026-04-28")
    assert ny.byline == "By Jane Doe"
    assert ny.date == "2026-04-28"
    # Defaults to empty string when absent.
    ny2 = NewYorker(available=False)
    assert ny2.byline == ""
    assert ny2.date == ""


def test_newyorker_schema_byline_and_date_in_model_dump():
    """model_dump() must include byline and date (not silently dropped)."""
    from jeeves.schema import NewYorker

    ny = NewYorker(available=True, title="T", byline="By X", date="2026-01-01")
    d = ny.model_dump()
    assert "byline" in d
    assert "date" in d
    assert d["byline"] == "By X"
    assert d["date"] == "2026-01-01"


def test_postprocess_replaces_wrong_signoff() -> None:
    """postprocess_html replaces 'Yours faithfully' with the correct sign-off."""
    session = _session()
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Some content.</p>"
        "<div class='signoff'><p>Yours faithfully,<br/>Jeeves</p></div>"
        "<!-- COVERAGE_LOG: [] -->"
        "</body></html>"
    )
    result = postprocess_html(html, session)
    assert "Yours faithfully" not in result.html
    assert "Your reluctantly faithful Butler" in result.html


def test_banned_transitions_catches_turning_to_space() -> None:
    """postprocess_html flags 'Turning to Mali' as a banned transition."""
    session = _session()
    html = (
        "<!DOCTYPE html><html><body>"
        "<p>Turning to Mali, armed groups have escalated operations.</p>"
        "<!-- COVERAGE_LOG: [] -->"
        "</body></html>"
    )
    result = postprocess_html(html, session)
    assert any("turning to" in hit.lower() for hit in result.banned_transition_hits)


def test_refine_system_strips_significant_implications() -> None:
    """'significant implications for the region' must be in _REFINE_SYSTEM banned list."""
    from jeeves.write import _REFINE_SYSTEM

    assert "significant implications for the region" in _REFINE_SYSTEM
