"""Unit tests for jeeves.research_sectors — per-sector dedup + parsing helpers."""

from __future__ import annotations

import pytest

from jeeves.research_sectors import (
    SECTOR_SPECS,
    _NO_QUOTA_CHECK,
    _ParseFailed,
    _build_user_prompt,
    _is_nim_rate_limit,
    _is_redirect_artifact,
    _is_retryable_network_error,
    _parse_sector_output,
    _python_repr_to_json,
    _quota_increased,
    _quota_snapshot,
    _recover_truncated_array,
    _remove_trailing_commas,
    _try_normalize_json,
    collect_headlines_from_sector,
    collect_urls_from_sector,
    extract_correspondence_references,
)


def _spec(name: str):
    return next(s for s in SECTOR_SPECS if s.name == name)


def test_parse_sector_output_string_sector_returns_raw_text():
    raw = "Partly cloudy with a 40% chance of rain. High 58F."
    assert _parse_sector_output(raw, _spec("weather")) == raw


def test_parse_sector_output_strips_markdown_fences():
    raw = '```json\n[{"category":"municipal","source":"My Edmonds News","findings":"x","urls":["https://a"]}]\n```'
    out = _parse_sector_output(raw, _spec("local_news"))
    assert isinstance(out, list) and out[0]["source"] == "My Edmonds News"


def test_parse_sector_output_tolerates_prose_around_json():
    raw = 'Here is the result:\n[{"source":"BBC","findings":"...","urls":["https://bbc.com/a"]}]\nEnd.'
    out = _parse_sector_output(raw, _spec("global_news"))
    assert isinstance(out, list) and len(out) == 1


def test_parse_sector_output_drops_uncited_list_items():
    """Items with urls:[] are hallucination signatures — they must be dropped."""
    raw = '[{"source":"NYRB","findings":"Essay A","urls":[]},{"source":"Aeon","findings":"Essay B","urls":["https://aeon.co/x"]}]'
    out = _parse_sector_output(raw, _spec("intellectual_journals"))
    assert len(out) == 1
    assert out[0]["source"] == "Aeon"


def test_parse_sector_output_all_uncited_returns_default():
    """If every list item is uncited, return the sector default (empty list)."""
    raw = '[{"source":"NYRB","findings":"x","urls":[]},{"source":"LRB","findings":"y","urls":[]}]'
    out = _parse_sector_output(raw, _spec("intellectual_journals"))
    assert out == []


def test_parse_sector_output_deep_no_urls_returns_default():
    """Deep sector with no URLs returns default rather than uncited findings."""
    raw = '{"findings":"some thoughts","urls":[]}'
    out = _parse_sector_output(raw, _spec("triadic_ontology"))
    assert out == {"findings": "", "urls": []}


def test_parse_sector_output_returns_parse_failed_on_no_json():
    """No JSON token in output returns _ParseFailed (not the default) so caller can repair."""
    out = _parse_sector_output("completely not json", _spec("local_news"))
    assert isinstance(out, _ParseFailed)
    assert out.raw == "completely not json"


def test_parse_sector_output_repairs_python_repr_deterministically():
    """Python repr (single quotes, True/False/None) is repaired without LLM retry."""
    raw = "{'findings': 'job posting found', 'urls': ['https://example.com'], 'deadline': None}"
    out = _parse_sector_output(raw, _spec("career"))
    assert isinstance(out, dict)
    assert out["findings"] == "job posting found"
    assert out["urls"] == ["https://example.com"]
    assert out["deadline"] is None


def test_parse_sector_output_returns_parse_failed_on_truly_unrecoverable_json():
    """Garbled output that can't be deterministically repaired returns _ParseFailed."""
    out = _parse_sector_output("{broken: json: with 'mixed' \"quotes\" and [unclosed", _spec("career"))
    assert isinstance(out, _ParseFailed)


