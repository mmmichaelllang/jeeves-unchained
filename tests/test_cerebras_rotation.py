"""Tests for Cerebras runtime model rotation (M4).

Covers _resolve_cerebras_model and _rotate_on_429 in research_sectors.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import jeeves.research_sectors as rs


def _reset() -> None:
    """Reset module-level Cerebras state between tests."""
    rs._RESOLVED_CEREBRAS_MODEL = None
    rs._CEREBRAS_TRIED_MODELS = set()
    rs._CEREBRAS_EXHAUSTED = False


# ---------------------------------------------------------------------------
# Test 1: resolution from live /v1/models
# ---------------------------------------------------------------------------


def test_resolve_cerebras_model_from_live():
    _reset()
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "data": [{"id": "llama-3.3-70b"}, {"id": "some-other-model"}]
    }
    with patch("httpx.get", return_value=fake_resp):
        result = rs._resolve_cerebras_model("fake-key")

    assert result == "llama-3.3-70b", "should pick highest-priority chain model"
    assert rs._RESOLVED_CEREBRAS_MODEL == "llama-3.3-70b"


# ---------------------------------------------------------------------------
# Test 2: 429 rotation advances to next model
# ---------------------------------------------------------------------------


def test_rotate_on_429_advances_model():
    _reset()
    rs._RESOLVED_CEREBRAS_MODEL = "llama-3.3-70b"

    next_model = rs._rotate_on_429("llama-3.3-70b")

    assert next_model is not None, "rotation should return next model"
    assert next_model != "llama-3.3-70b", "should not return the failed model"
    assert next_model in rs._CEREBRAS_MODEL_CHAIN
    assert "llama-3.3-70b" in rs._CEREBRAS_TRIED_MODELS
    assert rs._RESOLVED_CEREBRAS_MODEL is None, "cache must be invalidated"


# ---------------------------------------------------------------------------
# Test 3: exhaustion → OR fallback (returns None when chain exhausted)
# ---------------------------------------------------------------------------


def test_rotate_on_429_exhaustion_returns_none():
    _reset()
    rs._CEREBRAS_TRIED_MODELS = set(rs._CEREBRAS_MODEL_CHAIN)

    result = rs._rotate_on_429("llama-3.3-70b")

    assert result is None, "should return None when all models exhausted"


# ---------------------------------------------------------------------------
# Test 4: model cache invalidation causes re-probe on next resolve
# ---------------------------------------------------------------------------


def test_resolve_cerebras_model_cache_invalidation():
    _reset()
    # First probe: llama-3.3-70b available → cached
    resp1 = MagicMock()
    resp1.status_code = 200
    resp1.json.return_value = {"data": [{"id": "llama-3.3-70b"}]}

    with patch("httpx.get", return_value=resp1):
        m1 = rs._resolve_cerebras_model("fake-key")
    assert m1 == "llama-3.3-70b"

    # Simulate _rotate_on_429 invalidating the cache
    rs._RESOLVED_CEREBRAS_MODEL = None
    rs._CEREBRAS_TRIED_MODELS = {"llama-3.3-70b"}

    # Second probe: llama-3.3-70b still listed but now tried → skip to next
    resp2 = MagicMock()
    resp2.status_code = 200
    resp2.json.return_value = {
        "data": [
            {"id": "llama-3.3-70b"},
            {"id": "llama-4-maverick-17b-128e-instruct"},
        ]
    }

    with patch("httpx.get", return_value=resp2):
        m2 = rs._resolve_cerebras_model("fake-key")

    assert m2 == "llama-4-maverick-17b-128e-instruct", (
        "re-probe should skip 429'd model and pick next in chain"
    )


# ---------------------------------------------------------------------------
# Test 5+: Cerebras exhaustion breaker (2026-05-29 fix)
#
# Chronicle Pattern P3: circuit-breaker + skip beats per-call retry when
# the provider is broken. Before this fix, every sector after chain
# exhaustion would re-probe /v1/models and re-walk the chain, all
# returning None — pure waste. The _CEREBRAS_EXHAUSTED breaker collapses
# that to a single None return without probing.
# ---------------------------------------------------------------------------


def test_breaker_trips_when_rotate_exhausts_chain():
    """Sector A's 429 cascade exhausts the chain → breaker MUST be set
    so sector B's _resolve_cerebras_model returns None immediately."""
    _reset()
    rs._CEREBRAS_TRIED_MODELS = set(rs._CEREBRAS_MODEL_CHAIN[:-1])

    # Last untried model 429s — should exhaust the chain.
    last_model = rs._CEREBRAS_MODEL_CHAIN[-1]
    result = rs._rotate_on_429(last_model)

    assert result is None, "rotation should return None on exhaustion"
    assert rs._CEREBRAS_EXHAUSTED is True, (
        "exhaustion via _rotate_on_429 MUST trip the breaker"
    )


def test_breaker_short_circuits_subsequent_resolve_calls():
    """Once tripped, _resolve_cerebras_model returns None immediately
    without making the httpx call. Cheapest possible skip."""
    _reset()
    rs._CEREBRAS_EXHAUSTED = True

    # Patch httpx to RAISE if called — proves the breaker short-circuited.
    with patch("httpx.get", side_effect=AssertionError("breaker should skip probe")):
        result = rs._resolve_cerebras_model("fake-key")

    assert result is None, "tripped breaker MUST return None"


def test_breaker_trips_via_no_untried_models_branch():
    """The exhaustion path through the /v1/models-listed-no-untried-models
    branch must also trip the breaker — the other production exhaustion
    route catches probe-time exhaustion (vs rotation-time exhaustion)."""
    _reset()
    # Mark every chain model as tried so the probe finds no untried.
    rs._CEREBRAS_TRIED_MODELS = set(rs._CEREBRAS_MODEL_CHAIN)

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    # Probe lists only banned/tried models — leaves no untried.
    fake_resp.json.return_value = {
        "data": [{"id": m} for m in rs._CEREBRAS_MODEL_CHAIN]
    }

    with patch("httpx.get", return_value=fake_resp):
        result = rs._resolve_cerebras_model("fake-key")

    assert result is None
    assert rs._CEREBRAS_EXHAUSTED is True, (
        "probe-time exhaustion MUST trip the breaker"
    )


def test_breaker_helper_resets_all_state():
    """_reset_cerebras_breaker is a test helper used by both this suite
    and the Patchright-recovery tests as a one-shot pristine state."""
    _reset()
    rs._CEREBRAS_EXHAUSTED = True
    rs._RESOLVED_CEREBRAS_MODEL = "llama-3.3-70b"
    rs._CEREBRAS_TRIED_MODELS = {"llama-3.3-70b"}

    rs._reset_cerebras_breaker()

    assert rs._CEREBRAS_EXHAUSTED is False
    assert rs._RESOLVED_CEREBRAS_MODEL is None
    assert rs._CEREBRAS_TRIED_MODELS == set()


def test_untripped_breaker_does_not_block_normal_resolution():
    """Negative test — when the breaker is NOT tripped, resolution
    proceeds normally. Required to prevent the new check from accidentally
    short-circuiting healthy runs."""
    _reset()
    assert rs._CEREBRAS_EXHAUSTED is False

    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": [{"id": "llama-3.3-70b"}]}

    with patch("httpx.get", return_value=fake_resp):
        result = rs._resolve_cerebras_model("fake-key")

    assert result == "llama-3.3-70b"
    assert rs._CEREBRAS_EXHAUSTED is False
