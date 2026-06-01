"""Item B (m8d): code-level enforcement of Part 1 weather sentinel.

History: PART1_INSTRUCTIONS has long contained an "Empty weather rule"
directive instructing the LLM to emit a sentinel paragraph when
session.weather is empty. Models repeatedly ignored it — chronicled
in briefing-2026-05-30.html (no weather section at all) and
briefing-2026-06-01.html (same). PR #m8d adds code-level enforcement
via _ensure_weather_sentinel called from the per-part processing
block in generate_briefing.
"""
from __future__ import annotations

import pytest

try:
    from jeeves.write import _ensure_weather_sentinel
except Exception as exc:
    pytest.skip(f"jeeves.write import failed: {exc}", allow_module_level=True)


class TestEnsureWeatherSentinel:
    SENTINEL = "<p>The weather forecast is unavailable this morning, Sir.</p>"

    def test_empty_weather_missing_sentinel_injects(self):
        html = (
            '<!DOCTYPE html><html><body>'
            '<p><span class="dc">G</span>ood morning, Mister Lang.</p>'
            '<h2>The Domestic Sphere</h2>'
            '<p>Stuff happened.</p>'
        )
        out = _ensure_weather_sentinel(html, "", [])
        assert self.SENTINEL in out
        # Placement: must precede the <h2>, not appear after it.
        idx_sentinel = out.find(self.SENTINEL)
        idx_h2 = out.find("<h2")
        assert 0 <= idx_sentinel < idx_h2

    def test_populated_weather_no_op(self):
        weather_text = (
            "Mostly Sunny, with a high temperature near 67°F and a low of 48°F. "
            "Westerly winds around 8 mph. Comfortable evening expected."
        )
        html = (
            '<p>Good morning, Mister Lang.</p>'
            '<p>The forecast for Edmonds is sunny.</p>'
        )
        out = _ensure_weather_sentinel(html, weather_text, [])
        # No-op: sentinel must NOT appear when real weather is present.
        assert self.SENTINEL not in out
        assert out == html

    def test_empty_weather_but_p_mentions_forecast_no_op(self):
        """LLM honoured the empty-weather directive on its own — don't double-inject."""
        html = (
            '<p>Good morning, Mister Lang.</p>'
            '<p>The weather forecast is unavailable this morning, Sir.</p>'
        )
        out = _ensure_weather_sentinel(html, "", [])
        # Sentinel already there; one occurrence only.
        assert out.count(self.SENTINEL) == 1
        assert out == html

    def test_empty_weather_but_p_mentions_temperature_no_op(self):
        """LLM emitted weather paragraph with different phrasing — trust it."""
        html = (
            '<p>Good morning, Mister Lang.</p>'
            '<p>Today the temperature should hover in the mid-50s with light wind.</p>'
            '<h2>The Domestic Sphere</h2>'
        )
        out = _ensure_weather_sentinel(html, "", [])
        # Heuristic: <p> mentions temperature/wind so no inject.
        assert self.SENTINEL not in out

    def test_short_weather_below_threshold_treated_as_empty(self):
        """weather_text < 30 chars is treated as empty (likely error placeholder)."""
        html = '<p>Hello.</p><h2>Section</h2>'
        out = _ensure_weather_sentinel(html, "Unknown", [])
        assert self.SENTINEL in out

    def test_idempotent_multiple_calls(self):
        html = '<p>Hello.</p><h2>Section</h2>'
        once = _ensure_weather_sentinel(html, "", [])
        twice = _ensure_weather_sentinel(once, "", [])
        assert once.count(self.SENTINEL) == 1
        assert twice.count(self.SENTINEL) == 1
        assert once == twice

    def test_no_h2_appends_at_end(self):
        html = '<p>Just an intro.</p>'
        out = _ensure_weather_sentinel(html, "", [])
        assert out.rstrip().endswith(self.SENTINEL)

    def test_quality_warning_emitted_on_inject(self):
        html = '<p>x</p><h2>y</h2>'
        warnings: list[str] = []
        _ensure_weather_sentinel(html, "", warnings)
        assert "part1_weather_sentinel_injected" in warnings

    def test_quality_warning_not_emitted_on_no_op(self):
        html = '<p>real weather here, sunny 70°F</p>'
        warnings: list[str] = []
        _ensure_weather_sentinel(html, "Real weather text > 30 chars goes here, lots.", warnings)
        assert warnings == []

    def test_non_string_input_safe(self):
        assert _ensure_weather_sentinel(None, "", []) is None  # type: ignore[arg-type]
        assert _ensure_weather_sentinel("", "", []) == ""
