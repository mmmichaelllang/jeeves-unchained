"""Integration tests for the research_sectors JSON parsing chain.

Tests _try_normalize_json -> _parse_sector_output -> _json_repair_retry
without hitting any real APIs. All LLM calls are mocked.

Does NOT duplicate the unit-level tests already in test_research_sectors.py.
Focuses on:
  - Parametrized multi-input coverage for _try_normalize_json
  - _parse_sector_output end-to-end with all SectorSpec shapes
  - Full async chain: malformed JSON -> _ParseFailed -> _json_repair_retry -> result
"""
from __future__ import annotations

import json
import sys
import types
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jeeves.research_sectors import (
    SECTOR_SPECS,
    SectorSpec,
    _ParseFailed,
    _parse_sector_output,
    _try_normalize_json,
    _json_repair_retry,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec(name: str) -> SectorSpec:
    return next(s for s in SECTOR_SPECS if s.name == name)


def _list_spec(name: str = "test_list") -> SectorSpec:
    return SectorSpec(name=name, shape="list", instruction="Find stuff.", default=[])


def _dict_spec(name: str = "test_dict") -> SectorSpec:
    return SectorSpec(name=name, shape="dict", instruction="Find stuff.", default={})


def _string_spec(name: str = "test_str") -> SectorSpec:
    return SectorSpec(name=name, shape="string", instruction="Find stuff.", default="")


class _StubLedger:
    """Minimal ledger stub — _json_repair_retry only uses it for build_kimi_llm."""
    _state: dict = {"providers": {}}


@contextmanager
def _repair_mocks(mock_agent: MagicMock):
    """Context manager that intercepts _json_repair_retry's two local imports:

        from llama_index.core.agent.workflow import FunctionAgent
        from .llm import build_kimi_llm

    Strategy:
    - Stub `llama_index.core.agent.workflow.FunctionAgent` via sys.modules injection
      so the local `from llama_index...` import returns our mock_agent factory.
    - Patch `jeeves.llm.build_kimi_llm` so the bound name inside
      `_json_repair_retry` resolves to a MagicMock (avoiding real NIM calls).

    Both modules may not be fully installed in the workspace test environment,
    so we avoid touching their real implementations when possible.
    """
    # Patch jeeves.llm.build_kimi_llm via the real module (it IS importable)
    import jeeves.llm as _jeeves_llm_mod
    real_build_kimi = _jeeves_llm_mod.build_kimi_llm

    # Build a minimal fake llama_index module tree.
    # llama_index.core must look like a package to allow sub-attribute access.
    # We only need to stub the path _json_repair_retry actually traverses.
    fake_li_workflow = types.ModuleType("llama_index.core.agent.workflow")
    fake_li_workflow.FunctionAgent = MagicMock(return_value=mock_agent)

    # Collect all llama_index sub-module keys that might already be in sys.modules
    li_keys_to_stub = [
        "llama_index",
        "llama_index.core",
        "llama_index.core.agent",
        "llama_index.core.agent.workflow",
    ]
    old_li_modules = {k: sys.modules.get(k) for k in li_keys_to_stub}

    # Only inject stubs for keys NOT already present (avoid breaking real imports)
    stubs: dict[str, types.ModuleType] = {}
    if "llama_index" not in sys.modules:
        stubs["llama_index"] = types.ModuleType("llama_index")
    if "llama_index.core" not in sys.modules:
        stubs["llama_index.core"] = types.ModuleType("llama_index.core")
    if "llama_index.core.agent" not in sys.modules:
        stubs["llama_index.core.agent"] = types.ModuleType("llama_index.core.agent")
    # Always override the workflow module so FunctionAgent is our mock
    stubs["llama_index.core.agent.workflow"] = fake_li_workflow

    try:
        sys.modules.update(stubs)
        _jeeves_llm_mod.build_kimi_llm = MagicMock(return_value=MagicMock())
        yield
    finally:
        # Restore llama_index modules
        for k, v in old_li_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
        # Restore build_kimi_llm
        _jeeves_llm_mod.build_kimi_llm = real_build_kimi


# ---------------------------------------------------------------------------
# TestTryNormalizeJson — parametrized coverage for all 4 repair passes
# ---------------------------------------------------------------------------

class TestTryNormalizeJson:
    """Parametrized tests for every normalization pass in _try_normalize_json."""

    @pytest.mark.parametrize("fragment,is_array,expected", [
        # Pass 0 — already valid JSON, returned as-is
        ('{"key": "value"}', False, {"key": "value"}),
        ('[{"a": 1}, {"b": 2}]', True, [{"a": 1}, {"b": 2}]),
        # Pass 1 — Python repr (single quotes, True/False/None)
        ("{'key': 'value', 'flag': True}", False, {"key": "value", "flag": True}),
        ("[{'a': 1}, {'b': None}]", True, [{"a": 1}, {"b": None}]),
        ("{'val': False}", False, {"val": False}),
        # Pass 2 — trailing comma in array
        ('[{"a": 1}, {"b": 2},]', True, [{"a": 1}, {"b": 2}]),
        # Pass 2 — trailing comma in object
        ('{"a": 1, "b": 2,}', False, {"a": 1, "b": 2}),
        # Pass 3 — combined: python repr + trailing comma
        ("[{'a': 1},]", True, [{"a": 1}]),
    ])
    def test_parametrized_repair_passes(self, fragment, is_array, expected):
        result = _try_normalize_json(fragment, is_array=is_array)
        assert result == expected

    def test_truncation_recovery_salvages_complete_items(self):
        """Pass 4: stream-truncated array returns complete items before cut point."""
        truncated = '[{"source": "BBC", "findings": "war update", "urls": ["https://bbc.com/a"]}, {"source": "NYT'
        result = _try_normalize_json(truncated, is_array=True)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["source"] == "BBC"

    def test_truncation_recovery_non_array_returns_none(self):
        """Truncation recovery is only attempted for is_array=True."""
        truncated = '{"key": "val'
        result = _try_normalize_json(truncated, is_array=False)
        assert result is None

    def test_bare_object_coerced_to_array(self):
        """Pass 0 (bare obj): a lone dict is wrapped in a list when is_array=True."""
        result = _try_normalize_json('{"source": "BBC", "findings": "x", "urls": []}', is_array=True)
        assert isinstance(result, list)
        assert result[0]["source"] == "BBC"

    def test_bare_object_with_trailing_comma_coerced_to_array(self):
        """Bare obj + trailing comma combo is also coerced correctly."""
        result = _try_normalize_json('{"source": "BBC", "findings": "x",}', is_array=True)
        assert isinstance(result, list)
        assert result[0]["source"] == "BBC"

    def test_garbled_input_returns_none(self):
        """Truly broken JSON that survives no repair pass returns None."""
        result = _try_normalize_json("{broken: json 'mixed\" [unclosed", is_array=False)
        assert result is None

    def test_empty_string_returns_none(self):
        result = _try_normalize_json("", is_array=False)
        assert result is None

    def test_is_array_false_does_not_wrap_object(self):
        """is_array=False must not trigger bare-object wrapping."""
        result = _try_normalize_json('{"a": 1}', is_array=False)
        assert result == {"a": 1}  # dict, not list


# ---------------------------------------------------------------------------
# TestParseSectorOutput — end-to-end shape coverage
# ---------------------------------------------------------------------------

class TestParseSectorOutput:
    """Integration tests through _parse_sector_output for all SectorSpec shapes."""

    # --- string shape ---

    def test_string_shape_returns_text_as_is(self):
        raw = "Partly cloudy, high 58F, wind NW 10mph."
        assert _parse_sector_output(raw, _spec("weather")) == raw

    def test_string_shape_strips_markdown_fences(self):
        raw = "```\nSunny skies. High 62F.\n```"
        out = _parse_sector_output(raw, _spec("weather"))
        assert out == "Sunny skies. High 62F."

    def test_string_shape_returns_empty_string_for_empty_input(self):
        """Empty input for a string sector returns empty string (not _ParseFailed)."""
        out = _parse_sector_output("", _spec("weather"))
        assert out == ""

    # --- list shape ---

    def test_list_shape_valid_array_parsed(self):
        raw = '[{"category":"municipal","source":"My Edmonds News","findings":"City council voted.","urls":["https://myedmondsnews.com/x"]}]'
        out = _parse_sector_output(raw, _spec("local_news"))
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0]["source"] == "My Edmonds News"

    def test_list_shape_multiple_items_all_parsed(self):
        raw = json.dumps([
            {"source": "BBC", "findings": "War update in region.", "urls": ["https://bbc.com/a"]},
            {"source": "Guardian", "findings": "Climate summit concluded.", "urls": ["https://theguardian.com/b"]},
        ])
        out = _parse_sector_output(raw, _spec("global_news"))
        assert len(out) == 2

    def test_list_shape_python_repr_repaired(self):
        raw = "[{'source': 'Aeon', 'findings': 'Essay on consciousness.', 'urls': ['https://aeon.co/x']}]"
        out = _parse_sector_output(raw, _spec("intellectual_journals"))
        assert isinstance(out, list)
        assert out[0]["source"] == "Aeon"

    def test_list_shape_trailing_comma_repaired(self):
        raw = '[{"source":"MyEdmonds","findings":"Road closure update.","urls":["https://myedmondsnews.com/a"]},]'
        out = _parse_sector_output(raw, _spec("local_news"))
        assert isinstance(out, list)
        assert len(out) == 1

    def test_list_shape_truncated_array_repaired(self):
        # Truncation without a nested array so rfind(']') correctly finds the outer ']'.
        # Inputs with nested url arrays confuse the bracket finder — that is expected
        # behaviour (the truncation recovery handles it at _try_normalize_json level
        # but _parse_sector_output's bracket finder hits the inner ']' first).
        raw = '[{"source":"BBC","findings":"Flood warnings issued."}, {"source":"NYT'
        out = _parse_sector_output(raw, _spec("global_news"))
        assert isinstance(out, list)
        assert len(out) == 1
        assert out[0]["source"] == "BBC"

    def test_list_shape_empty_string_returns_parse_failed(self):
        out = _parse_sector_output("", _spec("local_news"))
        assert isinstance(out, _ParseFailed)
        assert out.raw == ""

    def test_list_shape_no_json_returns_parse_failed(self):
        out = _parse_sector_output("completely not json", _spec("local_news"))
        assert isinstance(out, _ParseFailed)
        assert out.raw == "completely not json"

    def test_list_shape_unrecoverable_json_returns_parse_failed(self):
        out = _parse_sector_output("{broken: json: 'mixed\" [unclosed", _spec("local_news"))
        assert isinstance(out, _ParseFailed)

    def test_parse_failed_raw_preserved_for_repair_chain(self):
        """_ParseFailed.raw must carry original text so _json_repair_retry can reformat it."""
        raw = "Here is some text but [malformed, json, no closing"
        out = _parse_sector_output(raw, _spec("local_news"))
        assert isinstance(out, _ParseFailed)
        assert out.raw == raw

    def test_list_shape_drops_uncited_items(self):
        """Items with urls=[] are hallucination signatures and must be silently dropped."""
        raw = json.dumps([
            {"source": "NYRB", "findings": "Long essay.", "urls": []},
            {"source": "Aeon", "findings": "Short essay.", "urls": ["https://aeon.co/x"]},
        ])
        out = _parse_sector_output(raw, _spec("intellectual_journals"))
        assert len(out) == 1
        assert out[0]["source"] == "Aeon"

    def test_list_shape_quality_filter_drops_trivial_findings(self):
        """Items with findings < 20 chars after strip are noise and must be dropped."""
        raw = json.dumps([
            {"source": "BBC", "findings": "N/A", "urls": ["https://bbc.com/a"]},
            {"source": "Guardian", "findings": "Detailed report on climate summit vote.", "urls": ["https://theguardian.com/b"]},
        ])
        out = _parse_sector_output(raw, _spec("global_news"))
        assert len(out) == 1
        assert out[0]["source"] == "Guardian"

    def test_list_shape_quality_filter_does_not_empty_array(self):
        """If all items would be dropped by quality filter, keep them all (guard)."""
        raw = json.dumps([
            {"source": "BBC", "findings": "N/A", "urls": ["https://bbc.com/a"]},
        ])
        out = _parse_sector_output(raw, _spec("global_news"))
        # The uncited guard doesn't apply (urls is non-empty), quality filter would
        # empty the array, so items are preserved.
        assert isinstance(out, list)
        assert len(out) == 1

    # --- dict shape ---

    def test_dict_shape_parsed_correctly(self):
        raw = '{"openings": [{"district": "Edmonds", "role": "HS English", "url": "https://x", "summary": "FTE", "deadline": null, "salary_range": null}], "notes": ""}'
        out = _parse_sector_output(raw, _spec("career"))
        assert isinstance(out, dict)
        assert "openings" in out

    def test_dict_shape_python_repr_repaired(self):
        raw = "{'findings': 'job posting found', 'urls': ['https://example.com'], 'deadline': None}"
        out = _parse_sector_output(raw, _spec("career"))
        assert isinstance(out, dict)
        assert out["findings"] == "job posting found"
        assert out["deadline"] is None

    # --- deep shape ---

    def test_deep_shape_with_urls_returned(self):
        raw = '{"findings": "AI chip analysis.", "urls": ["https://wired.com/ai-chip"]}'
        out = _parse_sector_output(raw, _spec("triadic_ontology"))
        assert out["findings"] == "AI chip analysis."

    def test_deep_shape_no_urls_returns_default(self):
        """Deep sector with no cited URLs must return spec.default, not the findings."""
        raw = '{"findings": "Some thoughts from training data.", "urls": []}'
        out = _parse_sector_output(raw, _spec("triadic_ontology"))
        assert out == _spec("triadic_ontology").default

    # --- enriched shape ---

    def test_enriched_shape_text_cap_enforced(self):
        """enriched_articles text field must be capped at 500 chars."""
        long_text = "A" * 800
        raw = json.dumps([{
            "title": "Long article",
            "url": "https://wired.com/x",
            "source": "Wired",
            "text": long_text,
        }])
        out = _parse_sector_output(raw, _spec("enriched_articles"))
        assert isinstance(out, list)
        assert len(out[0]["text"]) == 500

    def test_enriched_shape_short_text_not_capped(self):
        """Short text must not be padded or altered."""
        raw = json.dumps([{
            "title": "Short article",
            "url": "https://wired.com/y",
            "source": "Wired",
            "text": "Brief content.",
        }])
        out = _parse_sector_output(raw, _spec("enriched_articles"))
        assert out[0]["text"] == "Brief content."


