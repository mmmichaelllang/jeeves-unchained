"""LLM factories — Kimi K2.5 on NVIDIA NIM for research, Groq for write."""

from __future__ import annotations

from .config import Config


def build_kimi_llm(cfg: Config, *, temperature: float = 0.3, max_tokens: int = 8192):
    """Return a LlamaIndex LLM bound to Kimi K2.5 on NVIDIA NIM.

    The NIM endpoint is OpenAI-compatible. `llama-index-llms-nvidia` handles
    native tool-calling format so FunctionAgent can dispatch parallel tool
    calls without a ReAct shim.
    """

    from llama_index.llms.nvidia import NVIDIA

    return NVIDIA(
        model=cfg.kimi_model_id,
        api_key=cfg.nvidia_api_key,
        base_url=cfg.kimi_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
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


def _ensure_groq_key() -> str:
    import os

    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY is required for the write phase.")
    return key
