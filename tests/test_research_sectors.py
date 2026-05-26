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
    """family {choir, toddler} strings must produce covered_headlines entries.

    2026-05-21 — headline extraction now picks proper-noun clusters as the
    distinguishing label (Flaw 2 fix). "Seattle Symphony Chorale" remains
    the dominant cluster for choir; the toddler entry's distinguishing
    cluster is "Baby Storytime Thursdays" rather than "Lynnwood library"
    (single-token "Lynnwood" doesn't form a 2+-token cluster on its own).
    Both still produce SOMETHING that uniquely identifies the entry, which
    is what the dedup signal needs.
    """
    value = {
        "choir": "Seattle Symphony Chorale open auditions May 3.",
        "toddler": "Lynnwood library: Baby Storytime Thursdays 10:30am.",
        "urls": [],
    }
    out = collect_headlines_from_sector(value)
    assert any("Seattle Symphony Chorale" in h for h in out), f"choir not found: {out}"
    # Either "Lynnwood" or "Baby Storytime" — both are valid distinguishing
    # labels for the toddler entry. The bug we are guarding against is
    # producing NO entry at all.
    assert any(
        "Lynnwood" in h or "Baby Storytime" in h for h in out
    ), f"toddler not found: {out}"


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

    excluded = {
        "date", "status", "dedup", "correspondence", "vault_insight", "schema_version",
        # uap_has_new is a flag set inline by the agent alongside the uap sector,
        # not a separate FunctionAgent sector call.
        "uap_has_new",
        # quality_warnings is populated during the write phase, not researched.
        "quality_warnings",
    }
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


# ---------------------------------------------------------------------------
# Sprint 12 quality fixes — headline extraction quality + cross-sector dedup
# ---------------------------------------------------------------------------

def test_first_sentence_default_is_250_chars():
    """_first_sentence default cap raised from 150 → 250 chars so titles
    aren't truncated mid-phrase before the dedup match runs."""
    from jeeves.research_sectors import _first_sentence

    long_text = "a" * 300
    out = _first_sentence(long_text)
    # No sentence terminator, so it falls back to the max_chars truncation.
    assert len(out) == 250


def test_first_sentence_respects_terminator_within_window():
    from jeeves.research_sectors import _first_sentence

    text = "Short headline. Long second sentence padding."
    out = _first_sentence(text)
    assert out == "Short headline."


def test_first_two_sentences_extracts_both_when_present():
    """Two-sentence extraction captures the title sentence + the
    distinguishing detail sentence for cross-day dedup matching."""
    from jeeves.research_sectors import _first_two_sentences

    text = (
        "AI policy update. The Department of Commerce released new export "
        "restrictions on advanced GPUs."
    )
    out = _first_two_sentences(text)
    assert "AI policy update." in out
    assert "Department of Commerce" in out


def test_first_two_sentences_falls_back_to_one_when_second_overflows():
    from jeeves.research_sectors import _first_two_sentences

    text = "First sentence. " + ("X" * 400) + "."
    out = _first_two_sentences(text, max_chars=300)
    assert out == "First sentence."


def test_first_two_sentences_returns_truncation_when_no_terminator():
    from jeeves.research_sectors import _first_two_sentences

    text = "no terminator at all in this entire string of text"
    out = _first_two_sentences(text, max_chars=20)
    assert out == "no terminator at all"


def test_collect_headlines_findings_like_keys_produce_distinguishing_label():
    """Family-style nested findings (choir/toddler) should produce a
    distinguishing label that survives cross-day matching.

    2026-05-21 — replaced two-sentence-prefix scheme with proper-noun
    cluster extraction (Flaw 2 fix). What matters for the dedup signal is
    that the OUTPUT contains the entry's distinguishing detail — exact
    label shape (two sentences vs. proper-noun cluster) is internal."""
    from jeeves.research_sectors import collect_headlines_from_sector

    value = {
        "toddler": (
            "Lynnwood library storytime. Baby Storytime Thursdays 10:30am "
            "in the children's wing."
        ),
        "urls": [],
    }
    out = collect_headlines_from_sector(value)
    # Output must contain SOMETHING that uniquely identifies this entry.
    # "Baby Storytime Thursdays" is a 3-token proper-noun cluster — exactly
    # the kind of distinguishing detail dedup needs.
    assert any("Baby Storytime" in h for h in out), (
        f"distinguishing label missing: {out}"
    )


