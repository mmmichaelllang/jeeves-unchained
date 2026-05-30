"""Tests for `jeeves.schema.is_refusal_text` and `_cap` refusal stripping.

Triggered by 2026-05-30 incident: weather sector captured raw LLM refusal
("I am not able to execute the instructions...") followed by fabricated JSON.
GATE-A in scripts/write.py catches all-empty research but missed this
sector-level refusal case. These tests guard the sector-level mirror in
schema._cap.
"""

from __future__ import annotations

import pytest

from jeeves.schema import (
    FIELD_CAPS,
    _cap,
    apply_field_caps,
    is_refusal_text,
)


class TestIsRefusalText:
    @pytest.mark.parametrize(
        "text",
        [
            "I am not able to execute the instructions as they require external searches.",
            "I'm not able to fulfill that request.",
            "I cannot provide that information.",
            "I can't access live data.",
            "I don't have access to current information.",
            "I do not have the ability to browse.",
            "I'm sorry, but I can't help with that.",
            "I am sorry, but I am unable to comply.",
            "I'm unable to retrieve real-time data.",
            "I am unable to satisfy this request.",
            "Unfortunately, I cannot complete this task.",
            "Unfortunately i lack the tools required.",
            "As an AI, I don't have access to live data.",
            "As a language model, I cannot browse the web.",
        ],
    )
    def test_known_refusal_openings_detected(self, text: str) -> None:
        assert is_refusal_text(text) is True

    @pytest.mark.parametrize(
        "text",
        [
            "Edmonds: partly cloudy, 58°F, light westerly winds.",
            "Today's headlines include a major rate decision and three storms.",
            "I voted on the bill yesterday.",  # leading "I" but not refusal
            "I love the new exhibit at the library.",
            "",
            None,
            123,
            "Weather: 51°F partly cloudy with 2 mph winds.",
        ],
    )
    def test_non_refusals_pass_through(self, text) -> None:
        assert is_refusal_text(text) is False

    def test_refusal_with_leading_whitespace_still_detected(self) -> None:
        """LLMs sometimes prepend a blank line — strip before matching."""
        assert is_refusal_text("\n\n  I am not able to do that.") is True

    def test_today_weather_payload_detected(self) -> None:
        """The exact 2026-05-30 weather sector string (refusal + fabricated JSON)."""
        payload = (
            "I am not able to execute the instructions as they require external "
            "searches and access to current data. However, I can provide a general "
            'outline of how the response could be structured.\n\n```json\n{\n  '
            '"conditions": "Partly cloudy",\n  "temperature": "51°F"\n}\n```'
        )
        assert is_refusal_text(payload) is True


class TestCapStripsRefusal:
    def test_cap_strips_refusal_to_empty(self) -> None:
        text = "I am not able to execute the instructions."
        assert _cap(text, 800) == ""

    def test_cap_passes_real_content_through(self) -> None:
        text = "Edmonds: partly cloudy, 58°F."
        assert _cap(text, 800) == text

    def test_cap_truncates_long_real_content(self) -> None:
        text = "A" * 1000
        out = _cap(text, 800)
        assert out.endswith(" [TRUNCATED]")
        assert len(out) == 800 + len(" [TRUNCATED]")

    def test_cap_handles_non_string_input(self) -> None:
        assert _cap(None, 800) is None  # type: ignore[arg-type]
        assert _cap(42, 800) == 42  # type: ignore[arg-type]


class TestApplyFieldCapsStripsRefusalInWeather:
    """Integration: today's weather payload survives apply_field_caps as ''."""

    def test_weather_refusal_stripped_in_apply_field_caps(self) -> None:
        session = {
            "weather": (
                "I am not able to execute the instructions as they require "
                "external searches and access to current data."
            ),
        }
        apply_field_caps(session)
        assert session["weather"] == ""

    def test_weather_real_forecast_preserved(self) -> None:
        session = {"weather": "Edmonds: partly cloudy, 58°F."}
        apply_field_caps(session)
        assert session["weather"] == "Edmonds: partly cloudy, 58°F."

    def test_sector_findings_refusal_stripped(self) -> None:
        """Per-sector findings strings also get cleaned (triadic_ontology case)."""
        session = {
            "triadic_ontology": {
                "findings": "I cannot provide that — no live tools available.",
                "urls": ["https://example.com/a"],
            },
        }
        apply_field_caps(session)
        assert session["triadic_ontology"]["findings"] == ""
        # URLs untouched.
        assert session["triadic_ontology"]["urls"] == ["https://example.com/a"]
