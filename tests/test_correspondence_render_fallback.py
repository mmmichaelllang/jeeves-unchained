"""Tests for render_with_groq three-tier fallback (sprint-20 hotfix).

Hermetic — every LLM call is monkey-patched. Verifies:
* Groq success → returns Groq output, never invokes NIM/OR.
* Groq 429 → escalates to NIM, returns NIM output.
* Groq 429 + NIM fail → escalates to OR via dynamic resolver.
* Groq non-rate-limit error → re-raised (no silent fallback for real bugs).
* _is_groq_rate_limit matches common shapes.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.delenv("JEEVES_OR_FREE_MODELS", raising=False)
    yield


def _cfg(tmp_path: Path):
    cfg = MagicMock()
    cfg.groq_api_key = "groq-fake"
    cfg.nvidia_api_key = "nim-fake"
    cfg.openrouter_api_key = "or-fake"
    cfg.nim_write_model_id = "meta/llama-3.3-70b-instruct"
    # render_with_groq calls _load_prior_briefing_text(cfg) which reads from
    # cfg.sessions_dir; point at an empty tmp dir.
    cfg.sessions_dir = tmp_path
    cfg.run_date.isoformat.return_value = "2026-05-06"
    return cfg


def _patch_groq_llm(monkeypatch, *, raise_exc=None, return_text="<html>groq</html>"):
    """Stub jeeves.llm.build_groq_llm to return an llm whose chat() does what we want."""
    from jeeves import llm as llm_mod

    class _Resp:
        def __init__(self, content):
            self.message = MagicMock(content=content)

    class _LLM:
        def chat(self, messages):
            if raise_exc is not None:
                raise raise_exc
            return _Resp(return_text)

    monkeypatch.setattr(llm_mod, "build_groq_llm", lambda *a, **kw: _LLM())


def _stub_prompt(monkeypatch, tmp_path):
    """correspondence_write.md is a real file in the repo; pin its content."""
    from jeeves import correspondence as cmod

    prompts = tmp_path / "prompts"
    prompts.mkdir(exist_ok=True)
    (prompts / "correspondence_write.md").write_text("system prompt\n")
    monkeypatch.setattr(cmod, "PROMPTS_DIR", prompts)


def _stub_prior(monkeypatch):
    """Pin _load_prior_briefing_text to empty so we exercise the no-prior path."""
    from jeeves import correspondence as cmod

    monkeypatch.setattr(cmod, "_load_prior_briefing_text", lambda cfg: "")


def _stub_trim(monkeypatch):
    from jeeves import correspondence as cmod

    monkeypatch.setattr(cmod, "_trim_for_render", lambda classified: [])


# ---------------------------------------------------------------------------
# 1. Groq happy path — no fallback invoked
# ---------------------------------------------------------------------------


def test_render_groq_happy_path(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core.base.llms.types")
    from jeeves.correspondence import render_with_groq

    _stub_prompt(monkeypatch, tmp_path)
    _stub_prior(monkeypatch)
    _stub_trim(monkeypatch)
    _patch_groq_llm(monkeypatch, return_text="<html>GROQ_OK</html>")

    nim_called = {"n": 0}

    def _nim_should_not_run(*a, **kw):
        nim_called["n"] += 1
        raise AssertionError("NIM should not have been called")

    from jeeves import write as wmod
    monkeypatch.setattr(wmod, "_try_nim_then_or", _nim_should_not_run)

    out = render_with_groq(_cfg(tmp_path), [], {}, run_date_iso="2026-05-06")
    assert out == "<html>GROQ_OK</html>"
    assert nim_called["n"] == 0


# ---------------------------------------------------------------------------
# 2. Groq 429 → NIM picks up
# ---------------------------------------------------------------------------


def test_render_falls_through_to_nim_on_groq_429(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core.base.llms.types")
    from jeeves.correspondence import render_with_groq

    _stub_prompt(monkeypatch, tmp_path)
    _stub_prior(monkeypatch)
    _stub_trim(monkeypatch)

    class _RateLimitError(Exception):
        pass
    _RateLimitError.__name__ = "RateLimitError"

    _patch_groq_llm(
        monkeypatch,
        raise_exc=_RateLimitError(
            "Error code: 429 - Rate limit reached for model llama-3.3-70b-versatile "
            "tokens per day (TPD): Limit 100000, Used 95811, Requested 10150"
        ),
    )

    from jeeves import write as wmod
    captured = {}

    def _fake(cfg, system, user, *, max_tokens, label):
        captured["label"] = label
        captured["max_tokens"] = max_tokens
        return "<html>NIM_RESCUED</html>"

    monkeypatch.setattr(wmod, "_try_nim_then_or", _fake)

    out = render_with_groq(_cfg(tmp_path), [], {}, run_date_iso="2026-05-06")
    assert out == "<html>NIM_RESCUED</html>"
    assert captured["label"] == "correspondence_render"
    assert captured["max_tokens"] == 4096


# ---------------------------------------------------------------------------
# 3. Non-rate-limit Groq error is re-raised (no silent fallback)
# ---------------------------------------------------------------------------


def test_render_reraises_non_rate_limit_groq_error(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core.base.llms.types")
    from jeeves.correspondence import render_with_groq

    _stub_prompt(monkeypatch, tmp_path)
    _stub_prior(monkeypatch)
    _stub_trim(monkeypatch)
    _patch_groq_llm(monkeypatch, raise_exc=ValueError("invalid request — not a 429"))

    from jeeves import write as wmod

    def _should_not_run(*a, **kw):
        raise AssertionError("fallback must not fire on non-rate-limit errors")

    monkeypatch.setattr(wmod, "_try_nim_then_or", _should_not_run)

    with pytest.raises(ValueError, match="invalid request"):
        render_with_groq(_cfg(tmp_path), [], {}, run_date_iso="2026-05-06")


# ---------------------------------------------------------------------------
# 4. Empty Groq output triggers fallback (defensive)
# ---------------------------------------------------------------------------


def test_render_falls_through_on_empty_groq_output(monkeypatch, tmp_path):
    pytest.importorskip("llama_index.core.base.llms.types")
    from jeeves.correspondence import render_with_groq

    _stub_prompt(monkeypatch, tmp_path)
    _stub_prior(monkeypatch)
    _stub_trim(monkeypatch)
    _patch_groq_llm(monkeypatch, return_text="   ")  # whitespace only

    from jeeves import write as wmod
    monkeypatch.setattr(wmod, "_try_nim_then_or",
                        lambda cfg, sys, usr, *, max_tokens, label: "<html>FALLBACK</html>")

    out = render_with_groq(_cfg(tmp_path), [], {}, run_date_iso="2026-05-06")
    assert out == "<html>FALLBACK</html>"


# ---------------------------------------------------------------------------
# 5. _is_groq_rate_limit pattern matcher
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc, expected",
    [
        (Exception("Error code: 429 - rate_limit_exceeded"), True),
        (Exception("tokens per day (TPD): Limit 100000"), True),
        (Exception("Rate limit reached for model"), True),
        (Exception("TPM exceeded"), True),
        (Exception("authentication failed"), False),
        (Exception("invalid request"), False),
        (Exception("connection refused"), False),
    ],
)
def test_is_groq_rate_limit_classification(exc, expected):
    from jeeves.correspondence import _is_groq_rate_limit

    assert _is_groq_rate_limit(exc) is expected


def test_is_groq_rate_limit_matches_class_name():
    from jeeves.correspondence import _is_groq_rate_limit

    class RateLimitError(Exception):
        pass

    assert _is_groq_rate_limit(RateLimitError("any message")) is True