def test_find_cross_sector_dupes_identifies_repeated_urls():
    from jeeves.research_sectors import _find_cross_sector_dupes

    session = {
        "global_news": [
            {"source": "BBC", "urls": ["https://propublica.org/x"]},
        ],
        "intellectual_journals": [
            {"source": "ProPublica", "urls": ["https://propublica.org/x"]},
        ],
        "enriched_articles": [
            {"url": "https://propublica.org/x", "urls": ["https://propublica.org/x"]},
        ],
        "wearable_ai": [],
        "local_news": [],
    }
    dupes = _find_cross_sector_dupes(session)
    assert "https://propublica.org/x" in dupes
    assert len(dupes) == 1


def test_find_cross_sector_dupes_ignores_single_sector_urls():
    from jeeves.research_sectors import _find_cross_sector_dupes

    session = {
        "global_news": [{"source": "BBC", "urls": ["https://bbc.com/a"]}],
        "intellectual_journals": [
            {"source": "Aeon", "urls": ["https://aeon.co/b"]},
        ],
        "wearable_ai": [],
        "local_news": [],
        "enriched_articles": [],
    }
    assert _find_cross_sector_dupes(session) == []


def test_find_cross_sector_dupes_handles_missing_or_malformed_fields():
    from jeeves.research_sectors import _find_cross_sector_dupes

    session = {
        "global_news": "not a list",
        "intellectual_journals": [None, "string item", {"urls": "not a list"}],
        # No other fields present
    }
    # Should not crash, should return empty list
    assert _find_cross_sector_dupes(session) == []


def test_dedup_schema_has_cross_sector_dupes_field():
    """Dedup model must expose cross_sector_dupes for the write phase."""
    from jeeves.schema import Dedup

    d = Dedup(
        covered_urls=[],
        covered_headlines=[],
        cross_sector_dupes=["https://example.com/a"],
    )
    assert d.cross_sector_dupes == ["https://example.com/a"]
    # Default is empty list
    assert Dedup().cross_sector_dupes == []


# ---------------------------------------------------------------------------
# M2: JEEVES_USE_CRAWL4AI_RESEARCH flag + _CRAWL4AI_ELIGIBLE_SECTORS
# ---------------------------------------------------------------------------

def test_crawl4ai_eligible_sectors_defined():
    from jeeves.research_sectors import _CRAWL4AI_ELIGIBLE_SECTORS
    assert _CRAWL4AI_ELIGIBLE_SECTORS == frozenset({
        "local_news", "global_news", "weather", "career", "family", "wearable_ai"
    })


def test_crawl4ai_eligible_sectors_excludes_deep():
    from jeeves.research_sectors import _CRAWL4AI_ELIGIBLE_SECTORS
    deep = {"triadic_ontology", "ai_systems", "uap"}
    assert not (deep & _CRAWL4AI_ELIGIBLE_SECTORS)


def test_crawl4ai_eligible_sectors_excludes_newyorker():
    from jeeves.research_sectors import _CRAWL4AI_ELIGIBLE_SECTORS
    assert "newyorker" not in _CRAWL4AI_ELIGIBLE_SECTORS


def test_sector_search_queries_covers_eligible_sectors():
    from jeeves.research_sectors import _CRAWL4AI_ELIGIBLE_SECTORS, _SECTOR_SEARCH_QUERIES
    for sector in _CRAWL4AI_ELIGIBLE_SECTORS:
        assert sector in _SECTOR_SEARCH_QUERIES, f"missing query for {sector}"
        assert _SECTOR_SEARCH_QUERIES[sector].strip(), f"empty query for {sector}"


async def test_run_sector_uses_crawl4ai_when_flag_set(monkeypatch):
    """flag=True + eligible sector → _run_crawl4ai_sector called instead of FunctionAgent."""
    import jeeves.research_sectors as rs
    from datetime import date
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger
    import threading

    crawl4ai_called: list[str] = []

    async def _fake_crawl4ai(cfg, spec, prior, ledger):
        crawl4ai_called.append(spec.name)
        return spec.default

    monkeypatch.setattr(rs, "_run_crawl4ai_sector", _fake_crawl4ai)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    await rs.run_sector(cfg, spec, [], ledger)

    assert crawl4ai_called == ["local_news"]


