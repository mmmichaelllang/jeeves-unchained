# TinyFish Provider — Reference Skeleton

Draft. Not committed. Reviewer feedback wanted before wiring up.

## 1. Interface being conformed to

Two surfaces, both already in `jeeves/tools/`:

**Search** — factory closure pattern. Reference: `jeeves/tools/serper.py:25-71`.

```python
# jeeves/tools/serper.py:25
def make_serper_search(cfg: Config, ledger: QuotaLedger):
    def serper_search(query: str = "", num: int = 10, tbs: str | None = None) -> str:
        ...
        return json.dumps({"provider": "serper", "query": query, "results": results})
    return serper_search
```

Contract: factory takes `(Config, QuotaLedger)`, returns a callable whose return type is **`str` of `json.dumps(...)`** — NIM tool-output contract, see `jeeves/tools/serper.py:34-36` comment. Each result row keys: `title, url, snippet, published_at, source, provider`.

**Fetch** — module-level function. Reference: `jeeves/tools/firecrawl_extractor.py:21-32`.

```python
# return shape spec at firecrawl_extractor.py:21
{"url": str, "title": str, "text": str, "success": bool,
 "extracted_via": "<provider>", "error": str, "quality_score": float}
```

`extract_article(url, *, timeout_seconds, max_chars)` — fail-soft, never raises.

**Config** — dataclass at `jeeves/config.py:71-89` with one `<provider>_api_key: str` per provider. **Quota** — `QuotaLedger.record(provider, count)` at `jeeves/tools/quota.py:89`.

## 2. Adapter

`jeeves/tools/tinyfish.py` (new file):