def test_parse_sector_output_parse_failed_preserves_raw_for_repair():
    """_ParseFailed.raw contains the original raw text so repair retry can reformat it."""
    raw = "Here is some text but [malformed, json"
    out = _parse_sector_output(raw, _spec("local_news"))
    assert isinstance(out, _ParseFailed)
    assert out.raw == raw


def test_parse_sector_output_empty_raw_returns_parse_failed():
    """Empty output (e.g. all tool calls had None id/name) returns _ParseFailed with empty raw."""
    out = _parse_sector_output("", _spec("local_news"))
    assert isinstance(out, _ParseFailed)
    assert out.raw == ""


def test_parse_sector_output_deep_shape():
    raw = '{"findings":"triadic stuff","urls":["https://x"]}'
    out = _parse_sector_output(raw, _spec("triadic_ontology"))
    assert out["findings"] == "triadic stuff"
    assert out["urls"] == ["https://x"]


def test_collect_urls_walks_nested_structures():
    value = [
        {"category": "a", "urls": ["https://one"], "findings": "x"},
        {"category": "b", "urls": ["https://two", "https://three"]},
    ]
    assert sorted(collect_urls_from_sector(value)) == [
        "https://one", "https://three", "https://two"
    ]


def test_collect_urls_picks_single_url_field_too():
    value = {"available": True, "url": "https://newyorker.com/x", "title": "T"}
    assert collect_urls_from_sector(value) == ["https://newyorker.com/x"]


def test_collect_headlines_pulls_title_headline_subject_role_district():
    value = [
        {"title": "Tacoma flooding", "urls": []},
        {"headline": "Senate hearing", "urls": []},
        {"openings": [{"role": "HS English", "district": "Edmonds"}]},
    ]
    out = collect_headlines_from_sector(value)
    assert "Tacoma flooding" in out
    assert "Senate hearing" in out
    assert "HS English" in out
    assert "Edmonds" in out


def test_collect_headlines_ignores_plain_strings_and_urls():
    assert collect_headlines_from_sector("a weather string") == []
    assert collect_headlines_from_sector({"urls": ["https://x"]}) == []


def test_collect_headlines_extracts_family_choir_and_toddler():
    """family {choir, toddler} strings must produce covered_headlines entries."""
    value = {
        "choir": "Seattle Symphony Chorale open auditions May 3.",
        "toddler": "Lynnwood library: Baby Storytime Thursdays 10:30am.",
        "urls": [],
    }
    out = collect_headlines_from_sector(value)
    assert any("Seattle Symphony Chorale" in h for h in out), f"choir not found: {out}"
    assert any("Lynnwood library" in h for h in out), f"toddler not found: {out}"


def test_collect_headlines_extracts_findings_first_sentence():
    """findings strings in list-shaped sectors produce a headline from first sentence."""
    value = [
        {
            "category": "politics",
            "source": "BBC",
            "findings": "Parliament voted on the budget. The result was close.",
            "urls": [],
        }
    ]
    out = collect_headlines_from_sector(value)
    assert any("Parliament voted" in h for h in out), f"findings not extracted: {out}"


def test_extract_correspondence_references_parses_handoff_lines():
    text = (
        "- [escalation] Sarah Lang: pick up milk, confirm storytime\n"
        "- [scheduling] Northshore SD HR: first-round interview\n"
        "- [no action] GitHub: workflow queued\n"
    )
    refs = extract_correspondence_references(text)
    assert refs == [
        "email | Sarah Lang",
        "email | Northshore SD HR",
        "email | GitHub",
    ]


def test_extract_correspondence_references_handles_empty_and_malformed():
    assert extract_correspondence_references("") == []
    assert extract_correspondence_references("just prose, no bracketed classifications") == []


def test_sector_specs_cover_every_researched_session_field():
    # Guard against schema drift: every researched SessionModel field should
    # have a matching SECTOR_SPEC. Housekeeping fields (date/status/dedup) are
    # populated by the driver, `correspondence` by Phase 4, and `vault_insight`
    # is an offline hook that may be filled by a separate sync — none of those
    # are researched by the agent.
    from jeeves.schema import SessionModel

    excluded = {"date", "status", "dedup", "correspondence", "vault_insight", "schema_version"}
    researched = set(SessionModel.model_fields.keys()) - excluded
    spec_names = {s.name for s in SECTOR_SPECS}
    assert researched == spec_names, (
        f"spec/schema mismatch. spec has {spec_names}, researched schema has {researched}"
    )


