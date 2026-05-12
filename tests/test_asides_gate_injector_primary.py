"""Tests for 2026-05-12 GATE C rearchitecture — injector primary.

Verifies the new contract in ``scripts.write._apply_asides_gate``:

  - Tier 3 deterministic injector runs UNCONDITIONALLY to top up to
    ``ASIDES_TARGET`` (=5). Was previously only fired when count < FLOOR.
  - ``gate_blocked`` is always False — the injector is the guarantor;
    the send is never blocked on asides anymore.
  - When LLM tiers already produce >= TARGET asides, injector no-ops.
  - When LLM tiers produce N < TARGET, injector tops up (TARGET - N).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.write as write_script
from jeeves.write import ASIDES_FLOOR, ASIDES_TARGET, BriefingResult


def _result(html: str, asides: int) -> BriefingResult:
    return BriefingResult(
        html=html,
        coverage_log=[],
        word_count=200,
        profane_aside_count=asides,
        banned_word_hits=[],
        banned_transition_hits=[],
        aside_placement_violations=[],
        link_density=0.0,
        structure_errors=[],
        quality_warnings=[],
    )


def test_asides_target_constant_above_floor():
    """ASIDES_TARGET MUST be > ASIDES_FLOOR — they're different thresholds."""
    assert ASIDES_TARGET > ASIDES_FLOOR
    assert ASIDES_TARGET >= 5  # Wodehouse cadence target


def test_gate_returns_false_when_at_target(monkeypatch, tmp_path):
    """LLM path already produced >= TARGET asides — injector no-ops, no block."""
    result = _result("<p>x</p>", asides=ASIDES_TARGET)
    out_path = tmp_path / "briefing.html"
    out_path.write_text(result.html, encoding="utf-8")

    cfg = MagicMock()
    cfg.cerebras_api_key = ""

    # OR retry shouldn't fire (already above FLOOR).
    or_spy = MagicMock(return_value=result.html)
    monkeypatch.setattr(write_script, "_invoke_openrouter_narrative_edit", or_spy)

    # Injector shouldn't add anything when already at target.
    inj_spy = MagicMock(return_value=(result.html, []))
    monkeypatch.setattr(
        "jeeves.write._inject_asides_to_floor", inj_spy,
    )
    monkeypatch.setattr(write_script, "_recently_used_asides", lambda _cfg: [])

    out, blocked = write_script._apply_asides_gate(cfg, MagicMock(), result, out_path)
    assert blocked is False
    or_spy.assert_not_called()  # not below floor
    inj_spy.assert_called_once()
    # Injector was called with target = ASIDES_TARGET.
    assert inj_spy.call_args.kwargs.get("target_count") == ASIDES_TARGET


def test_gate_runs_injector_unconditionally_when_above_floor_below_target(
    monkeypatch, tmp_path,
):
    """Most common case: LLM produced 2 asides (>= floor, < target).
    Tier 1 + Tier 2 should NOT fire; Tier 3 should top up to TARGET."""
    result = _result("<p>x</p>", asides=2)
    out_path = tmp_path / "b.html"
    out_path.write_text(result.html, encoding="utf-8")

    cfg = MagicMock()
    cfg.cerebras_api_key = "ce-key"

    or_spy = MagicMock(return_value=result.html)
    monkeypatch.setattr(write_script, "_invoke_openrouter_narrative_edit", or_spy)
    cer_spy = MagicMock(return_value=result.html)
    monkeypatch.setattr(write_script, "_invoke_cerebras_narrative_edit", cer_spy)

    inj_calls = []
    def fake_inject(*, html, recently_used, current_count, target_count, **kw):
        inj_calls.append({"current": current_count, "target": target_count})
        return html, ["a1", "a2", "a3"]  # injected 3, reaching 5
    monkeypatch.setattr(
        "jeeves.write._inject_asides_to_floor",
        lambda html, **kw: fake_inject(html=html, **kw),
    )
    # postprocess returns a result with 5 asides after injection.
    monkeypatch.setattr(
        "jeeves.write.postprocess_html",
        lambda *a, **kw: _result(a[0], asides=5),
    )
    monkeypatch.setattr(write_script, "_recently_used_asides", lambda _cfg: [])

    out, blocked = write_script._apply_asides_gate(cfg, MagicMock(), result, out_path)
    assert blocked is False
    # Tier 1 / 2 didn't fire (above floor).
    or_spy.assert_not_called()
    cer_spy.assert_not_called()
    # Injector fired with current=2, target=ASIDES_TARGET.
    assert inj_calls == [{"current": 2, "target": ASIDES_TARGET}]


