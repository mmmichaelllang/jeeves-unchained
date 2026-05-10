"""GATE C — asides-floor hard-block with OR retry.

2026-05-09 run-1 shipped with 0 profane asides because the OR narrative
editor was skipped or returned text without asides. The briefing was
emailed regardless. GATE C blocks the email when asides_count < floor
after a single retry. Tests:

  1. retry_recovers_above_floor — sterile result + OR retry that adds
     enough asides → gate passes, send proceeds.
  2. retry_still_below_floor_blocks — sterile result + OR retry that
     ALSO returns sterile → gate blocks, caller exits non-zero.
  3. retry_returns_unchanged_html_blocks — OR returns same HTML it was
     given (stub / failure mode) → gate blocks.
  4. above_floor_no_retry — result already has asides → no retry, no
     block, no rewrite of out_path.
  5. retry_raises_blocks_with_log — OR raises an exception → caught,
     gate blocks (asides still 0).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jeeves.write import ASIDES_FLOOR, BriefingResult


def _result_with_asides(count: int, html: str = "<html>x</html>") -> BriefingResult:
    return BriefingResult(
        html=html,
        coverage_log=[],
        word_count=5000,
        profane_aside_count=count,
        banned_word_hits=[],
        banned_transition_hits=[],
        quality_warnings=[],
    )


def _post_with_asides(count: int):
    """Return a fake postprocess_html that yields a result with the given count."""
    def _fake(html, session, *, quality_warnings=None):
        return _result_with_asides(count, html)
    return _fake


def test_above_floor_no_retry(tmp_path: Path):
    """Result already has asides ≥ floor → gate is a no-op (no OR, no Cerebras)."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    out_path.write_text("untouched", encoding="utf-8")
    cfg = MagicMock()
    cfg.cerebras_api_key = "k"  # set to prove Cerebras is NOT called
    session = MagicMock()
    starting = _result_with_asides(ASIDES_FLOOR, "<html>orig</html>")

    with patch("scripts.write._invoke_openrouter_narrative_edit") as mock_or, \
         patch("scripts.write._invoke_cerebras_narrative_edit") as mock_ce:
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is False
    assert result is starting
    mock_or.assert_not_called()
    mock_ce.assert_not_called()
    assert out_path.read_text(encoding="utf-8") == "untouched"


def test_retry_recovers_above_floor(tmp_path: Path):
    """Sterile starting result; OR tier-1 adds asides → gate passes,
    Cerebras tier-2 NOT called."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = "k"
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>fixed</html>",
    ), patch(
        "scripts.write._invoke_cerebras_narrative_edit",
    ) as mock_ce, patch(
        "scripts.write._recently_used_asides", return_value=[]
    ), patch(
        "jeeves.write.postprocess_html", _post_with_asides(ASIDES_FLOOR + 1)
    ):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is False
    assert result.profane_aside_count >= ASIDES_FLOOR
    assert result.html == "<html>fixed</html>"
    assert out_path.read_text(encoding="utf-8") == "<html>fixed</html>"
    # OR tier-1 succeeded — Cerebras NOT consulted.
    mock_ce.assert_not_called()


def test_cerebras_tier2_rescues_when_or_fails(tmp_path: Path):
    """OR tier-1 returns unchanged → Cerebras tier-2 fires and rescues."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = "k"
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    # OR returns identical HTML → tier-1 didn't rescue.
    # Cerebras returns improved HTML → tier-2 rescues.
    # postprocess sees Cerebras output and returns count above floor.
    pp_calls = {"count": 0}

    def fake_pp(html, session, *, quality_warnings=None):
        # First call (after OR) returns same 0; second (after Cerebras)
        # returns above floor. But OR returned same HTML so no pp call
        # occurs there. Cerebras call IS routed through pp → above floor.
        pp_calls["count"] += 1
        if html == "<html>cerebras_fix</html>":
            return _result_with_asides(ASIDES_FLOOR + 1, html)
        return _result_with_asides(0, html)

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>sterile</html>",  # unchanged → OR fails
    ), patch(
        "scripts.write._invoke_cerebras_narrative_edit",
        return_value="<html>cerebras_fix</html>",
    ), patch(
        "scripts.write._recently_used_asides", return_value=[]
    ), patch("jeeves.write.postprocess_html", fake_pp):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is False
    assert result.profane_aside_count == ASIDES_FLOOR + 1
    assert result.html == "<html>cerebras_fix</html>"
    assert out_path.read_text(encoding="utf-8") == "<html>cerebras_fix</html>"