def test_intellectual_journals_instruction_has_three_parallel_searches():
    """intellectual_journals must mandate 3 parallel exa calls covering different outlet groups."""
    spec = _spec("intellectual_journals")
    assert "LRB" in spec.instruction and "Aeon" in spec.instruction
    assert "NYRB" in spec.instruction and "ProPublica" in spec.instruction
    assert "Marginalian" in spec.instruction or "Big Think" in spec.instruction
    # At least 3 numbered parallel dispatch lines
    assert spec.instruction.count("exa_search(") >= 3


def test_global_news_instruction_has_diversity_requirement():
    """global_news must require BBC/Guardian/Al Jazeera diversity."""
    spec = _spec("global_news")
    assert "BBC" in spec.instruction
    assert "Guardian" in spec.instruction or "Al Jazeera" in spec.instruction


def test_global_news_instruction_bans_redirect_urls():
    """global_news must explicitly warn against vertexaisearch redirect URLs."""
    spec = _spec("global_news")
    assert "vertexaisearch" in spec.instruction


def test_career_instruction_includes_deadline_and_salary_range():
    """Career sector instruction must request deadline and salary_range fields."""
    career_spec = _spec("career")
    assert "deadline" in career_spec.instruction
    assert "salary_range" in career_spec.instruction


def test_context_header_quota_summary_inserted():
    """_build_user_prompt includes quota summary when provided."""
    spec = _spec("weather")
    prompt = _build_user_prompt(
        spec, "2026-04-23", [],
        quota_summary="serper: 100/2500, tavily: EXHAUSTED — avoid",
    )
    assert "serper: 100/2500" in prompt
    assert "EXHAUSTED" in prompt


def test_context_header_story_continuity_inserted():
    """_build_user_prompt includes story continuity block when provided."""
    spec = _spec("global_news")
    prompt = _build_user_prompt(
        spec, "2026-04-23", [],
        story_continuity="Ongoing stories:\n  [global] Tariff talks resumed.",
    )
    assert "Tariff talks resumed" in prompt


def test_context_header_empty_quota_not_inserted():
    """Empty quota_summary should not inject any text."""
    spec = _spec("weather")
    prompt = _build_user_prompt(spec, "2026-04-23", [], quota_summary="")
    assert "Provider quota remaining" not in prompt


def test_covered_headlines_includes_newyorker_title():
    """covered_headlines() must include the New Yorker title from the session."""
    from jeeves.dedup import covered_headlines
    from jeeves.schema import SessionModel

    sess = SessionModel.model_validate({
        "date": "2026-04-23",
        "dedup": {"covered_urls": [], "covered_headlines": ["Some headline"]},
        "newyorker": {
            "available": True,
            "title": "Talk of the Town: Mock Article",
            "url": "https://www.newyorker.com/mock",
        },
    })
    hl = covered_headlines(sess)
    assert "Talk of the Town: Mock Article" in hl
    assert "Some headline" in hl


# ---------------------------------------------------------------------------
# vertex_grounded_search must return JSON strings (not dicts) at all exits
# ---------------------------------------------------------------------------

def test_vertex_grounded_returns_json_string_when_project_not_configured():
    """vertex_grounded_search must return a JSON string, not a dict, for the
    early-exit (no GOOGLE_CLOUD_PROJECT) path — same contract as all other tools."""
    import json as _json
    from unittest.mock import MagicMock

    from jeeves.tools.vertex_search import make_vertex_grounded
    from jeeves.tools.quota import QuotaLedger
    from pathlib import Path

    cfg = MagicMock()
    cfg.google_cloud_project = ""  # triggers early-exit
    ledger = QuotaLedger(Path("/tmp/test-quota-vertex.json"))

    fn = make_vertex_grounded(cfg, ledger)
    result = fn("test question")

    assert isinstance(result, str), f"expected str, got {type(result).__name__}"
    parsed = _json.loads(result)
    assert parsed["provider"] == "vertex"
    assert "error" in parsed