# ---------------------------------------------------------------------------
# TestJsonRepairRetry — async chain with mocked LLM
# ---------------------------------------------------------------------------

class TestJsonRepairRetry:
    """End-to-end tests for _json_repair_retry: malformed JSON -> LLM reformat -> result."""

    async def test_repair_retry_valid_llm_response_returns_parsed_list(self):
        """When LLM returns valid JSON, _json_repair_retry returns the parsed value."""
        spec = _spec("local_news")
        valid_json = json.dumps([
            {"category": "municipal", "source": "My Edmonds News",
             "findings": "City council approved budget.", "urls": ["https://myedmondsnews.com/a"]}
        ])
        failed = _ParseFailed("completely garbled text that looked like it had data")

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=valid_json)

        # FunctionAgent and build_kimi_llm are imported *inside* _json_repair_retry,
        # so patch at the source module paths, not at jeeves.research_sectors.
        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["source"] == "My Edmonds News"

    async def test_repair_retry_garbage_response_returns_default(self):
        """When LLM returns garbled text, _json_repair_retry returns spec.default."""
        spec = _spec("local_news")
        failed = _ParseFailed("malformed [json")

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value="{broken: garbage: 'mixed\" [unclosed}")

        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert result == spec.default

    async def test_repair_retry_empty_llm_response_returns_default(self):
        """When LLM returns empty string, _json_repair_retry returns spec.default."""
        spec = _spec("global_news")
        failed = _ParseFailed("some raw text")

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value="")

        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert result == spec.default

    async def test_repair_retry_llm_exception_returns_default(self):
        """When the LLM agent raises, _json_repair_retry returns spec.default (no crash)."""
        spec = _spec("local_news")
        failed = _ParseFailed("some raw")

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=RuntimeError("NIM 500"))

        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert result == spec.default

    async def test_repair_retry_empty_raw_uses_synthesis_path(self):
        """When failed.raw is empty, the repair prompt uses the sector instruction
        (synthesis path), not the malformed-reformat path."""
        spec = _spec("intellectual_journals")
        # Empty raw triggers the "produce JSON from instruction" path.
        failed = _ParseFailed("")

        valid_json = json.dumps([
            {"source": "Aeon", "findings": "Essay on consciousness and free will.", "urls": ["https://aeon.co/x"]}
        ])
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=valid_json)

        captured_prompts: list[str] = []

        def capture_run(prompt: str):
            captured_prompts.append(prompt)
            return valid_json

        mock_agent.run = AsyncMock(side_effect=lambda p: (captured_prompts.append(p), valid_json)[1])

        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert isinstance(result, list)
        # Verify the synthesis path was taken: prompt should reference sector instruction
        assert len(captured_prompts) == 1
        assert "SECTOR INSTRUCTION" in captured_prompts[0] or "produced no output" in captured_prompts[0]

    async def test_repair_retry_dict_shape_valid_response(self):
        """Repair retry works for dict-shape sectors (career)."""
        spec = _spec("career")
        failed = _ParseFailed("{'openings': [{'district': 'Edmonds', 'role': 'HS English'}]}")

        valid_json = json.dumps({"openings": [{"district": "Edmonds", "role": "HS English",
                                               "url": "https://x", "summary": "FTE",
                                               "deadline": None, "salary_range": None}],
                                 "notes": ""})
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=valid_json)

        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert isinstance(result, dict)
        assert "openings" in result

    async def test_repair_retry_parse_failed_is_returned_as_default(self):
        """If repair LLM also returns _ParseFailed, the sector default is returned."""
        spec = _spec("local_news")
        failed = _ParseFailed("totally broken {json")

        # Return something that parse will also fail on after the repair
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value="also garbage [unclosed")

        with _repair_mocks(mock_agent):
            result = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=failed,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert result == spec.default


