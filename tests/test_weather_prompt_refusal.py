"""Pin the 2026-05-30 weather prompt refusal-trigger removal (m8c).

History:
- 2026-05-27 / 28 / 29 / 30 (pre-merge): weather sector LLM consistently
  refused with 'I am not able to execute...' + fabricated JSON or quoted
  canned narrative. PR #201 (schema._cap refusal detector) stripped the
  refusal at write time -> briefing shipped with EMPTY weather.
- 2026-05-30 (m8c): remove the literal canned narrative from the prompt,
  add explicit anti-refusal block + graceful unavailable fallback line.

These tests pin the prompt-level guards so the regression cannot reopen.
"""
from __future__ import annotations

import re

import pytest


# Lazy import — research_sectors.py touches llama_index which may be absent
# in some test environments. xfail rather than break the suite.
def _load_weather_spec():
    try:
        from jeeves.research_sectors import SECTOR_SPECS
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"research_sectors import failed: {exc}")
    for spec in SECTOR_SPECS:
        if spec.name == "weather":
            return spec
    pytest.fail("weather SectorSpec not found in SECTOR_SPECS")


class TestWeatherPromptAntiRefusal:
    """The refusal-trigger removal lands here. The instruction must NOT
    contain the literal canned narrative — that text is what the LLM
    was copying after its refusal preamble (chronicled 2026-05-27 .. 30).
    """

    def test_no_canned_partly_cloudy_narrative(self):
        spec = _load_weather_spec()
        # The exact line that triggered the copy-and-refuse pattern.
        forbidden = "partly cloudy, mid-50s to low 60s, chance of afternoon drizzle"
        assert forbidden not in spec.instruction, (
            "Canned narrative still present in weather prompt — LLM will "
            "continue to refuse-and-quote. See m8c PR rationale."
        )

    def test_no_canned_westerly_winds(self):
        spec = _load_weather_spec()
        forbidden = "westerly winds 5-10 mph"
        assert forbidden not in spec.instruction, (
            "Canned wind narrative still present in weather prompt."
        )

    def test_no_estimate_escape_hatch(self):
        """The word 'estimate' was the bridge from refusal to canned text
        ('return a narrative estimate: ...'). Forbid it in the prompt so
        the LLM can't use the same escape hatch with a new template."""
        spec = _load_weather_spec()
        # 'narrative estimate' was the exact phrase. The new prompt forbids
        # the word 'estimate' in the LLM output, but the prompt itself
        # may still mention it as a forbidden token.
        # What's banned: instructional text that licenses an estimate fallback.
        assert "return a narrative estimate" not in spec.instruction
        assert "Note it clearly as an estimate" not in spec.instruction

    def test_has_anti_refusal_block(self):
        spec = _load_weather_spec()
        assert "CRITICAL ANTI-REFUSAL" in spec.instruction, (
            "Missing the anti-refusal header — the prompt should make the "
            "refusal-strip behavior explicit so the LLM stops generating "
            "refusal preambles."
        )

    def test_forbids_refusal_phrases_explicitly(self):
        """Prompt should enumerate the schema._cap refusal prefixes so the
        LLM cannot plausibly claim it didn't know they would be stripped."""
        spec = _load_weather_spec()
        for phrase in ("I am not able", "I cannot", "as an AI"):
            assert phrase in spec.instruction, (
                f"Anti-refusal block must enumerate {phrase!r} as a "
                f"forbidden response prefix."
            )

    def test_has_graceful_unavailable_fallback(self):
        """When the model truly has no data, it must have ONE exact bare
        line to fall back to — not a creative narrative. This is a
        positive signal to the auditor that the model tried."""
        spec = _load_weather_spec()
        assert "Weather data unavailable for today." in spec.instruction, (
            "Missing graceful unavailable fallback line."
        )

    def test_forbids_fabrication(self):
        spec = _load_weather_spec()
        assert "Do NOT fabricate" in spec.instruction, (
            "Prompt should explicitly forbid fabricated specifics — the "
            "2026-05-30 weather output (pre-schema._cap) was a fabricated "
            "JSON wrapper claiming '51°F' with no tool source."
        )

    def test_primary_six_step_chain_preserved(self):
        """Surgical edit promise: the 1..6 tool-call chain stays intact.
        Only the post-chain fallback block changed."""
        spec = _load_weather_spec()
        # Spot-check four anchor steps that must survive.
        assert "1. serper_search(query='Edmonds WA weather today forecast'" in spec.instruction
        assert "2. tavily_search(query='weather forecast Edmonds Washington 98020 today')" in spec.instruction
        assert "3. gemini_grounded_synthesize" in spec.instruction
        assert "6. serper_search(query='Seattle area weather forecast today" in spec.instruction
