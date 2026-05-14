#!/usr/bin/env python3
"""
Probe NIM kimi-k2.6 tool-call protocol shape. Stdlib-only (no httpx).

Decides whether the 2026-05-13 empty-research failure is:
  (A) K2.6 emits tool calls as text (handoff theory) -> patch normalizer
  (B) K2.6 protocol fine, root cause elsewhere       -> ignore handoff
  (B') Model ignores tool registration, answers prose -> router justified
  (C) NIM auth/rate-limit/endpoint                   -> fix upstream

Usage:
  NVIDIA_API_KEY=nvapi-... python3 probe_kimi_protocol.py

No dependencies. Pure stdlib.
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL = "moonshotai/kimi-k2.6"
TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for a query. Use this for any factual question.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query"},
            },
            "required": ["query"],
        },
    },
}
SYSTEM = (
    "You are a research assistant. ALWAYS call web_search before answering "
    "any factual question. Do not answer from memory."
)
USER = "What were the top three Reuters world-news headlines on 2026-05-13?"


def main() -> int:
    api_key = os.environ.get("NVIDIA_API_KEY") or os.environ.get("NVIDIA_NIM_API_KEY")
    if not api_key:
        print("ERROR: set NVIDIA_API_KEY in environment (no $ prefix in shell).")
        print("Example: NVIDIA_API_KEY=nvapi-NH... python3 probe_kimi_protocol.py")
        return 0

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        "tools": [TOOL],
        "tool_choice": "auto",
        "max_tokens": 512,
        "temperature": 0.2,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    print(f"POST {BASE_URL}/chat/completions  model={MODEL}")
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            status = resp.status
            raw = resp.read()
    except urllib.error.HTTPError as e:
        status = e.code
        raw = e.read() if hasattr(e, "read") else b""
        dt = time.monotonic() - t0
        print(f"HTTP {status}  latency={dt*1000:.0f}ms")
        body_str = raw.decode("utf-8", errors="replace")[:800]
        print(f"BODY: {body_str}")
        if status in (401, 403):
            print("DECISION: (C) auth -- fix NVIDIA_API_KEY")
        elif status in (429, 503):
            print("DECISION: (C) rate-limit or service-unavailable")
        elif status == 404:
            print("DECISION: (C) model/endpoint not found -- NIM rotated again")
        else:
            print(f"DECISION: (C) HTTP {status} -- investigate body")
        return 0
    except urllib.error.URLError as e:
        print(f"NETWORK FAIL: {type(e).__name__}: {e}")
        print("DECISION: (C) upstream -- network or DNS issue")
        return 0
    except Exception as e:
        print(f"UNEXPECTED FAIL: {type(e).__name__}: {e}")
        print("DECISION: investigate")
        return 0

    dt = time.monotonic() - t0
    print(f"HTTP {status}  latency={dt*1000:.0f}ms")

    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:
        print(f"JSON PARSE FAIL: {e}")
        print(f"RAW[:800]: {raw[:800]!r}")
        return 0

    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    finish = choice.get("finish_reason")

    print(f"finish_reason={finish}  tool_calls_count={len(tool_calls)}  content_len={len(content)}")
    print()
    if tool_calls:
        print("STRUCTURED tool_calls:")
        for tc in tool_calls[:3]:
            fn = tc.get("function", {})
            print(f"  id={tc.get('id')} name={fn.get('name')} args={fn.get('arguments')!r}")
        print()
    if content:
        print(f"content[:500]: {content[:500]!r}")
        print()

    text_form_pattern = re.compile(
        r"functions\.\w+:\d+|<\|tool_call_\w+\|>|^tool_call:", re.M
    )
    text_form_hits = text_form_pattern.findall(content)

    print("=" * 60)
    if tool_calls:
        print("DECISION: (B) protocol fine -- structured tool_calls returned.")
        print("  Handoff's K2.6-emits-text theory is WRONG.")
        print("  Empty-research root cause is elsewhere. Check:")
        print("    - NIM rate limits (per-key TPM/RPD)")
        print("    - FunctionAgent loop budget exhaustion")
        print("    - quota-guard rejecting sectors that DID call tools")
        print("  DO NOT build model_router until root cause identified.")
    elif text_form_hits:
        print(f"DECISION: (A) text-form tool calls confirmed. Hits: {text_form_hits[:3]}")
        print("  Handoff's theory is CORRECT.")
        print("  CHEAP FIX (~30 LOC): patch jeeves/llm.py::get_tool_calls_from_response")
        print("  to regex-parse content when additional_kwargs.tool_calls is empty,")
        print("  synthesize ToolSelection objects, return them.")
        print("  Router rebuild is OPTIONAL (defense-in-depth, not required).")
    elif not content.strip():
        print("DECISION: (C) empty response -- NIM returned no content and no tools.")
        print(f"  finish_reason={finish}")
        print("  Investigate NIM-side behaviour for kimi-k2.6 with tool_choice=auto.")
    else:
        print("DECISION: (B') model answered from training, did NOT call tool.")
        print("  No structured tool_calls, no text-form tool-call syntax, just prose.")
        print("  Model is ignoring tool registration. Causes:")
        print("    - is_function_calling_model flag not honoured by NIM endpoint")
        print("    - K2.6 instruction-following degraded vs K2-instruct-0905")
        print("    - System prompt insufficient for tool-call insistence")
        print("  This DOES motivate model_router build (Cerebras/Gemini fallback).")
        print("  Also harden system prompt -- 'ALWAYS call X' may not be strong enough.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