async def test_run_sector_skips_crawl4ai_for_deep_sectors(monkeypatch):
    """Deep sectors always use FunctionAgent regardless of flag."""
    import jeeves.research_sectors as rs
    from datetime import date
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger
    import threading

    crawl4ai_called: list[str] = []

    async def _fake_crawl4ai(cfg, spec, prior, ledger):
        crawl4ai_called.append(spec.name)
        return spec.default

    # No LLM keys → both builders return None → run_sector returns spec.default early.
    monkeypatch.setattr(rs, "_run_crawl4ai_sector", _fake_crawl4ai)
    monkeypatch.setattr(rs, "_build_cerebras_llm", lambda max_tokens=8192: None)
    monkeypatch.setattr(rs, "_build_openrouter_llm", lambda max_tokens=8192, model=None: None)

    cfg = Config(
        nvidia_api_key="", serper_api_key="", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "triadic_ontology")
    await rs.run_sector(cfg, spec, [], ledger)

    assert "triadic_ontology" not in crawl4ai_called




# ---------------------------------------------------------------------------
# 2026-05-21 regression: _run_crawl4ai_sector must consume the
# make_serper_search WRAPPER shape (provider/query/results[].url), not the
# raw Serper API shape (organic[].link). Prior tests stubbed
# _run_crawl4ai_sector entirely and never exercised this contract — silent
# 0-URL starve for ~7+ days post-M2 ship.
# ---------------------------------------------------------------------------

async def test_run_crawl4ai_sector_reads_wrapper_results_key(monkeypatch):
    """The function must extract URLs from search_data['results'][i]['url'].

    Regression for 2026-05-21 silent starve: the original implementation
    keyed off 'organic'/'link' (raw API shape) but make_serper_search
    returns wrapped shape 'results'/'url' — every sector returned []
    fresh URLs and fell back to spec.default.
    """
    import json
    import threading
    from datetime import date

    import jeeves.research_sectors as rs
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    wrapped_serper_response = json.dumps({
        "provider": "serper",
        "query": "Edmonds WA news today",
        "results": [
            {"title": "MyEdmondsNews", "url": "https://example.com/a", "snippet": "", "provider": "serper"},
            {"title": "EdmondsBeacon", "url": "https://example.com/b", "snippet": "", "provider": "serper"},
            {"title": "KOMOnews",      "url": "https://example.com/c", "snippet": "", "provider": "serper"},
        ],
    })

    def _fake_make_serper_search(cfg, ledger):
        def _serper_search(query: str = "", num: int = 10, tbs=None):
            return wrapped_serper_response
        return _serper_search

    extracted_urls: list[str] = []

    async def _fake_batch_extract(urls, query=None, max_chars=6000):
        extracted_urls.extend(urls)
        return [(f"body text for {u}", "trafilatura") for u in urls]

    monkeypatch.setattr("jeeves.tools.serper.make_serper_search", _fake_make_serper_search)
    monkeypatch.setattr("jeeves.tools.crawl4ai_extract.batch_extract", _fake_batch_extract)
    # Synthesis LLM call must short-circuit so test stays hermetic — fake
    # Cerebras builder returning None forces the function to early-return
    # via its own fallback. The test only cares that URLs reach batch_extract.
    monkeypatch.setattr(rs, "_build_cerebras_llm", lambda max_tokens=8192: None)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    await rs._run_crawl4ai_sector(cfg, spec, [], ledger)

    # If the schema bug returns (organic/link), extracted_urls stays empty.
    assert extracted_urls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ], f"crawl4ai schema regression: got {extracted_urls!r}"


