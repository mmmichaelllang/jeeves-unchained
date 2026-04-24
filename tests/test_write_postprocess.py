"""Unit tests for jeeves.write post-processing."""

from __future__ import annotations

from datetime import date

from jeeves.schema import SessionModel
from jeeves.testing.mocks import canned_session
from jeeves.write import (
    PART1_SECTORS,
    PART2_SECTORS,
    PART3_SECTORS,
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


def test_postprocess_preserves_existing_coverage_log():
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body><p>content</p>"
        '<!-- COVERAGE_LOG: [{"headline":"h","url":"https://x.example.com","sector":"Sector 3"}] -->'
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    assert len(result.coverage_log) == 1
    assert result.coverage_log[0]["url"] == "https://x.example.com"


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
    assert "Deduplication" in trimmed
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

    # Content-generation parts still get the full pool.
    part2 = _system_prompt_for_parts(part_label="part2")
    assert "Pre-approved profane butler asides" in part2
    assert "Horrific Slips" in part2


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
    covered = {field for _, fields in PART_PLAN for field in fields}
    assert covered == set(SessionModel.model_fields.keys()) - {
        "date", "status", "dedup",
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
