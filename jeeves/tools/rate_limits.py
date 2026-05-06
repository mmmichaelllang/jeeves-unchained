"""Tier-choreographed rate limiting — sprint-19 slice E.

Every search/extract provider is assigned a concurrency tier. The
``acquire(provider)`` context manager wraps the HTTP call and serialises
parallel attempts beyond the tier ceiling. Used to prevent the per-sector
agent from launching, say, 6 simultaneous Playwright sessions and tripping
runner OOM, or 4 simultaneous DeepSearch calls and burning the daily cap in
one minute.

Tiers
-----

    +------+------------------------------------------------------------+
    | Tier | Providers                                              |  N |
    +------+------------------------------------------------------------+
    |   1  | serper, exa, jina_search                              |  8 |
    |   2  | tavily, gemini_grounded, vertex_grounded, jina_rerank |  4 |
    |   3  | jina_deepsearch, tinyfish, tinyfish_search, playwright,|  1 |
    |      | playwright_search, firecrawl                          |    |
    +------+------------------------------------------------------------+

Tier-1 providers are cheap, fast, deterministic — high concurrency is fine.
Tier-2 providers are moderate (RPM-bounded by free tier or token cost).
Tier-3 providers are expensive (LLM-backed, headless browser, multi-hop).

Overrides
---------

Set ``JEEVES_RL_<PROVIDER>=<int>`` to override the default ceiling (e.g.
``JEEVES_RL_SERPER=2`` to throttle serper to 2 in flight). Read once at
first ``acquire()`` call (cached in ``_SEMAPHORES``); change requires
process restart. This matches every other env-flag in the project.

Telemetry
---------

When acquire blocks for more than ``_SLOW_WAIT_MS`` (default 50ms) we emit
a ``semaphore_wait_ms`` event. Lets the eval harness flag tier mis-sizing
without spamming telemetry on every fast acquire.

Fail-soft contract
------------------

If telemetry import fails (during early bootstrap) or the semaphore raises
on release (shouldn't happen), the wrapping context manager still releases
the lock cleanly — every code path goes through ``finally``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from contextlib import contextmanager
from typing import Iterator

log = logging.getLogger(__name__)

# Default tiers — tune per provider in one place. New providers added here
# automatically get picked up by every wrap-site that calls ``acquire()``.
TIER_1: tuple[str, ...] = ("serper", "exa", "jina_search")
TIER_2: tuple[str, ...] = (
    "tavily",
    "gemini_grounded",
    "vertex_grounded",
    "jina_rerank",
)
TIER_3: tuple[str, ...] = (
    "jina_deepsearch",
    "tinyfish",
    "tinyfish_search",
    "playwright",
    "playwright_search",
    "firecrawl",
)

DEFAULT_LIMITS: dict[str, int] = {
    **{p: 8 for p in TIER_1},
    **{p: 4 for p in TIER_2},
    **{p: 1 for p in TIER_3},
}

# Slow-acquire threshold — anything below this is silent.
_SLOW_WAIT_MS = 50

_LOCK = threading.Lock()
_SEMAPHORES: dict[str, threading.BoundedSemaphore] = {}


def _resolve_limit(provider: str) -> int:
    """Compute the semaphore size for *provider*.

    Order: env override (``JEEVES_RL_<PROVIDER>``) → DEFAULT_LIMITS → 1.
    """
    env_key = f"JEEVES_RL_{provider.upper()}"
    raw = os.environ.get(env_key, "").strip()
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            log.warning("rate_limits: bad %s=%r — ignoring", env_key, raw)
    return DEFAULT_LIMITS.get(provider, 1)


def _semaphore_for(provider: str) -> threading.BoundedSemaphore:
    sem = _SEMAPHORES.get(provider)
    if sem is not None:
        return sem
    with _LOCK:
        sem = _SEMAPHORES.get(provider)  # double-check under lock
        if sem is None:
            limit = _resolve_limit(provider)
            sem = threading.BoundedSemaphore(limit)
            _SEMAPHORES[provider] = sem
    return sem


def _emit_wait(provider: str, wait_ms: int) -> None:
    """Emit a slow-acquire telemetry event. Defensive imports — telemetry
    failures must never propagate."""
    try:
        from . import telemetry

        telemetry.emit(
            "semaphore_wait_ms",
            provider=provider,
            wait_ms=wait_ms,
        )
    except Exception:
        pass


@contextmanager
def acquire(provider: str) -> Iterator[None]:
    """Block until a slot is available for *provider*; release on exit.

    Usage::

        with acquire("serper"):
            r = httpx_client.post(...)

    Every wrap-site is the same shape; the body is whatever HTTP call the
    tool was already making. The context manager adds a per-provider
    concurrency ceiling and (when slow) a single telemetry line.
    """
    sem = _semaphore_for(provider)
    t0 = time.monotonic()
    sem.acquire()
    wait_ms = int((time.monotonic() - t0) * 1000)
    if wait_ms >= _SLOW_WAIT_MS:
        _emit_wait(provider, wait_ms)
    try:
        yield
    finally:
        try:
            sem.release()
        except ValueError:
            # BoundedSemaphore raises if released past the initial value —
            # only possible if the caller releases manually. Swallow so the
            # wrapping context never propagates a bug from the body.
            pass


def reset_for_tests() -> None:
    """Drop the cached semaphores so the next ``acquire`` re-reads env.

    Used by pytest fixtures that monkeypatch ``JEEVES_RL_*`` variables.
    """
    with _LOCK:
        _SEMAPHORES.clear()


def current_limit(provider: str) -> int:
    """Return the resolved concurrency limit for *provider* (test helper)."""
    return _semaphore_for(provider)._initial_value  # type: ignore[attr-defined]