def test_vertex_grounded_returns_json_string_when_daily_cap_reached():
    """vertex_grounded_search daily cap exit must return a JSON string."""
    import json as _json
    from unittest.mock import MagicMock, patch

    from jeeves.tools.vertex_search import make_vertex_grounded
    from jeeves.tools.quota import QuotaExceeded, QuotaLedger
    from pathlib import Path

    cfg = MagicMock()
    cfg.google_cloud_project = "my-project"
    ledger = QuotaLedger(Path("/tmp/test-quota-vertex2.json"))

    with patch.object(ledger, "check_daily_allow", side_effect=QuotaExceeded("cap")):
        fn = make_vertex_grounded(cfg, ledger)
        result = fn("test question")

    assert isinstance(result, str), f"expected str, got {type(result).__name__}"
    parsed = _json.loads(result)
    assert "daily cap" in parsed.get("error", "")


# ---------------------------------------------------------------------------
# _normalize_tool_kwargs: string validation for additional_kwargs arguments
# ---------------------------------------------------------------------------

def test_normalize_tool_kwargs_fixes_null_string_in_additional_kwargs():
    """additional_kwargs tool_calls with function.arguments='null' must become '{}'."""
    from types import SimpleNamespace
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()

    def _make_msg(arguments):
        from llama_index.core.llms import ChatMessage, MessageRole
        tc = SimpleNamespace(id="c1", function=SimpleNamespace(arguments=arguments))
        msg = ChatMessage(role=MessageRole.ASSISTANT, content=None)
        msg.additional_kwargs["tool_calls"] = [tc]
        return msg, tc

    msg, tc = _make_msg("null")
    cls._normalize_tool_kwargs([msg])
    assert tc.function.arguments == "{}"


def test_normalize_tool_kwargs_fixes_corrupted_string_in_additional_kwargs():
    """additional_kwargs tool_calls with function.arguments='{}null' must become '{}'."""
    from types import SimpleNamespace
    from jeeves.llm import _build_kimi_class

    cls = _build_kimi_class()

    def _make_msg(arguments):
        from llama_index.core.llms import ChatMessage, MessageRole
        tc = SimpleNamespace(id="c1", function=SimpleNamespace(arguments=arguments))
        msg = ChatMessage(role=MessageRole.ASSISTANT, content=None)
        msg.additional_kwargs["tool_calls"] = [tc]
        return msg, tc

    msg, tc = _make_msg("{}null")
    cls._normalize_tool_kwargs([msg])
    assert tc.function.arguments == "{}"


def test_covered_headlines_no_newyorker_title_when_empty():
    """covered_headlines() must not insert empty string from newyorker.title."""
    from jeeves.dedup import covered_headlines
    from jeeves.schema import SessionModel

    sess = SessionModel.model_validate({
        "date": "2026-04-23",
        "dedup": {"covered_urls": [], "covered_headlines": []},
        "newyorker": {"available": False, "title": ""},
    })
    hl = covered_headlines(sess)
    assert "" not in hl


def test_correspondence_handoff_model_validates():
    """CorrespondenceHandoff accepts valid handoff data and rejects garbage."""
    from pydantic import ValidationError

    from jeeves.schema import CorrespondenceHandoff

    h = CorrespondenceHandoff.model_validate({"found": True, "fallback_used": False, "text": "Hi"})
    assert h.found is True
    assert h.text == "Hi"

    # Extra fields should be allowed (extra="allow").
    h2 = CorrespondenceHandoff.model_validate({"found": False, "extra_key": "ignored"})
    assert h2.found is False


