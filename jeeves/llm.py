"""LLM factories — Kimi K2.5 on NVIDIA NIM for research, Groq for write (NIM fallback)."""

from __future__ import annotations

import json
import logging

from .config import Config

log = logging.getLogger(__name__)


def _build_kimi_class():
    """Build a NVIDIA subclass with a None-tolerant tool-arg parser.

    Kimi K2.5 hosted on NIM occasionally emits tool calls where
    `function.arguments` is `None` instead of `"{}"`. The upstream class
    does `json.loads(None)` and raises `TypeError`, which kills the
    entire FunctionAgent workflow. This override treats None / empty
    strings / unparseable JSON as `{}` and logs a warning so we can
    spot the upstream bug in CI logs.
    """

    from llama_index.core.llms.llm import ToolSelection
    from llama_index.llms.nvidia import NVIDIA

    class KimiNVIDIA(NVIDIA):
        def get_tool_calls_from_response(
            self, response, error_on_no_tool_call: bool = True
        ):
            tool_calls = response.message.additional_kwargs.get("tool_calls", [])
            if len(tool_calls) < 1:
                if error_on_no_tool_call:
                    raise ValueError(
                        f"Expected at least one tool call, but got {len(tool_calls)} tool calls."
                    )
                return []

            out = []
            for tool_call in tool_calls:
                raw = tool_call.function.arguments
                if raw is None or raw == "":
                    log.warning(
                        "kimi tool call %s returned None/empty arguments; coercing to {}",
                        tool_call.function.name,
                    )
                    args = {}
                else:
                    try:
                        args = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        # FunctionAgent calls this parser on every streamed chunk,
                        # so partial args like '{"query": "Edm' flood the log mid-
                        # stream. DEBUG keeps it reachable without drowning the
                        # signal at WARNING.
                        log.debug(
                            "kimi tool call %s arguments not valid JSON (%r); coercing to {}",
                            tool_call.function.name, raw,
                        )
                        args = {}
                out.append(
                    ToolSelection(
                        tool_id=tool_call.id,
                        tool_name=tool_call.function.name,
                        tool_kwargs=args,
                    )
                )
            return out

    return KimiNVIDIA


def build_kimi_llm(
    cfg: Config,
    *,
    temperature: float = 0.3,
    max_tokens: int = 8192,
    timeout: float = 180.0,
):
    """Return a LlamaIndex LLM bound to Kimi K2.5 on NVIDIA NIM.

    The NIM endpoint is OpenAI-compatible. `llama-index-llms-nvidia` handles
    native tool-calling format so FunctionAgent can dispatch parallel tool
    calls without a ReAct shim.
    """

    cls = _build_kimi_class()
    return cls(
        model=cfg.kimi_model_id,
        api_key=cfg.nvidia_api_key,
        base_url=cfg.kimi_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def build_groq_llm(cfg: Config, *, temperature: float = 0.65, max_tokens: int = 8192):
    """Return a LlamaIndex LLM bound to Groq Llama 3.3 70B. Used in Phase 3."""

    from llama_index.llms.groq import Groq

    return Groq(
        model=cfg.groq_model_id,
        api_key=_ensure_groq_key(),
        temperature=temperature,
        max_tokens=max_tokens,
        top_p=0.9,
    )


def build_nim_write_llm(cfg: Config, *, temperature: float = 0.65, max_tokens: int = 4096):
    """Return a LlamaIndex LLM bound to a write-capable model on NVIDIA NIM.

    Used as an automatic fallback when Groq's free-tier daily TPD quota is
    exhausted. Defaults to meta/llama-3.3-70b-instruct — same model family
    as the Groq primary (llama-3.3-70b-versatile), different host.

    Uses the same NIM endpoint and NVIDIA_API_KEY as the research phase, so
    no extra secrets are required. Override the model with NIM_WRITE_MODEL_ID.
    """

    from llama_index.llms.nvidia import NVIDIA

    return NVIDIA(
        model=cfg.nim_write_model_id,
        api_key=cfg.nvidia_api_key,
        base_url=cfg.kimi_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
    )


def _ensure_groq_key() -> str:
    import os

    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY is required for the write phase.")
    return key
