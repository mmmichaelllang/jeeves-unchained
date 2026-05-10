"""Run-dedup gate (2026-05-09).

Same-date scheduled cron + manual retry both fired today, producing two
emails. Run-dedup gate prevents the second from running (and emailing)
when the first already shipped a quality-clean briefing.

"Clean" = signoff has 'Your reluctantly faithful Butler' AND profane
aside count >= ASIDES_FLOOR. Both conditions must hold; either failing
means the prior run was broken and a re-run is allowed (and desired).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jeeves.write import ASIDES_FLOOR
from scripts.write import _check_prior_briefing_clean


def _make_briefing(asides: int, signoff_correct: bool = True) -> str:
    """Build a minimal HTML matching the gate's checks."""
    asides_block = " ".join("clusterfuck" for _ in range(asides))
    signoff_text = (
        "Your reluctantly faithful Butler," if signoff_correct
        else "Your faithful Butler,"
    )
    return (
        "<!DOCTYPE html><html><body>"
        f"<p>Body. {asides_block}</p>"
        f'<div class="signoff"><p>{signoff_text}<br/>Jeeves</p></div>'
        "</body></html>"
    )


def test_no_prior_briefing(tmp_path: Path):
    """No file on disk → not clean → caller proceeds."""
    p = tmp_path / "briefing.html"
    is_clean, reason = _check_prior_briefing_clean(p)
    assert is_clean is False
    assert "no prior" in reason


def test_prior_briefing_clean(tmp_path: Path):
    """Correct signoff + asides above floor → clean."""
    p = tmp_path / "briefing.html"
    p.write_text(_make_briefing(asides=ASIDES_FLOOR, signoff_correct=True),
                 encoding="utf-8")
    is_clean, reason = _check_prior_briefing_clean(p)
    assert is_clean is True
    assert "signoff ok" in reason


def test_prior_briefing_wrong_signoff(tmp_path: Path):
    """Asides ok but wrong signoff → not clean → re-run allowed."""
    p = tmp_path / "briefing.html"
    p.write_text(_make_briefing(asides=ASIDES_FLOOR, signoff_correct=False),
                 encoding="utf-8")
    is_clean, reason = _check_prior_briefing_clean(p)
    assert is_clean is False
    assert "signoff" in reason


def test_prior_briefing_too_few_asides(tmp_path: Path):
    """Correct signoff but asides below floor → not clean."""
    p = tmp_path / "briefing.html"
    p.write_text(_make_briefing(asides=ASIDES_FLOOR - 1, signoff_correct=True),
                 encoding="utf-8")
    is_clean, reason = _check_prior_briefing_clean(p)
    assert is_clean is False
    assert "asides" in reason


def test_prior_briefing_zero_asides_zero_signoff(tmp_path: Path):
    """Both gates fail → not clean (signoff check fires first)."""
    p = tmp_path / "briefing.html"
    p.write_text(_make_briefing(asides=0, signoff_correct=False),
                 encoding="utf-8")
    is_clean, reason = _check_prior_briefing_clean(p)
    assert is_clean is False
    # Whichever reason fires first is fine — both are valid block reasons.
    assert ("signoff" in reason) or ("asides" in reason)


def test_unreadable_path_returns_unclean(tmp_path: Path):
    """Path that fails to read returns not-clean rather than raising."""
    # Make a directory at the briefing-path location so read_text raises.
    p = tmp_path / "briefing.html"
    p.mkdir()
    is_clean, reason = _check_prior_briefing_clean(p)
    assert is_clean is False
    # Either "unreadable" if read raises, or "no prior" if exists() check
    # somehow fails — both are acceptable not-clean reasons.
    assert isinstance(reason, str) and reason