async def test_run_crawl4ai_sector_filters_prior_urls(monkeypatch):
    """Wrapper-shape URLs must still be dedup-filtered against prior_urls_sample."""
    import json
    import threading
    from datetime import date

    import jeeves.research_sectors as rs
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    wrapped = json.dumps({
        "provider": "serper",
        "query": "q",
        "results": [
            {"url": "https://example.com/keep"},
            {"url": "https://example.com/drop"},
        ],
    })

    def _fake_make_serper_search(cfg, ledger):
        return lambda query="", num=10, tbs=None: wrapped

    seen: list[str] = []

    async def _fake_batch_extract(urls, query=None, max_chars=6000):
        seen.extend(urls)
        return [(f"body for {u}", "trafilatura") for u in urls]

    monkeypatch.setattr("jeeves.tools.serper.make_serper_search", _fake_make_serper_search)
    monkeypatch.setattr("jeeves.tools.crawl4ai_extract.batch_extract", _fake_batch_extract)
    monkeypatch.setattr(rs, "_build_cerebras_llm", lambda max_tokens=8192: None)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    await rs._run_crawl4ai_sector(
        cfg, spec, ["https://example.com/drop"], ledger,
    )

    assert seen == ["https://example.com/keep"]


# ---------------------------------------------------------------------------
# 2026-05-21 regression: _run_crawl4ai_sector and run_sector must call
# _rotate_on_429 when Cerebras returns a 429, NOT sleep-retry the same model.
# M4 unit tests covered _rotate_on_429 in isolation but the WIRING was never
# exercised — production confirmed 7 consecutive 429s on gpt-oss-120b before
# falling to OR. These tests pin the wiring.
# ---------------------------------------------------------------------------


async def test_run_crawl4ai_sector_rotates_cerebras_on_429(monkeypatch):
    """Synthesis call 429 on first Cerebras model → rotate to next → succeed."""
    import json
    import threading
    from datetime import date

    import jeeves.research_sectors as rs
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    # Reset module-level Cerebras state so prior tests don't bleed.
    rs._RESOLVED_CEREBRAS_MODEL = None
    rs._CEREBRAS_TRIED_MODELS = set()

    wrapped = json.dumps({
        "provider": "serper",
        "query": "q",
        "results": [{"url": "https://example.com/a"}],
    })

    def _fake_make_serper_search(cfg, ledger):
        return lambda query="", num=10, tbs=None: wrapped

    async def _fake_batch_extract(urls, query=None, max_chars=6000):
        return [(f"body for {u}", "trafilatura") for u in urls]

    # Cerebras "model" registry: first model 429s, second succeeds.
    builds: list[str] = []  # models requested via _build_cerebras_llm

    class _FakeLLM:
        def __init__(self, model):
            self.model = model

        async def achat(self, messages):
            if self.model == "gpt-oss-120b":
                # Mimic the openai SDK shape used by _is_nim_rate_limit.
                raise Exception(
                    "Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}"
                )
            from llama_index.core.llms import ChatResponse, ChatMessage as _CM
            return ChatResponse(message=_CM(role="assistant", content='[]'))

    def _fake_build_cerebras_llm(max_tokens=8192):
        # Simulate _resolve_cerebras_model walking the chain. First call
        # returns gpt-oss-120b; after _rotate_on_429 marks it TRIED, the
        # second call returns the next chain member that's "available".
        if "gpt-oss-120b" not in rs._CEREBRAS_TRIED_MODELS:
            model = "gpt-oss-120b"
        else:
            # Whatever the chain picks next is fine for this test.
            model = next(
                m for m in rs._CEREBRAS_MODEL_CHAIN
                if m not in rs._CEREBRAS_TRIED_MODELS
            )
        builds.append(model)
        return _FakeLLM(model)

    monkeypatch.setattr("jeeves.tools.serper.make_serper_search", _fake_make_serper_search)
    monkeypatch.setattr("jeeves.tools.crawl4ai_extract.batch_extract", _fake_batch_extract)
    monkeypatch.setattr(rs, "_build_cerebras_llm", _fake_build_cerebras_llm)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    await rs._run_crawl4ai_sector(cfg, spec, [], ledger)

    # Wiring assertions:
    # 1. The 429'd model is in TRIED set (proves _rotate_on_429 fired).
    assert "gpt-oss-120b" in rs._CEREBRAS_TRIED_MODELS, (
        f"rotation never fired; TRIED set: {rs._CEREBRAS_TRIED_MODELS!r}"
    )
    # 2. At least two distinct models were built (rotation actually rebuilt).
    assert len(builds) >= 2, f"only built {len(builds)} model(s): {builds!r}"
    assert builds[0] == "gpt-oss-120b"
    assert builds[1] != "gpt-oss-120b", f"rotation didn't advance: {builds!r}"


