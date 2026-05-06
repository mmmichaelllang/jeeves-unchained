"""Tests for the dynamic OpenRouter free-tier model resolver.

Hermetic — no real HTTP. Stubs ``httpx.Client`` to control every fetch
result. Verifies:
* Live-fetch path returns models ranked by param count then context.
* Vision/coder/embedding/guard variants are skipped.
* Models below ``_OR_FREE_MIN_CONTEXT_LENGTH`` are dropped.
* Fetch failure falls back to ``_OR_FREE_MODELS_FALLBACK``.
* Per-process cache memoises within the TTL window.
* ``JEEVES_OR_FREE_MODELS`` env override bypasses the network entirely.
* ``_parse_param_count_billions`` handles common naming variants.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Reset the module-level cache + env before every test."""
    from jeeves import write as w

    monkeypatch.setattr(w, "_OR_FREE_MODELS_CACHE", None, raising=True)
    for var in ("JEEVES_OR_FREE_MODELS",):
        monkeypatch.delenv(var, raising=False)
    yield
    monkeypatch.setattr(w, "_OR_FREE_MODELS_CACHE", None, raising=False)


class _FakeResp:
    def __init__(self, status: int, payload: dict):
        self.status_code = status
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, payload=None, raise_on_get=None):
        self._payload = payload
        self._raise = raise_on_get

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url):
        if self._raise is not None:
            raise self._raise
        return _FakeResp(200, self._payload or {"data": []})


def _patch_httpx(monkeypatch, payload=None, raise_on_get=None):
    import httpx

    def _factory(*a, **kw):
        return _FakeClient(payload=payload, raise_on_get=raise_on_get)

    monkeypatch.setattr(httpx, "Client", _factory, raising=True)


def test_live_fetch_ranks_by_param_count_then_context(monkeypatch):
    from jeeves import write as w

    payload = {
        "data": [
            {"id": "vendor/small-7b-instruct:free", "context_length": 32768},
            {"id": "vendor/big-72b-instruct:free", "context_length": 8192},
            {"id": "vendor/medium-24b-instruct:free", "context_length": 65536},
            {"id": "vendor/no-params-instruct:free", "context_length": 32768},
        ]
    }
    _patch_httpx(monkeypatch, payload=payload)

    chain = w._resolve_or_free_models()
    assert chain[0] == "vendor/big-72b-instruct:free"
    assert chain[1] == "vendor/medium-24b-instruct:free"
    assert chain[2] == "vendor/small-7b-instruct:free"
    assert chain[3] == "vendor/no-params-instruct:free"


def test_skip_specialized_variants(monkeypatch):
    from jeeves import write as w

    payload = {
        "data": [
            {"id": "vendor/qwen-72b-vision:free", "context_length": 32768},
            {"id": "vendor/qwen-72b-coder:free", "context_length": 32768},
            {"id": "vendor/qwen-2.5-vl-72b:free", "context_length": 32768},
            {"id": "vendor/llama-guard-7b:free", "context_length": 32768},
            {"id": "vendor/embed-3-large:free", "context_length": 32768},
            {"id": "vendor/qwen-72b-instruct:free", "context_length": 32768},
        ]
    }
    _patch_httpx(monkeypatch, payload=payload)

    chain = w._resolve_or_free_models()
    assert chain == ("vendor/qwen-72b-instruct:free",)


def test_drops_short_context_models(monkeypatch):
    from jeeves import write as w

    payload = {
        "data": [
            {"id": "vendor/tiny-1b-instruct:free", "context_length": 4096},
            {"id": "vendor/big-70b-instruct:free", "context_length": 8192},
        ]
    }
    _patch_httpx(monkeypatch, payload=payload)

    chain = w._resolve_or_free_models()
    assert chain == ("vendor/big-70b-instruct:free",)


def test_fetch_failure_falls_back(monkeypatch):
    from jeeves import write as w

    _patch_httpx(monkeypatch, raise_on_get=RuntimeError("network down"))

    chain = w._resolve_or_free_models()
    assert chain == w._OR_FREE_MODELS_FALLBACK
    assert any("llama" in m for m in chain)


def test_empty_payload_falls_back(monkeypatch):
    from jeeves import write as w

    _patch_httpx(monkeypatch, payload={"data": []})

    chain = w._resolve_or_free_models()
    assert chain == w._OR_FREE_MODELS_FALLBACK


def test_unexpected_payload_shape_falls_back(monkeypatch):
    from jeeves import write as w

    _patch_httpx(monkeypatch, payload={"unexpected": "shape"})

    chain = w._resolve_or_free_models()
    assert chain == w._OR_FREE_MODELS_FALLBACK


def test_cache_memoizes_within_ttl(monkeypatch):
    from jeeves import write as w

    calls = {"n": 0}

    def _counting_fetch():
        calls["n"] += 1
        return ("vendor/cached-70b-instruct:free",)

    monkeypatch.setattr(w, "_fetch_or_free_models", _counting_fetch, raising=True)

    a = w._resolve_or_free_models()
    b = w._resolve_or_free_models()
    c = w._resolve_or_free_models()
    assert a == b == c == ("vendor/cached-70b-instruct:free",)
    assert calls["n"] == 1


def test_env_override_bypasses_fetch(monkeypatch):
    from jeeves import write as w

    monkeypatch.setenv(
        "JEEVES_OR_FREE_MODELS",
        "vendor/a:free, vendor/b:free, vendor/c:free",
    )

    fetch_called = {"n": 0}

    def _should_not_run():
        fetch_called["n"] += 1
        return None

    monkeypatch.setattr(w, "_fetch_or_free_models", _should_not_run, raising=True)

    chain = w._resolve_or_free_models()
    assert chain == ("vendor/a:free", "vendor/b:free", "vendor/c:free")
    assert fetch_called["n"] == 0


@pytest.mark.parametrize(
    "model_id, expected",
    [
        ("meta-llama/llama-3.3-70b-instruct:free", 70.0),
        ("vendor/qwen-2.5-72b-instruct:free", 72.0),
        ("vendor/qwen3-coder-480b-a35b:free", 480.0),
        ("vendor/no-numbers-instruct:free", 0.0),
        ("vendor/llama-3.1-8b-instruct:free", 8.0),
        ("vendor/mistral-small-3.1-24b-instruct:free", 24.0),
    ],
)
def test_param_count_parser(model_id, expected):
    from jeeves.write import _parse_param_count_billions

    assert _parse_param_count_billions(model_id) == expected
