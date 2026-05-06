"""Search-shadow runner — sprint-19 slice E.

When ``serper_search`` returns, optionally fire one or more peer search
providers in parallel against the same query and write a comparison record
to ``sessions/shadow-search-<provider>-<utc-date>.jsonl``. Used to collect
side-by-side data before flipping any provider into the primary path.

Activation
----------

Each shadow gates on a single env flag:

    JEEVES_JINA_SEARCH_SHADOW=1
    JEEVES_TINYFISH_SEARCH_SHADOW=1
    JEEVES_PLAYWRIGHT_SEARCH_SHADOW=1

When all unset (vanilla run) ``maybe_run_shadows`` is a cheap early-return
and nothing happens. Production output is bit-identical to a no-shadow run.

Per-shadow contract
-------------------

Every shadow runner function takes ``(query, cfg, ledger)``, returns
``{success, results: [{title, url, snippet}], latency_ms, error?}``. They
never raise. Each result list is normalised to the same shape regardless of
provider so the JSONL line is directly diffable.

Output JSONL line shape::

    {
        "ts": "...",
        "primary": "serper",
        "shadow": "jina_search",
        "query": "Edmonds WA news",
        "primary_n": 10,
        "shadow_n": 8,
        "primary_urls": [...],
        "shadow_urls": [...],
        "jaccard": 0.4,
        "latency_primary_ms": 240,
        "latency_shadow_ms": 380,
        "shadow_error": null
    }

The eval harness reads these files; the shadow-jsonl is the on-disk truth.

Threading
---------

Each shadow runs in its own thread (``ThreadPoolExecutor`` capped at
``len(active_flags)``) with an 8s wall-clock cap. The primary call's
return value is computed and returned BEFORE shadows complete; this is
intentional — slow shadows must never delay the agent.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .telemetry import emit as _emit

log = logging.getLogger(__name__)

_SHADOW_TIMEOUT_S = 8.0
_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Output dir helpers
# ---------------------------------------------------------------------------

def _output_dir() -> Path:
    override = os.environ.get("JEEVES_TELEMETRY_DIR", "").strip()
    if override:
        return Path(override)
    return Path.cwd() / "sessions"


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_jsonl(provider: str, record: dict[str, Any]) -> None:
    """Append one record to ``sessions/shadow-search-<provider>-<date>.jsonl``."""
    out_dir = _output_dir()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"shadow-search-{provider}-{_utc_date()}.jsonl"
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception as exc:
        log.debug("shadow_jsonl write failed (%s): %s", provider, exc)


# ---------------------------------------------------------------------------
# Per-shadow runners — each returns a uniform dict
# ---------------------------------------------------------------------------

def _normalise_results(items: list[dict[str, Any]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        out.append(
            {
                "title": str(it.get("title") or "")[:300],
                "url": str(it.get("url") or it.get("link") or "")[:600],
                "snippet": str(it.get("snippet") or it.get("description") or "")[:600],
            }
        )
    return out


def _shadow_via_jina(query: str, cfg: Any, ledger: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        from .jina import make_jina_search

        out = make_jina_search(cfg, ledger)(query, num=10)
        data = json.loads(out) if isinstance(out, str) else out
        results = _normalise_results(data.get("results") or [])
        return {
            "success": not data.get("error"),
            "results": results,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": data.get("error"),
        }
    except Exception as exc:
        return {
            "success": False,
            "results": [],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": f"shadow_jina crashed: {exc}",
        }


def _shadow_via_tinyfish(query: str, cfg: Any, ledger: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        from .tinyfish import search as _tf_search

        # Force include_raw_content=False — shadow only needs SERP shape.
        data = _tf_search(query, num=5, include_raw_content=False, ledger=ledger)
        results = _normalise_results(data.get("results") or [])
        return {
            "success": bool(data.get("success")),
            "results": results,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": data.get("error"),
        }
    except Exception as exc:
        return {
            "success": False,
            "results": [],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": f"shadow_tinyfish crashed: {exc}",
        }


def _shadow_via_playwright(query: str, cfg: Any, ledger: Any) -> dict[str, Any]:
    t0 = time.monotonic()
    try:
        from .playwright_extractor import search as _pw_search

        data = _pw_search(query, engine="ddg", num=10, ledger=ledger)
        results = _normalise_results(data.get("results") or [])
        return {
            "success": bool(data.get("success")),
            "results": results,
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": data.get("error"),
        }
    except Exception as exc:
        return {
            "success": False,
            "results": [],
            "latency_ms": int((time.monotonic() - t0) * 1000),
            "error": f"shadow_playwright crashed: {exc}",
        }


# Registry of shadow flag → (provider tag, runner-name). Runner is looked
# up by *name* so monkeypatching the module attribute (in tests) replaces
# the function the dispatcher actually invokes. Capturing the function
# object at import time would freeze the test patch out.
_SHADOW_RUNNERS: tuple[tuple[str, str, str], ...] = (
    ("JEEVES_JINA_SEARCH_SHADOW", "jina_search", "_shadow_via_jina"),
    ("JEEVES_TINYFISH_SEARCH_SHADOW", "tinyfish_search", "_shadow_via_tinyfish"),
    ("JEEVES_PLAYWRIGHT_SEARCH_SHADOW", "playwright_search", "_shadow_via_playwright"),
)


def _active_shadows() -> list[tuple[str, Any]]:
    """Return [(tag, runner_callable)] for every flag currently set.

    Looks the runner callable up via ``globals()`` so test-time
    ``monkeypatch.setattr`` on this module is honoured.
    """
    g = globals()
    out: list[tuple[str, Any]] = []
    for env_key, tag, runner_name in _SHADOW_RUNNERS:
        if os.environ.get(env_key, "").strip() != "1":
            continue
        runner = g.get(runner_name)
        if callable(runner):
            out.append((tag, runner))
    return out


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def _jaccard(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb) or 1
    return round(inter / union, 4)


def maybe_run_shadows(
    *,
    primary: str,
    query: str,
    primary_results: list[dict[str, Any]],
    primary_latency_ms: int,
    cfg: Any,
    ledger: Any,
) -> None:
    """Fire each enabled shadow runner against *query* and persist a JSONL
    comparison line. Returns immediately when no shadow flag is set (typical
    case) — must remain fast on the hot path.

    Failure of any individual shadow is recorded in its own JSONL line and
    a ``shadow_error`` event in telemetry; primary return value is never
    affected.
    """
    active = _active_shadows()
    if not active:
        return

    primary_norm = _normalise_results(primary_results)
    primary_urls = [r["url"] for r in primary_norm if r["url"]]

    # Concurrent fan-out — bounded by active shadow count.
    with ThreadPoolExecutor(max_workers=len(active)) as pool:
        futures = {
            pool.submit(runner, query, cfg, ledger): tag for tag, runner in active
        }
        for fut, tag in list(futures.items()):
            try:
                result = fut.result(timeout=_SHADOW_TIMEOUT_S)
            except _Timeout:
                result = {
                    "success": False,
                    "results": [],
                    "latency_ms": int(_SHADOW_TIMEOUT_S * 1000),
                    "error": "shadow timeout",
                }
                fut.cancel()
            except Exception as exc:
                result = {
                    "success": False,
                    "results": [],
                    "latency_ms": 0,
                    "error": f"shadow exception: {exc}",
                }

            shadow_urls = [r["url"] for r in (result.get("results") or []) if r.get("url")]
            record = {
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "primary": primary,
                "shadow": tag,
                "query": query,
                "primary_n": len(primary_urls),
                "shadow_n": len(shadow_urls),
                "primary_urls": primary_urls[:25],
                "shadow_urls": shadow_urls[:25],
                "jaccard": _jaccard(primary_urls, shadow_urls),
                "latency_primary_ms": int(primary_latency_ms),
                "latency_shadow_ms": int(result.get("latency_ms") or 0),
                "shadow_success": bool(result.get("success")),
                "shadow_error": result.get("error"),
            }
            _write_jsonl(tag, record)
            _emit(
                "shadow_compare",
                primary=primary,
                shadow=tag,
                query=query,
                primary_n=record["primary_n"],
                shadow_n=record["shadow_n"],
                jaccard=record["jaccard"],
                latency_primary_ms=record["latency_primary_ms"],
                latency_shadow_ms=record["latency_shadow_ms"],
                shadow_success=record["shadow_success"],
            )