async def test_run_crawl4ai_sector_returns_default_when_chain_exhausted(monkeypatch):
    """All Cerebras AND OR models exhausted → returns spec.default."""
    import json
    import threading
    from datetime import date

    import jeeves.research_sectors as rs
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    rs._RESOLVED_CEREBRAS_MODEL = None
    rs._CEREBRAS_TRIED_MODELS = set()
    rs._OPENROUTER_TRIED_MODELS = set()

    wrapped = json.dumps({
        "provider": "serper", "query": "q",
        "results": [{"url": "https://example.com/a"}],
    })

    def _fake_make_serper_search(cfg, ledger):
        return lambda query="", num=10, tbs=None: wrapped

    async def _fake_batch_extract(urls, query=None, max_chars=6000):
        return [(f"body for {u}", "trafilatura") for u in urls]

    # Every model 429s; rotation should bottom out and the function returns default.
    class _AllRateLimitedLLM:
        def __init__(self, model):
            self.model = model

        async def achat(self, messages):
            raise Exception(
                "Error code: 429 - {'error': {'code': 'rate_limit_exceeded'}}"
            )

    def _fake_build_cerebras_llm(max_tokens=8192):
        untried = [
            m for m in rs._CEREBRAS_MODEL_CHAIN
            if m not in rs._CEREBRAS_TRIED_MODELS
        ]
        if not untried:
            return None  # _resolve_cerebras_model returns None
        return _AllRateLimitedLLM(untried[0])

    # 2026-05-21 round 6: _run_crawl4ai_sector now falls to OR after
    # Cerebras exhausts. Stub OR to also 429 on every entry so the
    # exhaustion path still bottoms out at spec.default.
    def _fake_build_openrouter_llm(max_tokens=4096, model=None):
        untried = [
            m for m in rs._OPENROUTER_MODEL_CHAIN
            if m not in rs._OPENROUTER_TRIED_MODELS
        ]
        if not untried:
            return None
        return _AllRateLimitedLLM(model or untried[0])

    monkeypatch.setattr("jeeves.tools.serper.make_serper_search", _fake_make_serper_search)
    monkeypatch.setattr("jeeves.tools.crawl4ai_extract.batch_extract", _fake_batch_extract)
    monkeypatch.setattr(rs, "_build_cerebras_llm", _fake_build_cerebras_llm)
    monkeypatch.setattr(rs, "_build_openrouter_llm", _fake_build_openrouter_llm)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    result = await rs._run_crawl4ai_sector(cfg, spec, [], ledger)

    assert result == spec.default, "exhaustion must return spec.default"
    # Cerebras chain iterated (proves Phase 1 rotation ran).
    assert len(rs._CEREBRAS_TRIED_MODELS) >= 2, (
        f"cerebras rotation didn't iterate; TRIED: "
        f"{rs._CEREBRAS_TRIED_MODELS!r}"
    )
    # OR chain also iterated (proves Phase 2 rotation ran).
    assert len(rs._OPENROUTER_TRIED_MODELS) >= 2, (
        f"OR rotation didn't iterate; TRIED: "
        f"{rs._OPENROUTER_TRIED_MODELS!r}"
    )


# ---------------------------------------------------------------------------
# 2026-05-21 round 4: OpenRouter model rotation primitives + paid backstop.
# Mirror of the Cerebras rotation tests but for the OR chain. Verifies
# _rotate_openrouter_on_429 advances the chain, that :floor paid entries
# are present, and that exhaustion returns None.
# ---------------------------------------------------------------------------

def test_openrouter_chain_includes_paid_backstop():
    import jeeves.research_sectors as rs
    paid_entries = [m for m in rs._OPENROUTER_MODEL_CHAIN if m.endswith(":floor")]
    assert len(paid_entries) >= 1, (
        f"chain must include at least one :floor (paid) entry; got "
        f"{rs._OPENROUTER_MODEL_CHAIN!r}"
    )
    # And free entries must precede paid entries — try free first.
    first_paid_idx = next(
        i for i, m in enumerate(rs._OPENROUTER_MODEL_CHAIN) if m.endswith(":floor")
    )
    free_before_paid = all(
        m.endswith(":free") for m in rs._OPENROUTER_MODEL_CHAIN[:first_paid_idx]
    )
    assert free_before_paid, (
        f"all entries before first :floor must be :free; got "
        f"{rs._OPENROUTER_MODEL_CHAIN[:first_paid_idx]!r}"
    )