def test_load_prior_sessions_returns_list(tmp_path):
    """load_prior_sessions returns a list of SessionModel objects from disk."""
    import json
    from datetime import date, timedelta

    from jeeves.config import Config
    from jeeves.session_io import load_prior_sessions

    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    run_date = date(2026, 4, 27)

    # Write two fake prior sessions.
    for delta in (1, 2):
        d = run_date - timedelta(days=delta)
        path = sessions_dir / f"session-{d.isoformat()}.json"
        path.write_text(json.dumps({"date": d.isoformat(), "status": "complete"}), encoding="utf-8")

    cfg = Config(
        nvidia_api_key="", serper_api_key="", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="test/repo",
        run_date=run_date, repo_root=tmp_path,
    )
    result = load_prior_sessions(cfg, days=7)
    assert len(result) == 2
    assert result[0].date == (run_date - timedelta(days=1)).isoformat()
    assert result[1].date == (run_date - timedelta(days=2)).isoformat()


def test_is_redirect_artifact_identifies_vertexaisearch():
    """vertexaisearch.cloud.google.com URLs are redirect artifacts."""
    url = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYX123"
    assert _is_redirect_artifact(url) is True


def test_is_redirect_artifact_allows_real_urls():
    assert _is_redirect_artifact("https://www.reuters.com/world/story") is False
    assert _is_redirect_artifact("https://www.theguardian.com/article") is False


def test_collect_urls_filters_redirect_artifacts():
    """vertexaisearch redirect URLs must not enter the covered_urls dedup window."""
    value = [
        {
            "source": "Gemini",
            "findings": "Iran war update.",
            "urls": [
                "https://vertexaisearch.cloud.google.com/grounding-api-redirect/ABC123",
                "https://www.reuters.com/world/middle-east/iran-story",
            ],
        }
    ]
    urls = collect_urls_from_sector(value)
    assert "https://www.reuters.com/world/middle-east/iran-story" in urls
    assert not any("vertexaisearch" in u for u in urls)


def test_collect_urls_filters_redirect_artifacts_single_url_key():
    """Single 'url' key redirect artifacts are also filtered."""
    value = {"url": "https://vertexaisearch.cloud.google.com/grounding-api-redirect/XYZ"}
    assert collect_urls_from_sector(value) == []


# ---------------------------------------------------------------------------
# Quota-snapshot helpers
# ---------------------------------------------------------------------------

class _FakeLedger:
    def __init__(self, state):
        self._state = state


def test_quota_snapshot_captures_used_counts():
    ledger = _FakeLedger({"providers": {"serper": {"used": 5}, "tavily": {"used": 3}}})
    snap = _quota_snapshot(ledger)
    assert snap == {"serper": 5, "tavily": 3}


def test_quota_increased_true_when_any_provider_increments():
    ledger = _FakeLedger({"providers": {"serper": {"used": 6}, "tavily": {"used": 3}}})
    before = {"serper": 5, "tavily": 3}
    assert _quota_increased(before, ledger) is True


def test_quota_increased_false_when_nothing_changed():
    ledger = _FakeLedger({"providers": {"serper": {"used": 5}, "tavily": {"used": 3}}})
    before = {"serper": 5, "tavily": 3}
    assert _quota_increased(before, ledger) is False


def test_no_quota_check_excludes_newyorker():
    assert "newyorker" in _NO_QUOTA_CHECK


def test_is_retryable_network_error_matches_known_phrases():
    assert _is_retryable_network_error(Exception("peer closed connection without sending complete message body"))
    assert _is_retryable_network_error(Exception("incomplete chunked read"))
    assert _is_retryable_network_error(Exception("connection reset by peer"))
    assert not _is_retryable_network_error(Exception("json decode error"))
    assert not _is_retryable_network_error(Exception("422 Unprocessable Entity"))


def test_is_nim_rate_limit_matches_429_strings():
    assert _is_nim_rate_limit(Exception("Error code: 429 - {'status': 429, 'title': 'Too Many Requests'}"))
    assert _is_nim_rate_limit(Exception("429 Too Many Requests"))
    assert _is_nim_rate_limit(Exception("too many requests"))
    assert not _is_nim_rate_limit(Exception("peer closed connection"))
    assert not _is_nim_rate_limit(Exception("400 Bad Request"))
    assert not _is_nim_rate_limit(Exception("RESOURCE_EXHAUSTED"))  # gemini, not NIM


