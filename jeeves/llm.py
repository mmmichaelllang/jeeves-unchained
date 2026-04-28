"""LLM factories — Kimi K2.5 on NVIDIA NIM for research, Groq for write (NIM fallback)."""

from __future__ import annotations

import json
import logging

from .config import Config

log = logging.getLogger(__name__)


def _build_kimi_class():
    """Build a NVIDIA subclass with a None-tolerant tool-arg parser and pre-send normalizer.

    Kimi K2.5 hosted on NIM occasionally emits tool calls where
    `function.arguments` is `None` instead of `"{}"`. The upstream class
    does `json.loads(None)` and raises `TypeError`, which kills the
    entire FunctionAgent workflow. This override treats None / empty
    strings / unparseable JSON as `{}` and logs a warning so we can
    spot the upstream bug in CI logs.

    Additionally overrides `achat_with_tools` to normalize every
    `ToolCallBlock.tool_kwargs` in the chat history before each NIM call.
    When arguments=None, LlamaIndex's `from_openai_message` stores
    `ToolCallBlock(tool_kwargs={})` — an empty dict.  When re-serialized
    by `to_openai_message_dict`, that dict becomes `"arguments": {}` (a
    JSON object) rather than the required JSON string `"{}"`.  NIM rejects
    the malformed request with 400 "Extra data: line 1 column 3 (char 2)".
    Normalizing tool_kwargs → "{}" before every send is the reliable fix.

    Also strips degenerate tool call entries with id=None from
    `additional_kwargs["tool_calls"]` before each NIM send.  Kimi
    occasionally emits tool calls where both id and function.name are None.
    These are already skipped by `get_tool_calls_from_response`, but
    LlamaIndex still records the raw assistant message (including the
    id=None entries) in the chat history.  NIM's pydantic validator then
    rejects the next request with 400 "Input should be a valid string"
    for ChatCompletionMessageFunctionToolCallParam.id, crashing the sector.
    """

    from llama_index.core.llms.llm import ToolSelection
    from llama_index.llms.nvidia import NVIDIA

    class KimiNVIDIA(NVIDIA):
        @staticmethod
        def _normalize_tool_kwargs(messages):
            """Ensure ToolCallBlock.tool_kwargs and ChoiceDelta arguments are JSON strings.

            Must be called on the full chat_history list before every NIM call so
            that any assistant messages carrying tool calls with None/empty-dict
            arguments are fixed in-place prior to serialization.
            """
            from llama_index.core.base.llms.types import ToolCallBlock as _TCB

            for msg in messages:
                # Fix ToolCallBlock.tool_kwargs (used by to_openai_message_dict
                # for the ToolCallBlock path — sets "arguments": <value>).
                for block in getattr(msg, "blocks", []):
                    if not isinstance(block, _TCB):
                        continue
                    kw = block.tool_kwargs
                    if kw is None or kw == {} or kw == "":
                        block.tool_kwargs = "{}"
                    elif isinstance(kw, dict):
                        block.tool_kwargs = json.dumps(kw)
                    elif isinstance(kw, str):
                        # Validate the string — "null" or invalid JSON must become "{}".
                        # This catches the "{}null" corruption produced by the old mutation
                        # bug, as well as JSON-null arguments Kimi occasionally emits.
                        try:
                            parsed = json.loads(kw)
                            if parsed is None:
                                block.tool_kwargs = "{}"
                        except (TypeError, json.JSONDecodeError):
                            block.tool_kwargs = "{}"
                # Belt-and-suspenders: also fix additional_kwargs["tool_calls"]
                # (used by to_openai_message_dict for the non-ToolCallBlock path).
                # Also strips entries with id=None — NIM's pydantic validator
                # requires id to be a non-null string; leaving None entries in
                # the history causes a 400 "Input should be a valid string" crash
                # on every subsequent NIM call in the sector.
                raw_tcs = msg.additional_kwargs.get("tool_calls") or []
                if raw_tcs:
                    kept = []
                    for tc in raw_tcs:
                        if getattr(tc, "id", None) is None:
                            log.debug("_normalize_tool_kwargs: dropping tool call with id=None from history")
                            continue
                        fn = getattr(tc, "function", None)
                        if fn is not None:
                            args = getattr(fn, "arguments", None)
                            if args is None or args == "":
                                try:
                                    fn.arguments = "{}"
                                except Exception:
                                    pass
                            elif isinstance(args, dict):
                                try:
                                    fn.arguments = json.dumps(args)
                                except Exception:
                                    pass
                        kept.append(tc)
                    msg.additional_kwargs["tool_calls"] = kept

        async def astream_chat_with_tools(self, tools, user_msg=None, chat_history=None, **kwargs):
            # FunctionAgent.take_step() always uses streaming=True (BaseWorkflowAgent default),
            # so it calls astream_chat_with_tools — NOT achat_with_tools. This is the only
            # method that actually runs before each NIM request in production.
            if chat_history:
                self._normalize_tool_kwargs(chat_history)
            return await super().astream_chat_with_tools(
                tools, user_msg=user_msg, chat_history=chat_history, **kwargs
            )

        async def achat_with_tools(self, tools, user_msg=None, chat_history=None, **kwargs):
            if chat_history:
                self._normalize_tool_kwargs(chat_history)
            return await super().achat_with_tools(
                tools, user_msg=user_msg, chat_history=chat_history, **kwargs
            )

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
                # Skip tool calls with None id or name — Kimi occasionally emits
                # degenerate tool call entries. Creating ToolSelection with None
                # fields raises a pydantic ValidationError that kills the sector.
                tc_id = getattr(tool_call, "id", None)
                tc_name = getattr(tool_call.function, "name", None)
                if tc_id is None or tc_name is None:
                    log.warning(
                        "kimi emitted tool call with None id/name (%r/%r); skipping.",
                        tc_id, tc_name,
                    )
                    continue

                raw = tool_call.function.arguments
                if raw is None or raw == "":
                    log.warning(
                        "kimi tool call %s returned None/empty arguments; coercing to {}",
                        tc_name,
                    )
                    args = {}
                    # IMPORTANT: do NOT mutate tool_call.function.arguments here.
                    # This method is called on every streaming chunk (function_agent.py
                    # line 78). Mutating the live accumulator corrupts update_tool_calls()
                    # concatenation: "" → "{}", then Kimi's next delta (e.g. "null") appends
                    # → "{}null". json.loads("{}null") → "Extra data: line 1 column 3 (char 2)".
                    # _normalize_tool_kwargs in astream_chat_with_tools handles history
                    # normalization before each NIM send, which is the right place.
                else:
                    try:
                        args = json.loads(raw)
                        if args is None:  # handles JSON "null" from Kimi
                            args = {}
                    except (TypeError, json.JSONDecodeError):
                        # FunctionAgent calls this parser on every streamed chunk,
                        # so partial args like '{"query": "Edm' flood the log mid-
                        # stream. DEBUG keeps it reachable without drowning the
                        # signal at WARNING.
                        log.debug(
                            "kimi tool call %s arguments not valid JSON (%r); coercing to {}",
                            tc_name, raw,
                        )
                        args = {}
                out.append(
                    ToolSelection(
                        tool_id=tc_id,
                        tool_name=tc_name,
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
        max_retries=0,  # let run_sector own all retry logic with proper 60s backoff
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


def build_nim_write_llm(
    cfg: Config,
    *,
    temperature: float = 0.65,
    max_tokens: int = 4096,
    timeout: float = 180.0,
):
    """Return a LlamaIndex LLM bound to a write-capable model on NVIDIA NIM.

    Used as an automatic fallback when Groq's free-tier daily TPD quota is
    exhausted. Defaults to meta/llama-3.3-70b-instruct — same model family
    as the Groq primary (llama-3.3-70b-versatile), different host.

    Uses the same NIM endpoint and NVIDIA_API_KEY as the research phase, so
    no extra secrets are required. Override the model with NIM_WRITE_MODEL_ID.

    timeout=180s per request (up from openai's 60s default) — a ~4000-char
    briefing part on a busy NIM endpoint can take 60-120s, and the SDK's
    auto-retry behavior means a short timeout causes spurious APITimeoutError
    failures (observed in the wild on 2026-04-24 part 5).
    """

    from llama_index.llms.nvidia import NVIDIA

    return NVIDIA(
        model=cfg.nim_write_model_id,
        api_key=cfg.nvidia_api_key,
        base_url=cfg.kimi_base_url,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def _ensure_groq_key() -> str:
    import os

    key = os.environ.get("GROQ_API_KEY", "")
    if not key:
        raise RuntimeError("GROQ_API_KEY is required for the write phase.")
    return key
