#!/usr/bin/env python3
"""
Probe NIM kimi-k2.6 in STREAMING mode (SSE), mimicking how LlamaIndex
FunctionAgent calls the model in research-phase. Stdlib only.

Decides whether the empty-research failure is:
  (A) K2.6 streaming returns NO tool_calls (only text) -> upstream protocol
       break. Need router fallback (handoff Plan A justified).
  (B) K2.6 streaming returns tool_calls in SSE deltas  -> LlamaIndex
       normalization is the bug. ~20-line patch to jeeves/llm.py
       get_tool_calls_from_response / astream_chat_with_tools.
  (C) HTTP-layer failure (auth / timeout / rate)       -> upstream.
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
        print("ERROR: NVIDIA_API_KEY not in environment (source .env first).")
        return 1

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
        "stream": True,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
    )

    print(f"POST {BASE_URL}/chat/completions  model={MODEL}  STREAM=true")
    t0 = time.monotonic()
    accumulated_content = ""
    accumulated_tool_calls: dict[int, dict] = {}
    chunk_count = 0
    first_chunk_t = None
    last_chunk_t = None
    finish_reason = None

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            print(f"HTTP {resp.status}  reading SSE stream...")
            for raw_line in resp:
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if first_chunk_t is None:
                    first_chunk_t = time.monotonic()
                last_chunk_t = time.monotonic()
                chunk_count += 1
                choice = (chunk.get("choices") or [{}])[0]
                delta = choice.get("delta") or {}
                if "content" in delta and delta["content"]:
                    accumulated_content += delta["content"]
                if "tool_calls" in delta and delta["tool_calls"]:
                    for tc_delta in delta["tool_calls"]:
                        idx = tc_delta.get("index", 0)
                        slot = accumulated_tool_calls.setdefault(
                            idx, {"id": None, "name": None, "arguments": ""}
                        )
                        if tc_delta.get("id"):
                            slot["id"] = tc_delta["id"]
                        fn = tc_delta.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if "arguments" in fn and fn["arguments"]:
                            slot["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:800] if hasattr(e, "read") else ""
        dt = time.monotonic() - t0
        print(f"HTTP {e.code} after {dt*1000:.0f}ms")
        print(f"BODY: {body}")
        if e.code in (401, 403):
            print("DECISION: (C) auth failure -- fix NVIDIA_API_KEY")
        elif e.code in (429, 503):
            print("DECISION: (C) rate-limit / service unavailable")
        elif e.code == 404:
            print("DECISION: (C) endpoint / model not hosted")
        else:
            print(f"DECISION: (C) HTTP {e.code}")
        return 0
    except urllib.error.URLError as e:
        print(f"NETWORK FAIL: {e}")
        print("DECISION: (C) network / DNS")
        return 0

    dt = time.monotonic() - t0
    ttfc = (first_chunk_t - t0) * 1000 if first_chunk_t else 0
    stream_dur = (last_chunk_t - first_chunk_t) * 1000 if first_chunk_t and last_chunk_t else 0
    print()
    print(f"chunks={chunk_count}  total={dt*1000:.0f}ms  ttfc={ttfc:.0f}ms  stream_window={stream_dur:.0f}ms")
    print(f"finish_reason={finish_reason}")
    print(f"content accumulated: len={len(accumulated_content)}")
    print(f"tool_calls accumulated: count={len(accumulated_tool_calls)}")
    print()

    if accumulated_tool_calls:
        print("STREAMING tool_calls:")
        for idx in sorted(accumulated_tool_calls.keys()):
            slot = accumulated_tool_calls[idx]
            print(f"  [{idx}] id={slot['id']!r} name={slot['name']!r} args={slot['arguments']!r}")
        print()
    if accumulated_content:
        print(f"content[:400]: {accumulated_content[:400]!r}")
        print()

    text_form_hits = re.findall(
        r"functions\.\w+:\d+|<\|tool_call_\w+\|>|^tool_call:", accumulated_content, re.M
    )

    print("=" * 60)
    if accumulated_tool_calls:
        print("DECISION: (B) STREAMING tool_calls DO arrive in SSE deltas.")
        print("  Protocol-layer is fine. Empty-research is LlamaIndex normalization.")
        print()
        print("  Specifically: research uses FunctionAgent.astream_chat_with_tools,")
        print("  which streams chunks. KimiNVIDIA must accumulate the tool_call")
        print("  deltas into response.message.additional_kwargs['tool_calls'] for")
        print("  get_tool_calls_from_response to see them. The accumulator path")
        print("  appears broken for K2.6's tool_call ID shape OR for the new")
        print("  is_function_calling_model=True branch.")
        print()
        print("  NEXT STEP: read jeeves/llm.py + llama-index version pin, locate")
        print("  the streaming tool_call accumulator, identify which level eats")
        print("  the K2.6 IDs. ~20 LOC patch likely sufficient.")
    elif text_form_hits:
        print(f"DECISION: (A) STREAMING emits text-form tool calls (no SSE deltas).")
        print(f"  Hits: {text_form_hits[:3]}")
        print("  Cheap fix: regex-parse content in get_tool_calls_from_response.")
    elif accumulated_content.strip():
        print("DECISION: (A') Streaming returned prose. Model ignored tool registration.")
        print("  Could be: NIM streaming endpoint not honouring tool_choice='auto'.")
        print("  Either patch prompt aggression OR build router with Cerebras fallback.")
    else:
        print("DECISION: (C) empty streaming response. Investigate NIM.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