def test_family_instruction_has_mandatory_parallel_searches():
    """family sector must specify 3 explicit parallel searches to prevent None-arg crashes."""
    spec = _spec("family")
    assert "serper_search(query=" in spec.instruction
    assert "exa_search(query=" in spec.instruction
    assert spec.instruction.count("serper_search(") >= 2
    assert "Seattle" in spec.instruction and "Edmonds" in spec.instruction


def test_enriched_articles_instruction_skips_failed_fetches():
    """enriched_articles must instruct Kimi to replace 401/403 URLs rather than include them."""
    spec = _spec("enriched_articles")
    assert "401" in spec.instruction or "fetch_failed" in spec.instruction
    assert "replace" in spec.instruction.lower() or "skip" in spec.instruction.lower()


def test_enriched_articles_instruction_warns_about_reuters():
    """enriched_articles must warn that Reuters blocks with 401 so Kimi picks alternatives."""
    spec = _spec("enriched_articles")
    assert "Reuters" in spec.instruction
    assert "401" in spec.instruction


def test_deep_fallback_queries_cover_all_deep_sectors():
    """Every deep-shaped sector must have a fallback query for forced-search retry."""
    from jeeves.research_sectors import _DEEP_FALLBACK_QUERIES

    deep_sectors = {s.name for s in SECTOR_SPECS if s.shape == "deep"}
    assert deep_sectors == set(_DEEP_FALLBACK_QUERIES.keys()), (
        f"missing fallback queries for: {deep_sectors - set(_DEEP_FALLBACK_QUERIES.keys())}"
    )


# ---------------------------------------------------------------------------
# Deterministic JSON normalisation helpers
# ---------------------------------------------------------------------------

def test_python_repr_to_json_converts_booleans_and_none():
    assert _python_repr_to_json("True") == "true"
    assert _python_repr_to_json("False") == "false"
    assert _python_repr_to_json("None") == "null"
    assert _python_repr_to_json("{'a': True, 'b': None}") == '{"a": true, "b": null}'


def test_python_repr_to_json_converts_single_quotes():
    result = _python_repr_to_json("{'key': 'value'}")
    import json
    assert json.loads(result) == {"key": "value"}


def test_remove_trailing_commas_cleans_objects_and_arrays():
    assert _remove_trailing_commas('{"a": 1,}') == '{"a": 1}'
    assert _remove_trailing_commas('[1, 2, 3,]') == '[1, 2, 3]'
    assert _remove_trailing_commas('[{"a": 1}, {"b": 2},]') == '[{"a": 1}, {"b": 2}]'


def test_recover_truncated_array_salvages_complete_items():
    truncated = '[{"title": "A", "url": "https://a"}, {"title": "B", "url": "https://b'
    result = _recover_truncated_array(truncated)
    assert result is not None
    import json
    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["title"] == "A"


def test_recover_truncated_array_returns_none_for_non_array():
    assert _recover_truncated_array('{"key": "val') is None


def test_try_normalize_json_fixes_python_repr():
    result = _try_normalize_json("{'findings': 'test', 'urls': ['https://x']}", is_array=False)
    assert result == {"findings": "test", "urls": ["https://x"]}


def test_try_normalize_json_fixes_trailing_comma():
    result = _try_normalize_json('[{"a": 1}, {"b": 2},]', is_array=True)
    assert result == [{"a": 1}, {"b": 2}]


def test_try_normalize_json_recovers_truncated_enriched_array():
    truncated = '[{"title": "Story A", "url": "https://example.com/a", "source": "BBC", "text": "content"}, {"title": "Story B'
    result = _try_normalize_json(truncated, is_array=True)
    assert isinstance(result, list)
    assert len(result) == 1
    assert result[0]["title"] == "Story A"


