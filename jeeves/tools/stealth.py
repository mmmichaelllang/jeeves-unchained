"""Stealth-browser article extractor — sprint-20 canary.

Adds a fingerprint-diversified, optionally authenticated browser layer
between TinyFish and the existing patchright singleton. The goal is two
problems the existing chain doesn't solve:

1. **Login-walled subscriber sources** (NYT, FT, WSJ, Bloomberg, Atlantic,
   New Yorker, Economist) — fed via a Playwright ``storage_state`` JSON
   bootstrapped locally from the user's own subscriptions. Encrypted into
   a single GitHub-Actions secret for CI runs.
2. **Anti-bot fingerprint uniformity** — patchright closes the obvious CDP
   tells but hammers every target with the *same* canvas / WebGL / audio
   profile from one Azure ASN every day. Stealth introduces (a) per-host
   browserforge fingerprint diversity and (b) an optional Camoufox
   (Firefox) backend whose JS surface is harder for Chromium-tuned bot
   detectors (DataDome, PerimeterX) to score.

Position in the fetch chain (when ``JEEVES_USE_STEALTH=1``)::

    httpx+trafilatura → Jina(r.jina.ai) → tinyfish → **stealth** → playwright

The existing playwright_extractor singleton remains the final fallback so
its 75-100 s/run amortised cost (sprint-15 rewrite) is preserved when
stealth declines or fails.

Free-tier-only stack
--------------------

No paid proxies, no paid bypass APIs. The four cheap ingredients are:

* **patchright** + **browserforge** — Apache-2.0, pip-only.
* **camoufox** — MPL-2.0, single binary, opt-in via ``JEEVES_USE_CAMOUFOX``.
* **storage_state JSON** — user's own subscriptions, encrypted into the
  ``STEALTH_STORAGE_STATE_B64`` GitHub secret; decrypted to a runner
  tmpfile at job start, path passed via ``STEALTH_STORAGE_STATE_PATH``.
* **Archive fallback** — ``archive.ph`` + Wayback CDX, both publisher-
  tolerated for personal use, free.

A free-tier scrape-API canary (ScrapingBee 1 000 calls/mo or ScrapFly
1 000 credits/mo) plugs in via ``STEALTH_FALLBACK_API`` for hard-paywall
last resort. Both vendors offer no-card-required free tiers; cap-aware.

Public surface
--------------
``extract_article(url, *, timeout_seconds, max_chars, ledger) -> dict``
    Fail-soft: never raises. Same shape as
    ``tinyfish.extract_article`` and ``playwright_extractor.extract_article``
    so call-sites can swap.

Return shape::

    {
        "url":            str,
        "title":          str,
        "text":           str,    # markdown / plain text, truncated
        "success":        bool,
        "extracted_via":  "stealth",
        "quality_score":  float,
        "backend":        str,    # "camoufox" | "patchright" | "archive" | "fallback_api"
        "auth_used":      bool,
        "error":          str,    # only present when success=False
    }

Feature flags
-------------
``JEEVES_USE_STEALTH=1``
    Registers ``stealth_extract`` as an agent tool and wires the layer
    into ``enrichment.fetch_article_text`` (PR2). Default-off.

``JEEVES_STEALTH_SHADOW=1``
    Shadow path: ``playwright_extractor.extract_article`` fires stealth
    in parallel and appends a comparison record to
    ``sessions/shadow-stealth-<date>.jsonl``. Production output unchanged.

``JEEVES_USE_CAMOUFOX=1``
    Prefer Camoufox (Firefox) backend over patchright when both available.

``JEEVES_USE_BROWSERFORGE=1``
    Inject browserforge fingerprint per-host. Falls back silently when
    browserforge is not installed.

``JEEVES_STEALTH_ARCHIVE_FALLBACK=1``
    On primary backend failure, try ``archive.ph/newest/<url>`` then
    Wayback CDX. Both calls are free; gated by daily cap to bound spend
    of someone-else's bandwidth.

Env config
----------
``STEALTH_STORAGE_STATE_PATH``  Absolute path to a directory of per-host
    state JSON files (``nyt_state.json``, ``ft_state.json`` …) produced
    by ``scripts/auth_refresh.py``. Absent → no auth, anon contexts only.

``STEALTH_FINGERPRINT_PROFILE``  Optional path to a single browserforge
    profile JSON. Absent → per-call random profile (still consistent
    UA / CH-UA / WebGL within a single page load).

``STEALTH_FALLBACK_API``        ``scrapingbee`` | ``scrapfly`` | ``none``
``STEALTH_FALLBACK_API_KEY``    Vendor key for the fallback (free-tier).

Quota
-----
Tracked under ``"stealth"`` in the ledger. Daily hard cap = 40 calls,
between tinyfish (30) and playwright (60). Auth-gated calls are still
counted to bound CI minutes when subscriptions get rotated and every
fetch falls through to playwright.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from typing import Any

import httpx

from .rate_limits import acquire as _rl_acquire
from .telemetry import emit as _emit

log = logging.getLogger(__name__)

_MIN_CONTENT_LENGTH = 300

# Hosts we expect to hold a subscriber storage_state for. Used only to map
# a target URL → state file name. Adding a row here is harmless when the
# corresponding JSON is absent — _state_for() returns None.
_AUTH_HOST_MAP: dict[str, str] = {
    "nytimes.com":      "nyt_state.json",
    "ft.com":           "ft_state.json",
    "wsj.com":          "wsj_state.json",
    "bloomberg.com":    "bloomberg_state.json",
    "theatlantic.com":  "atlantic_state.json",
    "newyorker.com":    "newyorker_state.json",
    "economist.com":    "economist_state.json",
}


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _state_for(url: str) -> str | None:
    """Return the absolute path to a storage_state JSON for *url*, or None.

    Resolution order: ``STEALTH_STORAGE_STATE_PATH`` env (a directory) →
    ``_AUTH_HOST_MAP[host_suffix]`` → existence check on disk.
    """
    base = os.environ.get("STEALTH_STORAGE_STATE_PATH", "").strip()
    if not base:
        return None
    try:
        host = (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return None
    if not host:
        return None
    for suffix, fname in _AUTH_HOST_MAP.items():
        if host == suffix or host.endswith("." + suffix):
            path = os.path.join(base, fname)
            if os.path.exists(path):
                return path
            return None
    return None


def _backend_choice() -> str:
    """Resolve which browser backend to use.

    Order: ``JEEVES_USE_CAMOUFOX=1`` → camoufox if importable → patchright
    if importable → playwright if importable → "none".
    """
    prefer_camoufox = os.environ.get("JEEVES_USE_CAMOUFOX", "").strip() == "1"
    if prefer_camoufox:
        try:
            import camoufox  # noqa: F401
            return "camoufox"
        except Exception:
            pass
    try:
        import patchright  # noqa: F401
        return "patchright"
    except Exception:
        pass
    try:
        import playwright  # noqa: F401
        return "playwright"
    except Exception:
        pass
    return "none"


def _archive_fallback_enabled() -> bool:
    return os.environ.get("JEEVES_STEALTH_ARCHIVE_FALLBACK", "").strip() == "1"


def _fallback_api_choice() -> str:
    return os.environ.get("STEALTH_FALLBACK_API", "").strip().lower()


# ---------------------------------------------------------------------------
# Backend extractor — placeholder for PR1.
#
# In PR2 this opens a fresh Camoufox/patchright context with the resolved
# storage_state + fingerprint, navigates, runs the existing playwright
# settle/score path, returns extracted text. PR1 ships an explicit
# ``not_implemented`` failure so every code path is testable end-to-end
# without a browser binary.
# ---------------------------------------------------------------------------


def _extract_with_backend(
    url: str,
    *,
    backend: str,
    storage_state_path: str | None,
    timeout_seconds: int,
    max_chars: int,
) -> dict[str, Any]:
    """Open a stealth context and extract article text.

    PR1 stub — returns a not-implemented failure so the orchestrator code
    paths are wired and testable. PR2 fills the body using the resolved
    backend (camoufox / patchright). Tests monkeypatch this function.
    """
    return {
        "success": False,
        "title": "",
        "text": "",
        "backend": backend,
        "auth_used": bool(storage_state_path),
        "quality_score": 0.0,
        "error": f"stealth backend not implemented in PR1 (backend={backend})",
    }


def _extract_via_archive(
    url: str,
    *,
    timeout_seconds: int,
    max_chars: int,
) -> dict[str, Any]:
    """Try archive.ph / Wayback as a last-resort free fallback.

    Best-effort. archive.ph occasionally returns 502 under load; Wayback
    CDX returns the most recent capture as a redirect we then GET via
    httpx. Trafilatura cleans the boilerplate. PR1: stub-returns
    not_implemented; PR2 wires the actual fetch.
    """
    return {
        "success": False,
        "title": "",
        "text": "",
        "backend": "archive",
        "auth_used": False,
        "quality_score": 0.0,
        "error": "archive fallback not implemented in PR1",
    }


def _extract_via_fallback_api(
    url: str,
    *,
    api: str,
    timeout_seconds: int,
    max_chars: int,
) -> dict[str, Any]:
    """ScrapingBee or ScrapFly free-tier scrape API. PR1 stub."""
    return {
        "success": False,
        "title": "",
        "text": "",
        "backend": f"fallback_api:{api}",
        "auth_used": False,
        "quality_score": 0.0,
        "error": f"fallback api {api!r} not implemented in PR1",
    }


# ---------------------------------------------------------------------------
# Public extract_article
# ---------------------------------------------------------------------------


def extract_article(
    url: str,
    *,
    timeout_seconds: int = 30,
    max_chars: int = 12_000,
    ledger: Any = None,
) -> dict:
    """Stealth-browser fetch with optional auth + fingerprint diversity.

    Fail-soft: every error path returns ``success=False`` with a populated
    ``error`` key. Quota-recorded on every completed attempt (browser
    launch counts even when navigation fails — that's where the cost is).
    """
    base: dict = {
        "url": url,
        "title": "",
        "text": "",
        "success": False,
        "extracted_via": "stealth",
        "quality_score": 0.0,
        "backend": "",
        "auth_used": False,
    }

    if not url:
        base["error"] = "empty url"
        return base

    # Daily cap guard — refuse before any browser/IO so a runaway loop
    # cannot burn the budget. Mirrors tinyfish.
    if ledger is not None:
        try:
            from .quota import DAILY_HARD_CAPS, QuotaExceeded  # noqa: F401

            cap = DAILY_HARD_CAPS.get("stealth")
            if cap is not None:
                ledger.check_daily_allow("stealth", hard_cap=cap)
        except Exception as exc:
            if exc.__class__.__name__ == "QuotaExceeded":
                log.warning("stealth: daily cap reached, skipping %s", url)
                base["error"] = f"stealth daily cap: {exc}"
                return base

    backend = _backend_choice()
    if backend == "none":
        base["error"] = "stealth: no browser backend available (install patchright or camoufox)"
        _emit("tool_call", provider="stealth", url=url, ok=False,
              latency_ms=0, error="no_backend")
        return base

    storage_state_path = _state_for(url)
    auth_used = bool(storage_state_path)

    _emit(
        "stealth_session_load",
        url=url,
        backend=backend,
        path=storage_state_path or "",
        ok=True,
    )

    t0 = time.monotonic()
    try:
        with _rl_acquire("stealth"):
            result = _extract_with_backend(
                url,
                backend=backend,
                storage_state_path=storage_state_path,
                timeout_seconds=timeout_seconds,
                max_chars=max_chars,
            )
    except Exception as exc:
        log.warning("stealth: backend crashed for %s: %s", url, exc)
        result = {
            "success": False,
            "title": "",
            "text": "",
            "backend": backend,
            "auth_used": auth_used,
            "quality_score": 0.0,
            "error": f"stealth backend crashed: {exc}",
        }
    latency_ms = int((time.monotonic() - t0) * 1000)

    # Charge ledger on every completed attempt — browser launch is the cost.
    if ledger is not None:
        try:
            ledger.record("stealth")
            ledger.record_daily("stealth")
        except Exception as exc:
            log.debug("stealth: ledger.record failed: %s", exc)

    text = str(result.get("text") or "")
    if result.get("success") and len(text) >= _MIN_CONTENT_LENGTH:
        base.update(
            {
                "title": str(result.get("title") or ""),
                "text": text[:max_chars],
                "success": True,
                "quality_score": float(result.get("quality_score") or 0.75),
                "backend": str(result.get("backend") or backend),
                "auth_used": bool(result.get("auth_used") or auth_used),
            }
        )
        _emit(
            "tool_call",
            provider="stealth",
            url=url,
            ok=True,
            chars=len(text),
            backend=base["backend"],
            auth_used=base["auth_used"],
            latency_ms=latency_ms,
        )
        return base

    primary_error = str(result.get("error") or "stealth backend returned no content")

    # Archive fallback — opt-in, free.
    if _archive_fallback_enabled():
        arch = _extract_via_archive(
            url,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
        )
        atext = str(arch.get("text") or "")
        if arch.get("success") and len(atext) >= _MIN_CONTENT_LENGTH:
            base.update(
                {
                    "title": str(arch.get("title") or ""),
                    "text": atext[:max_chars],
                    "success": True,
                    "quality_score": float(arch.get("quality_score") or 0.55),
                    "backend": "archive",
                    "auth_used": False,
                }
            )
            _emit(
                "tool_call",
                provider="stealth",
                url=url,
                ok=True,
                chars=len(atext),
                backend="archive",
                auth_used=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            return base

    # Scrape API fallback — opt-in, free-tier-only.
    fallback_api = _fallback_api_choice()
    if fallback_api in ("scrapingbee", "scrapfly"):
        api_res = _extract_via_fallback_api(
            url,
            api=fallback_api,
            timeout_seconds=timeout_seconds,
            max_chars=max_chars,
        )
        rtext = str(api_res.get("text") or "")
        if api_res.get("success") and len(rtext) >= _MIN_CONTENT_LENGTH:
            base.update(
                {
                    "title": str(api_res.get("title") or ""),
                    "text": rtext[:max_chars],
                    "success": True,
                    "quality_score": float(api_res.get("quality_score") or 0.6),
                    "backend": f"fallback_api:{fallback_api}",
                    "auth_used": False,
                }
            )
            _emit(
                "tool_call",
                provider="stealth",
                url=url,
                ok=True,
                chars=len(rtext),
                backend=f"fallback_api:{fallback_api}",
                auth_used=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
            )
            return base

    # All paths failed — fail-soft.
    base["backend"] = backend
    base["auth_used"] = auth_used
    base["error"] = primary_error
    _emit(
        "tool_call",
        provider="stealth",
        url=url,
        ok=False,
        backend=backend,
        auth_used=auth_used,
        latency_ms=latency_ms,
        error=primary_error[:200],
    )
    return base


# ---------------------------------------------------------------------------
# Helper used by the playwright_extractor shadow wrapper.
# ---------------------------------------------------------------------------


def shadow_call(url: str, *, timeout_seconds: int, max_chars: int) -> dict:
    """Wrapper used by the shadow path in playwright_extractor.extract_article.

    Always fail-soft: returns a dict even on import / runtime errors. Used
    in a ThreadPoolExecutor; the playwright primary call must never be
    affected by exceptions in here.
    """
    t0 = time.monotonic()
    try:
        res = extract_article(url, timeout_seconds=timeout_seconds, max_chars=max_chars)
    except Exception as exc:
        res = {
            "url": url,
            "title": "",
            "text": "",
            "success": False,
            "extracted_via": "stealth",
            "quality_score": 0.0,
            "backend": "",
            "auth_used": False,
            "error": f"stealth shadow exception: {exc}",
        }
    res["_latency_ms"] = int((time.monotonic() - t0) * 1000)
    return res


def auth_hosts() -> tuple[str, ...]:
    """Return the known authenticated-host suffixes (test helper, also
    used by the paywall-first router in PR3)."""
    return tuple(_AUTH_HOST_MAP.keys())
