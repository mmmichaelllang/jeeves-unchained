"""Tests for correspondence._classify_batch_with_openrouter (sprint 16 audit fix #1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jeeves.correspondence import _classify_batch_with_openrouter
from jeeves.config import Config


class _FakeChatMessage:
    """Minimal stand-in for llama_index ChatMessage (avoids heavy import)."""
    class _Role:
        def __init__(self, v):
            self.value = v

    def __init__(self, role: str, content: str):
        self.role = self._Role(role)
        self.content = content


def _make_cfg(or_key: str = "test-or-key") -> Config:
    """Build a Config with only the fields we care about."""
    cfg = Config.__new__(Config)
    cfg.openrouter_api_key = or_key
    return cfg


def test_or_fallback_returns_empty_when_no_api_key(monkeypatch):
    cfg = _make_cfg(or_key="")
    out = _classify_batch_with_openrouter(cfg, [_FakeChatMessage("user", "hi")])
    assert out == ""


def test_or_fallback_returns_first_model_response(monkeypatch):
    cfg = _make_cfg()
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.choices = [MagicMock(message=MagicMock(content='[{"id":"x"}]'))]
    fake_client.chat.completions.create.return_value = fake_resp

    class _FakeOpenAI:
        def __init__(self, **kw):
            pass

        chat = fake_client.chat

    monkeypatch.setitem(__import__("sys").modules, "openai",
                        type("openai", (), {"OpenAI": _FakeOpenAI})())

    out = _classify_batch_with_openrouter(cfg, [_FakeChatMessage("user", "hi")])
    assert out == '[{"id":"x"}]'


def test_or_fallback_iterates_through_models(monkeypatch):
    """First model fails, second succeeds — must return second result."""
    cfg = _make_cfg()
    call_log = []

    class _FakeClient:
        def __init__(self, **kw):
            self.chat = self
            self.completions = self

        def create(self, *, model, messages, max_tokens, temperature):
            call_log.append(model)
            if "llama" in model:
                raise RuntimeError("rate limit on llama")
            resp = MagicMock()
            resp.choices = [MagicMock(message=MagicMock(content="[]"))]
            return resp

    monkeypatch.setitem(__import__("sys").modules, "openai",
                        type("openai", (), {"OpenAI": _FakeClient})())

    out = _classify_batch_with_openrouter(cfg, [_FakeChatMessage("user", "hi")])
    assert out == "[]"
    assert len(call_log) == 2  # llama failed, then qwen succeeded
    assert "llama" in call_log[0]
    assert "qwen" in call_log[1]


def test_or_fallback_returns_empty_when_all_models_fail(monkeypatch):
    cfg = _make_cfg()

    class _FakeClient:
        def __init__(self, **kw):
            self.chat = self
            self.completions = self

        def create(self, **kw):
            raise RuntimeError("all OR models down")

    monkeypatch.setitem(__import__("sys").modules, "openai",
                        type("openai", (), {"OpenAI": _FakeClient})())

    out = _classify_batch_with_openrouter(cfg, [_FakeChatMessage("user", "hi")])
    assert out == ""