def test_rotate_openrouter_on_429_advances_chain():
    import jeeves.research_sectors as rs
    rs._OPENROUTER_TRIED_MODELS = set()

    first = rs._OPENROUTER_MODEL_CHAIN[0]
    next_model = rs._rotate_openrouter_on_429(first)

    assert next_model is not None, "rotation should return next chain entry"
    assert next_model != first, "rotation must not return the failed model"
    assert next_model in rs._OPENROUTER_MODEL_CHAIN
    assert first in rs._OPENROUTER_TRIED_MODELS


def test_rotate_openrouter_on_429_exhaustion_returns_none():
    import jeeves.research_sectors as rs
    rs._OPENROUTER_TRIED_MODELS = set(rs._OPENROUTER_MODEL_CHAIN)

    result = rs._rotate_openrouter_on_429(rs._OPENROUTER_MODEL_CHAIN[0])

    assert result is None, "exhausted chain must return None"


def test_build_openrouter_llm_skips_tried_models(monkeypatch):
    """Default-model resolution must pick the first UNTRIED entry, not chain[0]."""
    import jeeves.research_sectors as rs

    rs._OPENROUTER_TRIED_MODELS = set()
    # Mark the first entry as tried so the builder must pick the second.
    rs._OPENROUTER_TRIED_MODELS.add(rs._OPENROUTER_MODEL_CHAIN[0])

    captured: dict = {}

    class _Fake:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr("llama_index.llms.openai_like.OpenAILike", _Fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "fake-key")

    rs._build_openrouter_llm(max_tokens=4096)

    assert captured.get("model") == rs._OPENROUTER_MODEL_CHAIN[1], (
        f"builder picked {captured.get('model')!r}; expected "
        f"{rs._OPENROUTER_MODEL_CHAIN[1]!r} (first untried)"
    )


# ---------------------------------------------------------------------------
# 2026-05-21 round 5 regression: _is_or_dead_endpoint detects OR 404 dead
# routes (e.g., qwen-2.5-72b-instruct:free in May 2026). Production Run #48
# confirmed every sector that fell through to OR hit the qwen 404 wall and
# crashed silently because the catch-all `else` branch returned spec.default
# instead of rotating to the next chain entry. Helper + wiring now treat
# 404 "No endpoints found" as a rotation trigger, same as 429.
# ---------------------------------------------------------------------------

def test_is_or_dead_endpoint_detects_404_no_endpoints():
    import jeeves.research_sectors as rs
    exc = Exception(
        "Error code: 404 - {'error': {'message': 'No endpoints found for "
        "qwen/qwen-2.5-72b-instruct:free.', 'code': 404}, 'user_id': 'user_xxx'}"
    )
    assert rs._is_or_dead_endpoint(exc) is True


def test_is_or_dead_endpoint_ignores_429():
    import jeeves.research_sectors as rs
    exc = Exception(
        "Error code: 429 - {'error': {'message': 'Rate limit exceeded', "
        "'code': 429}}"
    )
    assert rs._is_or_dead_endpoint(exc) is False


def test_is_or_dead_endpoint_ignores_generic_404():
    import jeeves.research_sectors as rs
    # A 404 without the "No endpoints found" phrase is some other problem
    # (e.g., model name typo) — don't trigger rotation on it.
    exc = Exception("Error code: 404 - {'error': {'message': 'Not Found'}}")
    assert rs._is_or_dead_endpoint(exc) is False


def test_or_chain_no_longer_includes_dead_qwen_route():
    """OR chain must not include qwen/qwen-2.5-72b-instruct:free (deprecated)."""
    import jeeves.research_sectors as rs
    assert "qwen/qwen-2.5-72b-instruct:free" not in rs._OPENROUTER_MODEL_CHAIN, (
        "deprecated route — OR returns 404 'No endpoints found' as of 2026-05"
    )