```python
"""TinyFish — agentic browser-search provider.

TODO: confirm endpoints/payload schema against TinyFish docs.
      Public docs URL: https://docs.tinyfish.io  (NOT YET VERIFIED — stub).
      All TODO(tinyfish-spec) markers below need a real spec read.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Config
from .quota import QuotaLedger

log = logging.getLogger(__name__)

# TODO(tinyfish-spec): real endpoints
_SEARCH_ENDPOINT = "https://api.tinyfish.io/v1/search"
_FETCH_ENDPOINT = "https://api.tinyfish.io/v1/fetch"

_HTTP_CLIENT = httpx.Client(timeout=httpx.Timeout(connect=5.0, read=25.0, write=10.0, pool=5.0))
atexit.register(_HTTP_CLIENT.close)

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRIES = 3
_BACKOFF_BASE_S = 1.5  # 1.5s, 3s, 6s


@dataclass(frozen=True)
class TinyFishSettings:
    api_key: str
    base_url: str = "https://api.tinyfish.io"
    search_max_results: int = 10
    fetch_max_chars: int = 3000
    fetch_timeout_s: int = 30

    @classmethod
    def from_env(cls) -> "TinyFishSettings":
        return cls(
            api_key=os.environ.get("TINYFISH_API_KEY", ""),
            base_url=os.environ.get("TINYFISH_BASE_URL", "https://api.tinyfish.io"),
            search_max_results=int(os.environ.get("TINYFISH_MAX_RESULTS", "10")),
            fetch_max_chars=int(os.environ.get("TINYFISH_FETCH_MAX_CHARS", "3000")),
            fetch_timeout_s=int(os.environ.get("TINYFISH_FETCH_TIMEOUT_S", "30")),
        )


class TinyFishError(RuntimeError):
    """Maps to the same fail-soft surface used by serper/tavily — caller never sees this raised."""


def _post_with_retry(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    last: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            r = _HTTP_CLIENT.post(url, json=payload, headers=headers)
            if r.status_code in _RETRYABLE_STATUS:
                raise TinyFishError(f"retryable {r.status_code}: {r.text[:200]}")
            r.raise_for_status()
            return r.json()
        except (httpx.TransportError, httpx.HTTPStatusError, TinyFishError) as e:
            last = e
            sleep = _BACKOFF_BASE_S * (2 ** attempt)
            log.warning("tinyfish %s attempt %d/%d failed: %s — sleep %.1fs",
                        url, attempt + 1, _MAX_RETRIES, e, sleep)
            time.sleep(sleep)
    raise TinyFishError(f"tinyfish exhausted {_MAX_RETRIES} retries: {last}")


def make_tinyfish_search(cfg: Config, ledger: QuotaLedger):
    settings = TinyFishSettings.from_env()
    if not settings.api_key:
        log.info("TINYFISH_API_KEY not set — make_tinyfish_search returns disabled stub")

    def tinyfish_search(query: str = "", max_results: int = 10) -> str:
        if not (query or "").strip():
            return "ERROR: tinyfish_search requires a non-empty 'query' argument."
        if not settings.api_key:
            return json.dumps({"provider": "tinyfish", "error": "no api key", "results": []})

        # TODO(tinyfish-spec): confirm payload field names + auth header style
        headers = {"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"}
        payload = {"query": query, "max_results": min(max_results, settings.search_max_results)}

        try:
            data = _post_with_retry(_SEARCH_ENDPOINT, payload, headers)
        except TinyFishError as e:
            log.warning("tinyfish search failed: %s", e)
            return json.dumps({"provider": "tinyfish", "error": str(e), "results": []})

        ledger.record("tinyfish", 1)
        # TODO(tinyfish-spec): real result envelope key (assumed "results")
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("snippet", "") or r.get("summary", ""),
                "published_at": r.get("published_at", ""),
                "source": r.get("source", ""),
                "provider": "tinyfish",
            }
            for r in (data.get("results") or [])
        ]
        return json.dumps({"provider": "tinyfish", "query": query, "results": results})

    return tinyfish_search


def extract_article(url: str, *, timeout_seconds: int = 30, max_chars: int = 3000,
                    ledger: QuotaLedger | None = None) -> dict[str, Any]:
    """Fetch tier — matches firecrawl_extractor.extract_article shape exactly."""
    settings = TinyFishSettings.from_env()
    base = {"url": url, "title": "", "text": "", "success": False,
            "extracted_via": "tinyfish", "quality_score": 0.0}

    if not settings.api_key:
        return {**base, "error": "TINYFISH_API_KEY not set"}

    headers = {"Authorization": f"Bearer {settings.api_key}", "Content-Type": "application/json"}
    # TODO(tinyfish-spec): confirm fetch payload schema
    payload = {"url": url, "render_js": True, "max_chars": max_chars}

    try:
        data = _post_with_retry(_FETCH_ENDPOINT, payload, headers)
    except TinyFishError as e:
        log.warning("tinyfish fetch failed for %s: %s", url, e)
        return {**base, "error": str(e)}

    if ledger is not None:
        try: ledger.record("tinyfish", 1)
        except Exception: pass

    text = (data.get("markdown") or data.get("text") or "")[:max_chars]
    return {
        **base,
        "title": data.get("title", "") or "",
        "text": text,
        "success": bool(text and len(text) >= 300),
        "quality_score": min(1.0, len(text) / 2000.0),  # crude, matches firecrawl heuristic
    }
```

## 3. Wire-up diff

`jeeves/config.py` (around line 76):

```diff
     exa_api_key: str
+    tinyfish_api_key: str
     google_api_key: str
@@ around line 172
     exa_api_key=os.environ.get("EXA_API_KEY", ""),
+    tinyfish_api_key=os.environ.get("TINYFISH_API_KEY", ""),
```

`jeeves/tools/__init__.py` (after line 28 import block, then in `tools = [...]`):

```diff
     from .tavily import make_tavily_extract, make_tavily_search
+    from .tinyfish import make_tinyfish_search
@@
+        FunctionTool.from_defaults(
+            fn=make_tinyfish_search(cfg, ledger),
+            name="tinyfish_search",
+            description="TinyFish agentic-browser SERP. Best for JS-heavy sites that "
+                        "Serper misses. Args: query (str), max_results (int=10).",
+        ),
```

`jeeves/tools/quota.py` `DEFAULT_STATE` (line 21):