def test_cerebras_skipped_when_no_api_key(tmp_path: Path):
    """No CEREBRAS_API_KEY → Cerebras tier-2 NOT consulted; gate blocks
    on OR-only result. Preserves prior behavior for installs without
    the new key configured."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = ""  # not set
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>sterile</html>",  # unchanged
    ), patch(
        "scripts.write._invoke_cerebras_narrative_edit",
    ) as mock_ce, patch(
        "scripts.write._recently_used_asides", return_value=[]
    ):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is True
    mock_ce.assert_not_called()


def test_cerebras_tier2_also_fails_blocks(tmp_path: Path):
    """Both tiers fail → gate blocks (asides still 0)."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = "k"
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>sterile</html>",
    ), patch(
        "scripts.write._invoke_cerebras_narrative_edit",
        return_value="<html>still_sterile</html>",
    ), patch(
        "scripts.write._recently_used_asides", return_value=[]
    ), patch("jeeves.write.postprocess_html", _post_with_asides(0)):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is True
    assert result.profane_aside_count == 0


def test_cerebras_raises_blocks(tmp_path: Path):
    """Cerebras tier-2 raises → caught, gate blocks."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = "k"
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>sterile</html>",
    ), patch(
        "scripts.write._invoke_cerebras_narrative_edit",
        side_effect=RuntimeError("Cerebras down"),
    ), patch(
        "scripts.write._recently_used_asides", return_value=[]
    ):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is True
    assert result.profane_aside_count == 0


def test_retry_still_below_floor_blocks(tmp_path: Path):
    """OR returned different HTML but still 0 asides AND no Cerebras key → BLOCK."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = ""  # disable tier-2 for this scenario
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>still_sterile</html>",
    ), patch("scripts.write._recently_used_asides", return_value=[]), patch(
        "jeeves.write.postprocess_html", _post_with_asides(0)
    ):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is True
    assert result.profane_aside_count == 0


def test_retry_returns_unchanged_html_blocks(tmp_path: Path):
    """OR retry returned same HTML (stub/no-op), no Cerebras key → block."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    out_path.write_text("untouched", encoding="utf-8")
    cfg = MagicMock()
    cfg.cerebras_api_key = ""
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>sterile</html>",  # SAME as starting.html
    ), patch("scripts.write._recently_used_asides", return_value=[]):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is True
    assert result.profane_aside_count == 0
    assert out_path.read_text(encoding="utf-8") == "untouched"


def test_retry_raises_blocks(tmp_path: Path):
    """OR retry raises, no Cerebras key → caught, gate blocks."""
    from scripts.write import _apply_asides_gate

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = ""
    session = MagicMock()
    starting = _result_with_asides(0, "<html>sterile</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        side_effect=RuntimeError("OR API down"),
    ), patch("scripts.write._recently_used_asides", return_value=[]):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is True
    assert result.profane_aside_count == 0


def test_one_below_floor_retried(tmp_path: Path):
    """Edge: count=1 (below floor=2) triggers OR retry; OR rescues."""
    from scripts.write import _apply_asides_gate

    assert ASIDES_FLOOR == 2

    out_path = tmp_path / "briefing.html"
    cfg = MagicMock()
    cfg.cerebras_api_key = "k"
    session = MagicMock()
    starting = _result_with_asides(1, "<html>one_aside</html>")

    with patch(
        "scripts.write._invoke_openrouter_narrative_edit",
        return_value="<html>fixed</html>",
    ) as mock_or, patch(
        "scripts.write._invoke_cerebras_narrative_edit",
    ) as mock_ce, patch(
        "scripts.write._recently_used_asides", return_value=[]
    ), patch("jeeves.write.postprocess_html", _post_with_asides(3)):
        result, blocked = _apply_asides_gate(cfg, session, starting, out_path)

    assert blocked is False
    mock_or.assert_called_once()
    mock_ce.assert_not_called()
    assert result.profane_aside_count == 3
