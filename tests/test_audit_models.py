"""Tests for the reasoning-first audit model resolver."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    from jeeves import audit_models

    audit_models.reset_cache_for_tests()
    for var in ("JEEVES_AUDIT_MODEL", "JEEVES_AUDIT_MODELS"):
        monkeypatch.delenv(var, raising=False)
    yield
    audit_models.reset_cache_for_tests()


class _FakeResp:
    def __init__(self, payload):
        self._p = payload
    def raise_for_status(self):
        pass
    def json(self):
        return self._p


class _FakeClient:
    def __init__(self, payload=None, raise_on_get=None):
        self._payload = payload
        self._raise = raise_on_get
    def __enter__(self):
        return self
    def __exit__(self, *exc):
        return False
    def get(self, url):
        if self._raise:
            raise self._raise
        return _FakeResp(self._payload or {"data": []})


def _patch_httpx(monkeypatch, payload=None, raise_on_get=None):
    import httpx
    monkeypatch.setattr(
        httpx, "Client", lambda *a, **kw: _FakeClient(payload, raise_on_get),
        raising=True,
    )


def test_reasoning_marker_outranks_size(monkeypatch):
    """A 70B reasoning model should outrank a 405B vanilla instruct."""
    from jeeves.audit_models import resolve_audit_models

    payload = {"data": [
        {"id": "vendor/big-405b-instruct:free", "context_length": 32768},
        {"id": "deepseek/deepseek-r1-70b:free", "context_length": 32768},
    ]}
    _patch_httpx(monkeypatch, payload)
    chain = resolve_audit_models()
    assert chain[0] == "deepseek/deepseek-r1-70b:free"
    assert "vendor/big-405b-instruct:free" in chain


def test_filters_short_context(monkeypatch):
    """Models with context < 16K are dropped — auditor needs long-form."""
    from jeeves.audit_models import resolve_audit_models

    payload = {"data": [
        {"id": "vendor/short-7b-instruct:free", "context_length": 4096},
        {"id": "vendor/long-7b-instruct:free", "context_length": 32768},
    ]}
    _patch_httpx(monkeypatch, payload)
    chain = resolve_audit_models()
    assert chain == ("vendor/long-7b-instruct:free",)


def test_filters_specialized_variants(monkeypatch):
    from jeeves.audit_models import resolve_audit_models

    payload = {"data": [
        {"id": "qwen/qwen3-coder-480b:free", "context_length": 65536},
        {"id": "vendor/llama-3-70b-vision:free", "context_length": 32768},
        {"id": "vendor/embed-3-large:free", "context_length": 65536},
        {"id": "vendor/llama-70b-guard:free", "context_length": 32768},
        {"id": "deepseek/deepseek-r1:free", "context_length": 65536},
    ]}
    _patch_httpx(monkeypatch, payload)
    chain = resolve_audit_models()
    assert chain == ("deepseek/deepseek-r1:free",)


def test_fetch_failure_falls_back(monkeypatch):
    from jeeves import audit_models

    _patch_httpx(monkeypatch, raise_on_get=RuntimeError("network down"))
    chain = audit_models.resolve_audit_models()
    assert chain == audit_models._AUDIT_MODELS_FALLBACK
    assert any("deepseek" in m or "llama" in m for m in chain)


def test_single_model_env_override(monkeypatch):
    from jeeves.audit_models import resolve_audit_models

    monkeypatch.setenv("JEEVES_AUDIT_MODEL", "vendor/pinned-model:free")
    chain = resolve_audit_models()
    assert chain == ("vendor/pinned-model:free",)


def test_chain_env_override(monkeypatch):
    from jeeves.audit_models import resolve_audit_models

    monkeypatch.setenv(
        "JEEVES_AUDIT_MODELS",
        "vendor/a:free,vendor/b:free, vendor/c:free",
    )
    chain = resolve_audit_models()
    assert chain == ("vendor/a:free", "vendor/b:free", "vendor/c:free")


def test_env_override_bypasses_fetch(monkeypatch):
    from jeeves import audit_models

    monkeypatch.setenv("JEEVES_AUDIT_MODEL", "vendor/x:free")

    def _no_fetch():
        raise AssertionError("fetch should not run when env override set")

    monkeypatch.setattr(audit_models, "_fetch_audit_models", _no_fetch)
    chain = audit_models.resolve_audit_models()
    assert chain == ("vendor/x:free",)


def test_cache_memoizes(monkeypatch):
    from jeeves import audit_models

    counter = {"n": 0}

    def _counting_fetch():
        counter["n"] += 1
        return ("vendor/cached:free",)

    monkeypatch.setattr(audit_models, "_fetch_audit_models", _counting_fetch)
    audit_models.resolve_audit_models()
    audit_models.resolve_audit_models()
    audit_models.resolve_audit_models()
    assert counter["n"] == 1