def test_try_normalize_json_coerces_bare_object_to_array():
    """A lone dict returned for a list-shape sector is wrapped in an array."""
    result = _try_normalize_json('{"source": "BBC", "findings": "x", "urls": []}', is_array=True)
    assert isinstance(result, list)
    assert result[0]["source"] == "BBC"


def test_try_normalize_json_returns_none_for_garbled_input():
    result = _try_normalize_json("{broken: json 'mixed\" [unclosed", is_array=False)
    assert result is None


def test_parse_sector_output_repairs_trailing_comma():
    """Trailing comma in array output is fixed deterministically."""
    raw = '[{"category": "municipal", "source": "MyEdmonds", "findings": "x", "urls": ["https://a"]},]'
    out = _parse_sector_output(raw, _spec("local_news"))
    assert isinstance(out, list)
    assert out[0]["source"] == "MyEdmonds"


def test_parse_sector_output_repairs_truncated_enriched_array():
    """NIM stream-truncated enriched array is salvaged without LLM retry."""
    raw = '[{"title": "AI chip", "url": "https://example.com/chip", "source": "Wired", "text": "content A"}, {"title": "Robot'
    out = _parse_sector_output(raw, _spec("enriched_articles"))
    assert isinstance(out, list)
    assert len(out) == 1
    assert out[0]["title"] == "AI chip"


def test_repair_shape_hint_newyorker_uses_correct_schema():
    """_REPAIR_SHAPE_HINT for newyorker must include 'available' not 'findings'."""
    from jeeves.research_sectors import _REPAIR_SHAPE_HINT

    hint = _REPAIR_SHAPE_HINT["newyorker"]
    assert "available" in hint, "repair hint must include 'available' field"
    assert "findings" not in hint, "repair hint must NOT use generic 'findings' shape"
    assert "title" in hint
    assert "text" in hint
    assert "url" in hint


def test_repair_shape_hint_newyorker_is_valid_json_template():
    """The newyorker repair hint must be valid JSON when placeholders are substituted."""
    import json
    from jeeves.research_sectors import _REPAIR_SHAPE_HINT

    hint = _REPAIR_SHAPE_HINT["newyorker"]
    # Replace the literal ... placeholders with actual values to check structural validity.
    filled = (
        hint
        .replace('"..."', '"placeholder"')
        .replace("true", "true")  # already valid JSON
    )
    obj = json.loads(filled)
    assert isinstance(obj, dict)
    assert "available" in obj


def test_tavily_extract_coerces_string_url_to_list(monkeypatch):
    """tavily_extract must accept a bare string URL and treat it as a one-element list."""
    import json
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    captured: list = []

    class FakeTavilyClient:
        def __init__(self, api_key):
            pass

        def extract(self, urls):
            captured.append(urls)
            return {"results": [{"url": urls[0], "raw_content": "article text", "title": "T"}]}

    monkeypatch.setattr("jeeves.tools.tavily.TavilyClient", FakeTavilyClient, raising=False)

    import sys
    import types
    # Ensure tavily package mock is available.
    if "tavily" not in sys.modules:
        fake_mod = types.ModuleType("tavily")
        fake_mod.TavilyClient = FakeTavilyClient
        sys.modules["tavily"] = fake_mod

    from datetime import date
    from pathlib import Path
    cfg = Config(
        nvidia_api_key="", serper_api_key="", tavily_api_key="key", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="test/repo",
        run_date=date(2026, 4, 28),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    import threading
    ledger._lock = threading.Lock()

    from jeeves.tools.tavily import make_tavily_extract
    fn = make_tavily_extract(cfg, ledger)

    # Call with a bare string — should NOT slice into individual characters.
    result = fn("https://example.com/article")
    data = json.loads(result)
    assert "results" in data
    # Verify Tavily client received a list with the full URL, not sliced chars.
    assert captured, "TavilyClient.extract was never called"
    assert captured[0] == ["https://example.com/article"], (
        f"Expected list with full URL, got: {captured[0]}"
    )