def test_cerebras_chain_no_longer_includes_llama_8b():
    """8b context window (8192) too small for deep sectors. Drop entirely."""
    import jeeves.research_sectors as rs
    assert "llama3.1-8b" not in rs._CEREBRAS_MODEL_CHAIN, (
        "llama3.1-8b crashes deep sectors with context_length_exceeded — "
        "keep out of chain"
    )


# ---------------------------------------------------------------------------
# 2026-05-21 round 6: _run_crawl4ai_sector synthesis must fall through to
# OpenRouter rotation when Cerebras chain is exhausted. Run #48 confirmed
# Crawl4AI sectors were starved when deep sectors burnt the Cerebras chain
# before they ran. Phase 1 (Cerebras) → Phase 2 (OR) cascade now covers it.
# ---------------------------------------------------------------------------

async def test_run_crawl4ai_sector_falls_to_or_when_cerebras_exhausted(monkeypatch):
    """Cerebras chain returns None → OR rotation phase fires → success."""
    import json
    import threading
    from datetime import date

    import jeeves.research_sectors as rs
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    rs._RESOLVED_CEREBRAS_MODEL = None
    rs._CEREBRAS_TRIED_MODELS = set(rs._CEREBRAS_MODEL_CHAIN)  # pretend exhausted
    rs._OPENROUTER_TRIED_MODELS = set()

    wrapped = json.dumps({
        "provider": "serper", "query": "q",
        "results": [{"url": "https://example.com/a"}],
    })

    def _fake_make_serper_search(cfg, ledger):
        return lambda query="", num=10, tbs=None: wrapped

    async def _fake_batch_extract(urls, query=None, max_chars=6000):
        return [(f"body for {u}", "trafilatura") for u in urls]

    # Cerebras builder always returns None (chain exhausted).
    monkeypatch.setattr(rs, "_build_cerebras_llm", lambda max_tokens=8192: None)

    or_calls: list[str] = []

    class _OrLLM:
        def __init__(self, model):
            self.model = model

        async def achat(self, messages):
            or_calls.append(self.model)
            from llama_index.core.llms import (
                ChatResponse, ChatMessage as _CM,
            )
            return ChatResponse(message=_CM(role="assistant", content='[]'))

    def _fake_build_openrouter_llm(max_tokens=4096, model=None):
        # Mimic _next_untried_openrouter_model logic.
        for candidate in rs._OPENROUTER_MODEL_CHAIN:
            if candidate not in rs._OPENROUTER_TRIED_MODELS:
                return _OrLLM(model or candidate)
        return None

    monkeypatch.setattr("jeeves.tools.serper.make_serper_search", _fake_make_serper_search)
    monkeypatch.setattr("jeeves.tools.crawl4ai_extract.batch_extract", _fake_batch_extract)
    monkeypatch.setattr(rs, "_build_openrouter_llm", _fake_build_openrouter_llm)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    result = await rs._run_crawl4ai_sector(cfg, spec, [], ledger)

    # OR was reached + a call was made.
    assert len(or_calls) >= 1, (
        f"OR phase never fired; or_calls={or_calls!r}"
    )
    # Result is the parsed sector default for empty JSON — but the key
    # assertion is that the function got TO synthesis instead of bailing
    # at "Cerebras unavailable" in phase 1.


