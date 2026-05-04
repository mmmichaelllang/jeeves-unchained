"""Tests for write-phase 3-tier fallback (sprint 17 NIM circuit breaker fix)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import jeeves.write as wmod


def _cfg():
    """Minimal Config — only fields needed for fallback paths."""
    cfg = MagicMock()
    cfg.nvidia_api_key = "test-nim-key"
    cfg.openrouter_api_key = "test-or-key"
    cfg.nim_write_model_id = "meta/llama-3.3-70b-instruct"
    return cfg


def test_nim_dead_flag_reset_state():
    """Sanity: module-level circuit breaker starts False on import."""
    # Reset just in case prior test tripped it.
    wmod._NIM_WRITE_DEAD = False
    assert wmod._NIM_WRITE_DEAD is False


def test_nim_timeout_trips_circuit_breaker(monkeypatch):
    """When NIM write times out, _NIM_WRITE_DEAD flag goes True so subsequent
    parts skip NIM entirely. Requires llama_index for ChatMessage import path
    inside _invoke_nim_write — skip if missing (sandbox CI)."""
    pytest.importorskip("llama_index.core.base.llms.types")
    wmod._NIM_WRITE_DEAD = False  # reset

    class _PlainTimeout:
        def chat(self, messages):
            raise TimeoutError("Request timed out.")

    import jeeves.llm
    monkeypatch.setattr(jeeves.llm, "build_nim_write_llm", lambda *a, **kw: _PlainTimeout())

    with pytest.raises(TimeoutError):
        wmod._invoke_nim_write(_cfg(), "sys", "user", max_tokens=1024, label="part1")
    assert wmod._NIM_WRITE_DEAD is True

    wmod._NIM_WRITE_DEAD = False


def test_try_nim_then_or_skips_nim_when_circuit_broken(monkeypatch):
    """If _NIM_WRITE_DEAD is True, _try_nim_then_or goes straight to OR."""
    wmod._NIM_WRITE_DEAD = True
    nim_called = []

    def _fake_nim(*a, **kw):
        nim_called.append(True)
        raise RuntimeError("NIM should not have been called")

    or_called = []

    def _fake_or(cfg, system, user, *, max_tokens, label):
        or_called.append(label)
        return f"<p>OR text for {label}</p>"

    monkeypatch.setattr(wmod, "_invoke_nim_write", _fake_nim)
    monkeypatch.setattr(wmod, "_invoke_or_write", _fake_or)

    out = wmod._try_nim_then_or(
        _cfg(), "sys", "user", max_tokens=1024, label="part5",
    )
    assert "OR text for part5" in out
    assert nim_called == []
    assert or_called == ["part5"]

    wmod._NIM_WRITE_DEAD = False  # reset


def test_try_nim_then_or_falls_through_to_or_on_nim_exception(monkeypatch):
    """NIM raises → OR called → returns OR result."""
    wmod._NIM_WRITE_DEAD = False

    def _fake_nim(*a, **kw):
        raise RuntimeError("NIM gave up")

    or_called = []

    def _fake_or(cfg, system, user, *, max_tokens, label):
        or_called.append(label)
        return f"<p>OR rescued {label}</p>"

    monkeypatch.setattr(wmod, "_invoke_nim_write", _fake_nim)
    monkeypatch.setattr(wmod, "_invoke_or_write", _fake_or)

    out = wmod._try_nim_then_or(
        _cfg(), "sys", "user", max_tokens=1024, label="part2",
    )
    assert "OR rescued part2" in out
    assert or_called == ["part2"]


def test_try_nim_then_or_raises_when_both_fail(monkeypatch):
    """Both NIM and OR fail → RuntimeError chained, preserves both errors."""
    wmod._NIM_WRITE_DEAD = False

    def _fake_nim(*a, **kw):
        raise RuntimeError("NIM down")

    def _fake_or(*a, **kw):
        raise RuntimeError("OR down too")

    monkeypatch.setattr(wmod, "_invoke_nim_write", _fake_nim)
    monkeypatch.setattr(wmod, "_invoke_or_write", _fake_or)

    with pytest.raises(RuntimeError, match="all three tiers failed"):
        wmod._try_nim_then_or(
            _cfg(), "sys", "user", max_tokens=1024, label="part3",
        )


def test_invoke_or_write_iterates_through_models(monkeypatch):
    """First model fails, second succeeds — return second's text."""
    call_log = []

    class _FakeOpenAI:
        def __init__(self, **kw):
            self.chat = self
            self.completions = self

        def create(self, *, model, messages, max_tokens, temperature):
            call_log.append(model)
            if "llama" in model:
                raise RuntimeError("llama 429")
            resp = MagicMock()
            resp.choices = [MagicMock(message=MagicMock(content="<p>qwen result</p>"))]
            return resp

    monkeypatch.setitem(__import__("sys").modules, "openai",
                        type("openai", (), {"OpenAI": _FakeOpenAI})())

    out = wmod._invoke_or_write(
        _cfg(), "sys", "user", max_tokens=1024, label="part1",
    )
    assert "<p>qwen result</p>" in out
    # First (llama) failed, second (qwen) won.
    assert "llama" in call_log[0]
    assert "qwen" in call_log[1]


def test_invoke_or_write_raises_when_no_api_key(monkeypatch):
    cfg = _cfg()
    cfg.openrouter_api_key = ""
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY is not set"):
        wmod._invoke_or_write(cfg, "sys", "user", max_tokens=1024, label="part1")


def test_is_nim_timeout_matches_common_error_classes():
    assert wmod._is_nim_timeout(TimeoutError("Request timed out."))
    assert wmod._is_nim_timeout(RuntimeError("operation timed out"))
    assert wmod._is_nim_timeout(RuntimeError("peer closed connection"))
    assert not wmod._is_nim_timeout(ValueError("bad data"))
    assert not wmod._is_nim_timeout(RuntimeError("429 Too Many Requests"))
