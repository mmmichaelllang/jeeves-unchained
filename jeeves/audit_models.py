"""Reasoning-first OpenRouter free-tier model resolver — Auditor uses this.

Where ``jeeves.write._resolve_or_free_models`` ranks free-tier models by raw
param count (good enough for prose generation), the Auditor needs models
that can actually reason about correctness, narrative coherence, and dedup
across a long document. Reasoning-tuned models (DeepSeek-R1, models with
``thinking`` or ``reasoning`` in the id, Qwen3 reasoning variants) get
ranked above bigger but plainer instruct models.

Selection order:
1. ``JEEVES_AUDIT_MODEL=<id>`` env override — pin a single model for tests.
2. ``JEEVES_AUDIT_MODELS=a,b,c`` env override — pin a chain.
3. Live fetch from OR ``/api/v1/models``, scored:
   - +50 for explicit reasoning markers in id (``r1``, ``thinking``,
     ``reasoning``, ``-think``)
   - +30 for known reasoning families (``deepseek-r1``, ``qwen3``)
   - +param_count (in billions, capped at 100 to stop a 1T-param coder
     model from beating a 70B reasoning model on size alone)
   - +context_length / 1000 (longer context helps cross-section audits)
   - All vision / coder / embed / guard / rerank variants filtered out.
4. ``_AUDIT_MODELS_FALLBACK`` — conservative reasoning-first list when the
   OR API is unreachable.

Caching mirrors ``write._resolve_or_free_models``: 10 min per process,
falls back on network failure.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

log = logging.getLogger(__name__)

# Hardcoded fallback used ONLY when live fetch fails. Reasoning-first.
_AUDIT_MODELS_FALLBACK: tuple[str, ...] = (
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-chat-v3:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    "google/gemma-3-12b-it:free",
)

# Tunables.
_CACHE_TTL_S = 600
_FETCH_TIMEOUT_S = 5.0
_MIN_CONTEXT_LENGTH = 16384  # auditors need to read 25k+ char briefings

# Skip patterns — same as write resolver but slightly stricter
# (audit cares about long-form judgment, not coding/vision).
_SKIP_PATTERNS: tuple[str, ...] = (
    "vision",
    "-vl-",
    "coder",
    "-code-",
    "embed",
    "guard",
    "rerank",
    "tts",
    "audio",
    "-image",
)

# Reasoning markers in model id — strong signal.
_REASONING_MARKERS: tuple[str, ...] = (
    "-r1",
    ":r1",
    "/r1",
    "deepseek-r1",
    "thinking",
    "reasoning",
    "-think",
    "qwen3-reason",
)

# Known reasoning families — moderate signal.
_REASONING_FAMILIES: tuple[str, ...] = (
    "deepseek-r1",
    "deepseek-chat-v3",
    "qwen3",
    "qwen-3",
)

# Module cache.
_CACHE: tuple[float, tuple[str, ...]] | None = None


def _parse_param_billions(model_id: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*b\b", model_id.lower())
    return float(m.group(1)) if m else 0.0


def _score_model(model_id: str, context_length: int) -> float:
    """Higher is better. Composed of reasoning markers, family, params, context."""
    mid = model_id.lower()
    score = 0.0
    if any(m in mid for m in _REASONING_MARKERS):
        score += 50.0
    if any(f in mid for f in _REASONING_FAMILIES):
        score += 30.0
    params_b = _parse_param_billions(mid)
    score += min(params_b, 100.0)  # cap so a 480B coder model doesn't dominate
    score += context_length / 1000.0
    return score


def _fetch_audit_models() -> tuple[str, ...] | None:
    """Live fetch from OR API. Returns ranked list or None on failure."""
    try:
        import httpx as _httpx
    except ImportError:
        log.debug("audit_models: httpx not installed — falling back")
        return None

    try:
        with _httpx.Client(timeout=_FETCH_TIMEOUT_S) as client:
            resp = client.get("https://openrouter.ai/api/v1/models")
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        log.debug("audit_models: fetch failed (%s)", exc)
        return None

    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None

    candidates: list[tuple[float, str]] = []
    for m in items:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "")
        if not mid.endswith(":free"):
            continue
        mid_lower = mid.lower()
        if any(pat in mid_lower for pat in _SKIP_PATTERNS):
            continue
        try:
            ctx = int(m.get("context_length") or 0)
        except (TypeError, ValueError):
            ctx = 0
        if ctx < _MIN_CONTEXT_LENGTH:
            continue
        score = _score_model(mid, ctx)
        candidates.append((score, mid))

    if not candidates:
        return None

    # Descending score, then alphabetical for determinism.
    candidates.sort(key=lambda x: (-x[0], x[1]))
    ranked = tuple(mid for _, mid in candidates)
    log.info(
        "audit_models: %d candidates ranked, top=%s (score=%.1f)",
        len(ranked), ranked[0], candidates[0][0],
    )
    return ranked


def resolve_audit_models() -> tuple[str, ...]:
    """Return the active reasoning-first audit model chain.

    Resolution order:
    1. ``JEEVES_AUDIT_MODEL`` env override (single model).
    2. ``JEEVES_AUDIT_MODELS`` env override (comma-separated chain).
    3. Process-cached live fetch (10 min TTL).
    4. Live fetch, ranked by reasoning score.
    5. ``_AUDIT_MODELS_FALLBACK`` when OR API unreachable.
    """
    global _CACHE

    single = os.environ.get("JEEVES_AUDIT_MODEL", "").strip()
    if single:
        return (single,)

    chain = os.environ.get("JEEVES_AUDIT_MODELS", "").strip()
    if chain:
        return tuple(m.strip() for m in chain.split(",") if m.strip())

    now = time.monotonic()
    if _CACHE is not None:
        ts, cached = _CACHE
        if now - ts < _CACHE_TTL_S:
            return cached

    fetched = _fetch_audit_models()
    result = fetched if fetched else _AUDIT_MODELS_FALLBACK
    _CACHE = (now, result)
    return result


def reset_cache_for_tests() -> None:
    """Clear module cache. Test helper."""
    global _CACHE
    _CACHE = None
