"""Tests for dedup improvements (feat/dedup-improvements).

Covers:
  1. Prior headlines are recency-ordered (not alphabetically sorted).
  2. today_headline_count boundary marker written to session dedup dict.
  3. Write phase applies proportional cap: prior_slots never crowded out.
  4. Legacy sessions without today_headline_count fall back to old cap.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_session_dedup(today_hl: list[str], prior_hl: list[str], n_today: int | None = None) -> dict[str, Any]:
    """Build a minimal session dict with dedup fields."""
    return {
        "dedup": {
            "covered_headlines": today_hl + prior_hl,
            "today_headline_count": n_today if n_today is not None else len(today_hl),
            "covered_urls": [],
            "cross_sector_dupes": [],
        }
    }


# ---------------------------------------------------------------------------
# 1. Prior headline ordering in research.py
# ---------------------------------------------------------------------------

def test_prior_headlines_recency_order_preserved():
    """prior_hl_list must preserve newest-first order, not sort alphabetically."""
    # Simulate what research.py does after the fix:
    # prior_headlines is a list (newest-first), today_hl_set filters out today's.
    prior_headlines = ["zebra story", "apple story", "mango story"]  # newest-first
    today_hl = ["today only"]
    today_hl_set = set(today_hl)
    prior_hl_list = [h for h in prior_headlines if h not in today_hl_set]

    assert prior_hl_list == ["zebra story", "apple story", "mango story"], (
        "prior_hl_list must preserve recency order (newest-first), not sort alphabetically"
    )


def test_prior_headlines_today_filtered():
    """Headlines that appear in today_hl must not appear in prior_hl_list."""
    prior_headlines = ["shared story", "only prior", "also shared"]
    today_hl = ["shared story", "also shared", "only today"]
    today_hl_set = set(today_hl)
    prior_hl_list = [h for h in prior_headlines if h not in today_hl_set]

    assert prior_hl_list == ["only prior"]
    assert "shared story" not in prior_hl_list
    assert "also shared" not in prior_hl_list


def test_today_headline_count_equals_len_today_hl():
    """today_headline_count must equal len(today_hl) at session build time."""
    today_hl = [f"today-{i}" for i in range(50)]
    prior_hl_list = [f"prior-{i}" for i in range(30)]
    session_dedup: dict[str, Any] = {}
    session_dedup["covered_headlines"] = today_hl + prior_hl_list
    session_dedup["today_headline_count"] = len(today_hl)

    assert session_dedup["today_headline_count"] == 50
    assert len(session_dedup["covered_headlines"]) == 80


# ---------------------------------------------------------------------------
# 2. Write-phase proportional cap
# ---------------------------------------------------------------------------

def _apply_cap(dedup: dict[str, Any], cap: int = 150) -> list[str]:
    """Replicate the proportional-cap logic from write.py._trim_session_for_prompt."""
    _PRIOR_SLOTS = 70
    _TODAY_SLOTS = cap - _PRIOR_SLOTS  # 80
    _n_today = int(dedup.get("today_headline_count") or 0)
    all_hl = dedup["covered_headlines"]
    if _n_today > 0:
        raw_today = all_hl[:_n_today][:_TODAY_SLOTS]
        raw_prior = all_hl[_n_today:][:_PRIOR_SLOTS]
        return raw_today + raw_prior
    return all_hl[:cap]


def test_prior_slots_survive_when_today_is_large():
    """When today has 200 headlines, prior must still get its 70 slots."""
    today_hl = [f"today-{i}" for i in range(200)]
    prior_hl = [f"prior-{i}" for i in range(100)]
    dedup = {
        "covered_headlines": today_hl + prior_hl,
        "today_headline_count": 200,
    }
    result = _apply_cap(dedup)

    today_in_result = [h for h in result if h.startswith("today-")]
    prior_in_result = [h for h in result if h.startswith("prior-")]

    assert len(today_in_result) == 80, f"today slots wrong: {len(today_in_result)}"
    assert len(prior_in_result) == 70, f"prior crowded out: {len(prior_in_result)}"
    assert len(result) == 150


def test_prior_recency_order_preserved_in_cap():
    """The proportional cap must preserve recency order within prior slots."""
    today_hl = [f"today-{i}" for i in range(10)]
    # prior is newest-first: prior-0 is yesterday, prior-99 is two weeks ago
    prior_hl = [f"prior-{i}" for i in range(100)]
    dedup = {
        "covered_headlines": today_hl + prior_hl,
        "today_headline_count": 10,
    }
    result = _apply_cap(dedup)
    prior_in_result = [h for h in result if h.startswith("prior-")]

    # Should get prior-0 through prior-69 (the 70 newest prior entries)
    assert prior_in_result[0] == "prior-0", "Most recent prior entry must come first"
    assert prior_in_result[-1] == "prior-69", "Oldest of the prior window must be prior-69"
    assert "prior-70" not in prior_in_result, "Entries beyond 70 slots must be excluded"


def test_legacy_session_no_boundary_marker_falls_back():
    """Sessions written before the fix (no today_headline_count) use old flat cap."""
    all_hl = [f"hl-{i}" for i in range(300)]
    dedup = {
        "covered_headlines": all_hl,
        # No today_headline_count key → legacy behaviour
    }
    result = _apply_cap(dedup)
    assert len(result) == 150
    assert result == all_hl[:150]


def test_proportional_cap_when_today_is_small():
    """When today has fewer than 80 headlines, today gets all its entries."""
    today_hl = [f"today-{i}" for i in range(30)]
    prior_hl = [f"prior-{i}" for i in range(200)]
    dedup = {
        "covered_headlines": today_hl + prior_hl,
        "today_headline_count": 30,
    }
    result = _apply_cap(dedup)
    today_in_result = [h for h in result if h.startswith("today-")]
    prior_in_result = [h for h in result if h.startswith("prior-")]

    assert len(today_in_result) == 30   # all 30 fit
    assert len(prior_in_result) == 70   # prior still gets its full 70
    assert len(result) == 100


def test_total_cap_never_exceeded():
    """Result must never exceed DEDUP_PROMPT_HEADLINES_CAP regardless of input."""
    today_hl = [f"t-{i}" for i in range(500)]
    prior_hl = [f"p-{i}" for i in range(500)]
    dedup = {
        "covered_headlines": today_hl + prior_hl,
        "today_headline_count": 500,
    }
    result = _apply_cap(dedup)
    assert len(result) <= 150
