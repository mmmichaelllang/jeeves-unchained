"""Tests for the Groq research tier (Phase-D option ii, 2026-05-30).

Inserted between Cerebras-exhausted and OpenRouter in `_run_crawl4ai_sector`.
Default-OFF behind JEEVES_USE_GROQ_RESEARCH_TIER flag. Daily char budget
guard prevents cascade into write-phase TPD overage.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from jeeves import research_sectors as rs


@pytest.fixture(autouse=True)
def _reset_groq_state(monkeypatch):
    """Reset Groq research counter + flag between tests."""
    rs._reset_groq_research_breaker()
    monkeypatch.delenv("JEEVES_USE_GROQ_RESEARCH_TIER", raising=False)
    yield
    rs._reset_groq_research_breaker()


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeResponse:
    def __init__(self, content: str) -> None:
        self.message = _FakeMessage(content)


class _FakeGroqLLM:
    """Stand-in for the Groq LlamaIndex LLM."""

    def __init__(self, response_text: str = "SECTOR JSON", raises: Exception | None = None):
        self._response_text = response_text
        self._raises = raises
        self.calls = 0

    async def achat(self, messages):
        self.calls += 1
        if self._raises:
            raise self._raises
        return _FakeResponse(self._response_text)


class _SpecStub:
    """Minimal spec stand-in matching `_run_crawl4ai_sector` usage."""

    def __init__(self, name: str = "triadic_ontology", default=None):
        self.name = name
        self.default = default if default is not None else {}


# ─── Default-off / runtime flag ─────────────────────────────────────────────

class TestGroqResearchTierFlag:
    def test_default_off_returns_none(self, monkeypatch):
        """No flag set → tier is no-op, returns None."""
        spec = _SpecStub()
        messages = [_FakeMessage("prompt")]
        # Patch build_groq_llm so a False-positive would surface as call attempt.
        called = {"n": 0}

        def _fake_build(*a, **kw):
            called["n"] += 1
            return _FakeGroqLLM()

        monkeypatch.setattr("jeeves.llm.build_groq_llm", _fake_build)

        result = asyncio.run(
            rs._try_groq_research_synthesis(cfg=object(), spec=spec, messages=messages)
        )
        assert result is None
        assert called["n"] == 0  # build_groq_llm never called when flag off

    def test_flag_on_invokes_groq(self, monkeypatch):
        """Flag on → tier calls Groq once and returns its content."""
        monkeypatch.setenv("JEEVES_USE_GROQ_RESEARCH_TIER", "1")
        fake_llm = _FakeGroqLLM(response_text="SYNTHESIZED")
        monkeypatch.setattr("jeeves.llm.build_groq_llm", lambda *a, **kw: fake_llm)

        spec = _SpecStub()
        messages = [_FakeMessage("prompt body 100 chars")]
        result = asyncio.run(
            rs._try_groq_research_synthesis(cfg=object(), spec=spec, messages=messages)
        )
        assert result == "SYNTHESIZED"
        assert fake_llm.calls == 1


# ─── Daily char budget guard ────────────────────────────────────────────────

class TestGroqResearchBudget:
    def test_budget_remaining_starts_at_cap(self):
        assert rs._groq_research_budget_remaining() == rs._GROQ_RESEARCH_DAILY_CAP

    def test_record_use_decrements(self):
        rs._groq_research_record_use(1000)
        assert (
            rs._groq_research_budget_remaining()
            == rs._GROQ_RESEARCH_DAILY_CAP - 1000
        )

    def test_oversize_prompt_skips_groq_and_returns_none(self, monkeypatch):
        """Prompt larger than remaining budget → skip Groq, return None."""
        monkeypatch.setenv("JEEVES_USE_GROQ_RESEARCH_TIER", "1")
        rs._groq_research_record_use(rs._GROQ_RESEARCH_DAILY_CAP - 50)
        # Now ~50 chars remain.
        called = {"n": 0}
        monkeypatch.setattr(
            "jeeves.llm.build_groq_llm",
            lambda *a, **kw: (called.__setitem__("n", called["n"] + 1) or _FakeGroqLLM()),
        )
        spec = _SpecStub()
        # Prompt 500 chars — larger than 50 remaining.
        messages = [_FakeMessage("x" * 500)]
        result = asyncio.run(
            rs._try_groq_research_synthesis(cfg=object(), spec=spec, messages=messages)
        )
        assert result is None
        assert called["n"] == 0  # Groq never built when budget exhausted


# ─── Failure cascade ────────────────────────────────────────────────────────

class TestGroqResearchFailureCascade:
    def test_groq_exception_returns_none(self, monkeypatch):
        """Groq raises → tier returns None so caller cascades to OR."""
        monkeypatch.setenv("JEEVES_USE_GROQ_RESEARCH_TIER", "1")
        monkeypatch.setattr(
            "jeeves.llm.build_groq_llm",
            lambda *a, **kw: _FakeGroqLLM(raises=RuntimeError("rate limit")),
        )
        spec = _SpecStub()
        messages = [_FakeMessage("prompt")]
        result = asyncio.run(
            rs._try_groq_research_synthesis(cfg=object(), spec=spec, messages=messages)
        )
        assert result is None

    def test_build_groq_llm_failure_returns_none(self, monkeypatch):
        """build_groq_llm raises (no key, etc.) → tier returns None."""
        monkeypatch.setenv("JEEVES_USE_GROQ_RESEARCH_TIER", "1")

        def _raise(*a, **kw):
            raise RuntimeError("no GROQ_API_KEY")

        monkeypatch.setattr("jeeves.llm.build_groq_llm", _raise)
        spec = _SpecStub()
        messages = [_FakeMessage("prompt")]
        result = asyncio.run(
            rs._try_groq_research_synthesis(cfg=object(), spec=spec, messages=messages)
        )
        assert result is None


# ─── Successful call records use ────────────────────────────────────────────

class TestGroqResearchAccounting:
    def test_successful_call_records_chars(self, monkeypatch):
        """Successful synthesis decrements remaining budget by prompt+completion."""
        monkeypatch.setenv("JEEVES_USE_GROQ_RESEARCH_TIER", "1")
        fake_llm = _FakeGroqLLM(response_text="X" * 200)
        monkeypatch.setattr("jeeves.llm.build_groq_llm", lambda *a, **kw: fake_llm)
        spec = _SpecStub()
        messages = [_FakeMessage("Y" * 100)]
        before = rs._groq_research_budget_remaining()
        result = asyncio.run(
            rs._try_groq_research_synthesis(cfg=object(), spec=spec, messages=messages)
        )
        assert result == "X" * 200
        # 100 prompt + 200 completion = 300 chars consumed
        assert rs._groq_research_budget_remaining() == before - 300
