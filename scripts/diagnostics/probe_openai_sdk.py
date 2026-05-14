#!/usr/bin/env python3
"""
Probe NIM kimi-k2.6 via the OpenAI Python SDK with streaming + tools.

This is what LlamaIndex's OpenAILike does internally. If the OpenAI SDK
parses NIM's streaming tool_calls correctly, the bug is downstream in
LlamaIndex or jeeves. If the SDK can't parse them, the bug is at the
SDK <-> NIM interface.
"""
from __future__ import annotations
import json
import os
import sys
from openai import OpenAI

BASE_URL = "https://integrate.api.nvidia.com/v1"
MODEL = "moonshotai/kimi-k2.6"
TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for a query.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}


def main() -> int:
    api_key = os.environ.get("NVIDIA_API_KEY")
    if not api_key:
        print("ERROR: NVIDIA_API_KEY not set")
        return 1
    client = OpenAI(api_key=api_key, base_url=BASE_URL)

    print(f"Calling {MODEL} with stream=True + tools=[web_search]...")
    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": "Always call web_search before answering factual questions."},
            {"role": "user", "content": "What were top Reuters headlines on 2026-05-13?"},
        ],
        tools=[TOOL],
        tool_choice="auto",
        stream=True,
        max_tokens=256,
        temperature=0.2,
    )

    # Replicate LlamaIndex update_tool_calls logic to verify parity
    accumulated_tool_calls: list = []
    chunk_count = 0
    is_function = False
    content_total = ""
    finish_reason = None
    first_chunk_with_tools = None

    for chunk in stream:
        chunk_count += 1
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta

        if delta and delta.tool_calls:
            if first_chunk_with_tools is None:
                first_chunk_with_tools = chunk_count
            is_function = True
            tc_delta_list = delta.tool_calls
            # update_tool_calls equivalent
            for tc_delta in tc_delta_list:
                if not accumulated_tool_calls:
                    accumulated_tool_calls.append(tc_delta)
                else:
                    last = accumulated_tool_calls[-1]
                    if last.index != tc_delta.index:
                        accumulated_tool_calls.append(tc_delta)
                    else:
                        if last.function is None or tc_delta.function is None:
                            continue
                        if last.function.arguments is None:
                            last.function.arguments = ""
                        if last.function.name is None:
                            last.function.name = ""
                        if last.id is None:
                            last.id = ""
                        last.function.arguments += tc_delta.function.arguments or ""
                        last.function.name += tc_delta.function.name or ""
                        last.id += tc_delta.id or ""

        if delta and delta.content:
            content_total += delta.content

        if choice.finish_reason:
            finish_reason = choice.finish_reason

    print(f"chunks={chunk_count}  is_function_triggered={is_function}  finish_reason={finish_reason}")
    print(f"first chunk with tool_calls: #{first_chunk_with_tools}")
    print(f"content: {content_total!r}")
    print()
    print(f"accumulated_tool_calls: count={len(accumulated_tool_calls)}")
    for i, tc in enumerate(accumulated_tool_calls):
        print(f"  [{i}] type(tc)={type(tc).__module__}.{type(tc).__name__}")
        print(f"       index={tc.index!r}")
        print(f"       id={tc.id!r}")
        print(f"       type={tc.type!r}")
        if tc.function:
            print(f"       function.name={tc.function.name!r}")
            print(f"       function.arguments={tc.function.arguments!r}")
            # Verify arguments are valid JSON
            try:
                parsed = json.loads(tc.function.arguments) if tc.function.arguments else None
                print(f"       json.loads(args) = {parsed!r}")
            except Exception as e:
                print(f"       JSON PARSE ERROR: {e}")
        else:
            print("       function=None  <-- problem")

    print()
    print("=" * 60)
    if not accumulated_tool_calls:
        print("DECISION: OpenAI SDK got no tool_calls. NIM streaming differs from spec.")
        return 0
    tc = accumulated_tool_calls[0]
    ok_id = bool(tc.id)
    ok_name = bool(tc.function and tc.function.name)
    ok_args = False
    if tc.function and tc.function.arguments:
        try:
            json.loads(tc.function.arguments)
            ok_args = True
        except Exception:
            pass
    print(f"id present: {ok_id}    name present: {ok_name}    args parse: {ok_args}")
    if ok_id and ok_name and ok_args:
        print("DECISION: OpenAI SDK correctly accumulates K2.6 tool_calls.")
        print("  LlamaIndex normalizer SHOULD see the same data.")
        print("  Empty-research bug must be in: FunctionAgent loop / tool dispatch /")
        print("  jeeves quota guard / jeeves tool wrappers.")
        print()
        print("  Next: run the actual jeeves FunctionAgent path locally to observe.")
    else:
        print("DECISION: SDK parses partial data. Missing field is the bug-source.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