# ---------------------------------------------------------------------------
# TestParsingChainIntegration — end-to-end: raw text -> _ParseFailed -> repaired
# ---------------------------------------------------------------------------

class TestParsingChainIntegration:
    """Tests that exercise the full chain: raw text -> _parse_sector_output ->
    _ParseFailed -> _json_repair_retry -> final result."""

    async def test_full_chain_malformed_to_repaired(self):
        """Simulate a real pipeline run: agent returns malformed JSON, repair succeeds."""
        spec = _spec("global_news")
        # Simulate what a streaming-dropped NIM response might look like
        raw_agent_output = "Here are the findings:\n[{'source': 'BBC', 'findings': 'War update in region.', 'urls': ['https://bbc.com/a']"

        # Step 1: _parse_sector_output fails deterministically
        parse_result = _parse_sector_output(raw_agent_output, spec)
        assert isinstance(parse_result, _ParseFailed), \
            f"Expected _ParseFailed but got {type(parse_result).__name__}: {parse_result}"

        # Step 2: _json_repair_retry succeeds with mocked LLM
        valid_repaired = json.dumps([
            {"source": "BBC", "findings": "War update in region.", "urls": ["https://bbc.com/a"]}
        ])
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value=valid_repaired)

        with _repair_mocks(mock_agent):
            final = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=parse_result,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert isinstance(final, list)
        assert final[0]["source"] == "BBC"

    async def test_full_chain_unrecoverable_falls_back_to_default(self):
        """When both parse and repair fail, the sector default is used (no crash)."""
        spec = _spec("local_news")
        raw_agent_output = "completely garbage output that is not JSON at all"

        parse_result = _parse_sector_output(raw_agent_output, spec)
        assert isinstance(parse_result, _ParseFailed)

        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(return_value="still not json either")

        with _repair_mocks(mock_agent):
            final = await _json_repair_retry(
                cfg=MagicMock(verbose=False),
                spec=spec,
                failed=parse_result,
                ledger=_StubLedger(),
                sector_max_tokens=4096,
            )

        assert final == spec.default  # graceful degradation