```diff
     "exa": {"used": 0, "free_cap": 500, "overage_per_1k_usd": 5.00},
+    "tinyfish": {"used": 0, "free_cap": 0, "overage_per_1k_usd": 0.0},  # TODO real pricing
```

## 4. Test skeleton

`tests/test_tinyfish.py` — pytest + monkeypatch + MagicMock, mirroring `tests/test_firecrawl_extractor.py`:

```python
from unittest.mock import MagicMock
import json, pytest
from jeeves.tools import tinyfish as tf

def test_search_no_key_returns_error_envelope(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    cfg = MagicMock(); ledger = MagicMock()
    out = json.loads(tf.make_tinyfish_search(cfg, ledger)(query="x"))
    assert out["provider"] == "tinyfish" and out["results"] == []

def test_search_empty_query_returns_error_string():
    cfg = MagicMock(); ledger = MagicMock()
    out = tf.make_tinyfish_search(cfg, ledger)(query="")
    assert out.startswith("ERROR:")

def test_search_records_quota_on_success(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    fake = MagicMock(); fake.status_code = 200
    fake.json.return_value = {"results": [{"title": "T", "url": "https://x", "snippet": "s"}]}
    monkeypatch.setattr(tf._HTTP_CLIENT, "post", lambda *a, **k: fake)
    ledger = MagicMock()
    out = json.loads(tf.make_tinyfish_search(MagicMock(), ledger)(query="q"))
    ledger.record.assert_called_once_with("tinyfish", 1)
    assert out["results"][0]["provider"] == "tinyfish"

def test_retry_on_429(monkeypatch):
    monkeypatch.setenv("TINYFISH_API_KEY", "k")
    monkeypatch.setattr(tf.time, "sleep", lambda *_: None)
    seq = [MagicMock(status_code=429, text="rate"), MagicMock(status_code=429, text="rate"),
           MagicMock(status_code=200, **{"json.return_value": {"results": []}})]
    monkeypatch.setattr(tf._HTTP_CLIENT, "post", lambda *a, **k: seq.pop(0))
    out = json.loads(tf.make_tinyfish_search(MagicMock(), MagicMock())(query="q"))
    assert out["results"] == [] and seq == []  # all three consumed

def test_extract_article_quality_score_always_present(monkeypatch):
    monkeypatch.delenv("TINYFISH_API_KEY", raising=False)
    out = tf.extract_article("https://x")
    assert "quality_score" in out and out["success"] is False
```

## 5. Reviewer checklist

1. Verify TinyFish endpoint URLs, auth header style, and response envelope keys against actual docs — every `TODO(tinyfish-spec)` marker.
2. Confirm `tinyfish_search` returns `json.dumps(...)` in both success AND error paths (NIM contract — see `jeeves/tools/serper.py:34-36`).
3. Confirm `extract_article` return dict matches `firecrawl_extractor.py:21-32` shape exactly, including `quality_score` and `extracted_via: "tinyfish"`.
4. `QuotaLedger.record("tinyfish", 1)` fires only on real success — never on error/empty-key paths (test #1 covers).
5. `DEFAULT_STATE["tinyfish"]` pricing/`free_cap` numbers are real, not placeholder zeros.
6. Module-level `_HTTP_CLIENT` has `atexit.register(close)` — matches sprint-16 audit fix in `jeeves/tools/serper.py:22`.
7. Retry logic: 429 + 5xx only, max 3 attempts, exponential backoff. Confirm 4xx-non-429 fail fast (auth bugs shouldn't burn 6s of sleep).
8. Settings read from env at factory-build time, not at import — otherwise tests can't `monkeypatch.setenv`.
9. Decide where TinyFish fits in the fetch chain (httpx → Jina → Firecrawl → Playwright → ?). Document position in module docstring like `firecrawl_extractor.py:3`.
10. New `TINYFISH_API_KEY` added to `PHASE_REQUIREMENTS["research"]` only if TinyFish becomes mandatory; otherwise leave optional and rely on the empty-key disabled-stub path.
