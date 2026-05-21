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