def test_gate_runs_or_then_cerebras_then_injector_when_below_floor(
    monkeypatch, tmp_path,
):
    """LLM produced 0 asides (< floor). All three tiers fire in order."""
    result = _result("<p>x</p>", asides=0)
    out_path = tmp_path / "b.html"
    out_path.write_text(result.html, encoding="utf-8")

    cfg = MagicMock()
    cfg.cerebras_api_key = "ce-key"

    or_spy = MagicMock(return_value=result.html)
    monkeypatch.setattr(write_script, "_invoke_openrouter_narrative_edit", or_spy)
    cer_spy = MagicMock(return_value=result.html)
    monkeypatch.setattr(write_script, "_invoke_cerebras_narrative_edit", cer_spy)

    inj_spy = MagicMock(return_value=(result.html, ["a", "b", "c", "d", "e"]))
    monkeypatch.setattr(
        "jeeves.write._inject_asides_to_floor",
        lambda html, **kw: inj_spy(html, **kw),
    )
    monkeypatch.setattr(
        "jeeves.write.postprocess_html",
        lambda *a, **kw: _result(a[0], asides=5),
    )
    monkeypatch.setattr(write_script, "_recently_used_asides", lambda _cfg: [])

    out, blocked = write_script._apply_asides_gate(cfg, MagicMock(), result, out_path)
    assert blocked is False
    or_spy.assert_called_once()
    cer_spy.assert_called_once()
    inj_spy.assert_called_once()


def test_gate_never_blocks_even_when_injector_fails(monkeypatch, tmp_path):
    """When no qualifying paragraphs exist (cap-short briefing), injector
    returns 0 injected. Pre-2026-05-12 this triggered a hard block (exit 5);
    post-2026-05-12 the send proceeds with a warning. Verify no block."""
    result = _result("<p>too short</p>", asides=0)
    out_path = tmp_path / "b.html"
    out_path.write_text(result.html, encoding="utf-8")

    cfg = MagicMock()
    cfg.cerebras_api_key = ""

    monkeypatch.setattr(
        write_script, "_invoke_openrouter_narrative_edit",
        lambda *a, **kw: result.html,
    )
    monkeypatch.setattr(
        "jeeves.write._inject_asides_to_floor",
        lambda html, **kw: (html, []),  # nothing injected
    )
    monkeypatch.setattr(write_script, "_recently_used_asides", lambda _cfg: [])
    monkeypatch.setattr(
        "jeeves.write.postprocess_html",
        lambda *a, **kw: _result(a[0], asides=0),
    )

    out, blocked = write_script._apply_asides_gate(cfg, MagicMock(), result, out_path)
    # The defining 2026-05-12 contract: never block on asides.
    assert blocked is False


def test_gate_handles_injector_exception(monkeypatch, tmp_path):
    """Injector raising should not block the send — log and proceed."""
    result = _result("<p>x</p>", asides=2)
    out_path = tmp_path / "b.html"
    out_path.write_text(result.html, encoding="utf-8")

    cfg = MagicMock()
    cfg.cerebras_api_key = ""

    def boom(*a, **kw):
        raise RuntimeError("injector exploded")
    monkeypatch.setattr("jeeves.write._inject_asides_to_floor", boom)
    monkeypatch.setattr(write_script, "_recently_used_asides", lambda _cfg: [])

    out, blocked = write_script._apply_asides_gate(cfg, MagicMock(), result, out_path)
    assert blocked is False