async def test_run_crawl4ai_sector_rotates_or_on_dead_endpoint(monkeypatch):
    """OR 404 'No endpoints found' must rotate to next OR model, not return default."""
    import json
    import threading
    from datetime import date

    import jeeves.research_sectors as rs
    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    rs._RESOLVED_CEREBRAS_MODEL = None
    rs._CEREBRAS_TRIED_MODELS = set(rs._CEREBRAS_MODEL_CHAIN)
    rs._OPENROUTER_TRIED_MODELS = set()

    wrapped = json.dumps({
        "provider": "serper", "query": "q",
        "results": [{"url": "https://example.com/a"}],
    })

    def _fake_make_serper_search(cfg, ledger):
        return lambda query="", num=10, tbs=None: wrapped

    async def _fake_batch_extract(urls, query=None, max_chars=6000):
        return [(f"body for {u}", "trafilatura") for u in urls]

    monkeypatch.setattr(rs, "_build_cerebras_llm", lambda max_tokens=8192: None)

    or_calls: list[str] = []
    first_model = rs._OPENROUTER_MODEL_CHAIN[0]

    class _OrLLM:
        def __init__(self, model):
            self.model = model

        async def achat(self, messages):
            or_calls.append(self.model)
            if self.model == first_model:
                # Simulate OR's deprecated-route response.
                raise Exception(
                    "Error code: 404 - {'error': {'message': 'No endpoints "
                    f"found for {self.model}.', 'code': 404}}"
                )
            from llama_index.core.llms import (
                ChatResponse, ChatMessage as _CM,
            )
            return ChatResponse(message=_CM(role="assistant", content='[]'))

    def _fake_build_openrouter_llm(max_tokens=4096, model=None):
        if model is None:
            for candidate in rs._OPENROUTER_MODEL_CHAIN:
                if candidate not in rs._OPENROUTER_TRIED_MODELS:
                    model = candidate
                    break
        return _OrLLM(model) if model else None

    monkeypatch.setattr("jeeves.tools.serper.make_serper_search", _fake_make_serper_search)
    monkeypatch.setattr("jeeves.tools.crawl4ai_extract.batch_extract", _fake_batch_extract)
    monkeypatch.setattr(rs, "_build_openrouter_llm", _fake_build_openrouter_llm)

    cfg = Config(
        nvidia_api_key="", serper_api_key="k", tavily_api_key="", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="r/r",
        run_date=date(2026, 5, 21),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    ledger._lock = threading.Lock()

    spec = next(s for s in rs.SECTOR_SPECS if s.name == "local_news")
    await rs._run_crawl4ai_sector(cfg, spec, [], ledger)

    # Both first (dead) and second model were called — rotation fired.
    assert len(or_calls) >= 2, (
        f"OR rotation didn't advance past 404; or_calls={or_calls!r}"
    )
    assert or_calls[0] == first_model
    assert or_calls[1] != first_model
    # And the first model is now in TRIED set (proves _rotate_openrouter_on_429 fired).
    assert first_model in rs._OPENROUTER_TRIED_MODELS


# ---------------------------------------------------------------------------
# 2026-05-21 round 7 fixes
# ---------------------------------------------------------------------------

def test_parse_sector_output_drops_bare_url_strings_from_enriched():
    """OR models sometimes return a flat list of URL strings instead of
    EnrichedArticle dicts.  _parse_sector_output must filter them out so
    save_session doesn't crash on Pydantic validation.
    """
    raw = (
        '["https://arxiv.org/pdf/2603.28986",'
        ' "https://arxiv.org/pdf/2604.01007v2",'
        ' "https://github.com/Human-Agent-Society/CORAL"]'
    )
    spec = next(s for s in SECTOR_SPECS if s.name == "enriched_articles")
    out = _parse_sector_output(raw, spec)
    assert isinstance(out, list)
    assert out == [], (
        "bare URL strings must be filtered — result should be empty list, "
        f"got {out!r}"
    )


def test_parse_sector_output_keeps_valid_enriched_dicts():
    """Valid EnrichedArticle dicts are preserved; bare strings mixed in are dropped."""
    raw = (
        '["https://arxiv.org/pdf/discard",'
        ' {"url": "https://arxiv.org/abs/2603.28986",'
        '  "title": "Some Paper", "text": "Abstract here."}]'
    )
    spec = next(s for s in SECTOR_SPECS if s.name == "enriched_articles")
    out = _parse_sector_output(raw, spec)
    assert len(out) == 1
    assert out[0]["title"] == "Some Paper"


def test_is_retryable_network_error_matches_connection_error():
    """'Connection error.' (httpx.ConnectError) must be retryable."""
    import jeeves.research_sectors as rs
    exc = Exception("Connection error.")
    assert rs._is_retryable_network_error(exc), (
        "'Connection error.' should be retryable so crawl4ai OR rotation fires"
    )


def test_cerebras_ctx_banned_excludes_llama_8b():
    """llama3.1-8b must be in _CEREBRAS_CTX_BANNED to prevent fallback picks."""
    import jeeves.research_sectors as rs
    assert "llama3.1-8b" in rs._CEREBRAS_CTX_BANNED, (
        "llama3.1-8b has 8192-ctx — too small for deep sectors; "
        "must be banned from _resolve_cerebras_model fallback"
    )
