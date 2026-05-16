"""Tool-dispatch eval harness — compares K2.6 (NIM) vs Cerebras.

Sends a research-like prompt with tool definitions to each provider and
checks whether the model returns tool calls with valid (non-empty)
arguments. The core diagnostic for NIM stream truncation: K2.6 returns
tool_call.function.arguments as null/empty, while a working model should
return parseable JSON with a ``query`` field.

Usage::

    # Dry-run (no network, fixture responses):
    PYTHONPATH=. python scripts/eval_tool_dispatch.py --dry-run

    # Live against both providers:
    PYTHONPATH=. python scripts/eval_tool_dispatch.py

    # Live against Cerebras only:
    PYTHONPATH=. python scripts/eval_tool_dispatch.py --providers cerebras

Requires:
    NVIDIA_API_KEY  — for K2.6 on NIM (skipped if absent)
    CEREBRAS_API_KEY — for Cerebras (skipped if absent)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "serper_search",
            "description": (
                "Google SERP via Serper.dev. Args: query (str, required), "
                "num (int, default 10), tbs (str|None, e.g. 'qdr:d')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num": {"type": "integer", "default": 10},
                    "tbs": {"type": "string", "description": "Time filter"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exa_search",
            "description": (
                "Exa neural semantic search. Args: query (str, required), "
                "num_results (int, default 10), category (str|None)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {"type": "integer", "default": 10},
                    "category": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
]

# Test prompts — each should trigger at least one tool call with a query arg
TEST_CASES = [
    {
        "name": "local_news",
        "system": "You are a research agent. Call a search tool to find results.",
        "user": (
            "Find the latest local news for Edmonds, Washington. "
            "Call serper_search with an appropriate query."
        ),
        "expected_tool": "serper_search",
        "expected_arg": "query",
    },
    {
        "name": "intellectual_journals",
        "system": "You are a research agent. Call a search tool to find results.",
        "user": (
            "Find recent academic papers on triadic ontology published in 2025-2026. "
            "Call exa_search with an appropriate query."
        ),
        "expected_tool": "exa_search",
        "expected_arg": "query",
    },
    {
        "name": "multi_tool",
        "system": "You are a research agent. Call search tools to find results.",
        "user": (
            "Find breaking AI news from the last 24 hours. "
            "Use serper_search with tbs='qdr:d' for time-filtered results."
        ),
        "expected_tool": "serper_search",
        "expected_arg": "query",
    },
]

# ---------------------------------------------------------------------------
# Provider configs
# ---------------------------------------------------------------------------

PROVIDERS = {
    "nim_k26": {
        "base_url": "https://integrate.api.nvidia.com/v1",
        "model": "moonshotai/kimi-k2.5",
        "env_key": "NVIDIA_API_KEY",
        "fallback_models": [
            "moonshotai/kimi-k2.5",
            "moonshotai/kimi-k2.5-instruct",
            "moonshotai/kimi-k2-instruct",
        ],
    },
    "cerebras": {
        "base_url": "https://api.cerebras.ai/v1",
        "model": "llama-3.3-70b",
        "env_key": "CEREBRAS_API_KEY",
        "fallback_models": [
            "llama-3.3-70b",
            "llama3.1-70b",
            "llama3.1-8b",
        ],
    },
}


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    provider: str
    case_name: str
    ok: bool
    tool_name: str | None = None
    has_args: bool = False
    has_query: bool = False
    args_raw: str | None = None
    latency_ms: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Dry-run fixture
# ---------------------------------------------------------------------------

_DRY_RUN_RESPONSE = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_dry_001",
                        "type": "function",
                        "function": {
                            "name": "serper_search",
                            "arguments": json.dumps(
                                {"query": "Edmonds WA news today", "num": 10}
                            ),
                        },
                    }
                ],
            }
        }
    ]
}

_DRY_RUN_RESPONSE_EXA = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_dry_002",
                        "type": "function",
                        "function": {
                            "name": "exa_search",
                            "arguments": json.dumps(
                                {"query": "triadic ontology 2025 2026", "num_results": 10}
                            ),
                        },
                    }
                ],
            }
        }
    ]
}


def _dry_run_dispatch(case: dict) -> dict:
    """Return a fixture response matching the expected tool."""
    if case["expected_tool"] == "exa_search":
        return _DRY_RUN_RESPONSE_EXA
    return _DRY_RUN_RESPONSE


# ---------------------------------------------------------------------------
# Live dispatch
# ---------------------------------------------------------------------------


def _live_dispatch(
    provider_cfg: dict, case: dict, api_key: str
) -> dict:
    """Call the provider's chat completions endpoint with tool definitions."""
    import httpx

    messages = [
        {"role": "system", "content": case["system"]},
        {"role": "user", "content": case["user"]},
    ]

    # Try each model in the fallback chain
    last_error = None
    for model_id in provider_cfg["fallback_models"]:
        try:
            resp = httpx.post(
                f"{provider_cfg['base_url']}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model_id,
                    "messages": messages,
                    "tools": TOOLS,
                    "tool_choice": "auto",
                    "max_tokens": 512,
                    "temperature": 0.1,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.json()
            last_error = f"HTTP {resp.status_code}: {resp.text[:200]}"
        except Exception as e:
            last_error = str(e)[:200]

    return {"error": last_error}


# ---------------------------------------------------------------------------
# Evaluate a single response
# ---------------------------------------------------------------------------


def _evaluate_response(
    provider: str, case: dict, response: dict, latency_ms: int
) -> DispatchResult:
    """Extract tool call info from the response and evaluate."""
    if "error" in response and response["error"]:
        return DispatchResult(
            provider=provider,
            case_name=case["name"],
            ok=False,
            error=response["error"],
            latency_ms=latency_ms,
        )

    choices = response.get("choices", [])
    if not choices:
        return DispatchResult(
            provider=provider,
            case_name=case["name"],
            ok=False,
            error="no choices in response",
            latency_ms=latency_ms,
        )

    message = choices[0].get("message", {})
    tool_calls = message.get("tool_calls") or []

    if not tool_calls:
        # Model responded with text instead of tool calls
        content = (message.get("content") or "")[:100]
        return DispatchResult(
            provider=provider,
            case_name=case["name"],
            ok=False,
            error=f"no tool_calls (text: {content}...)",
            latency_ms=latency_ms,
        )

    # Check first tool call
    tc = tool_calls[0]
    fn = tc.get("function", {})
    tool_name = fn.get("name")
    args_raw = fn.get("arguments")

    has_args = bool(args_raw and args_raw.strip() and args_raw.strip() != "{}")
    has_query = False

    if has_args:
        try:
            parsed = json.loads(args_raw)
            has_query = bool(parsed.get(case["expected_arg"]))
        except (json.JSONDecodeError, TypeError):
            pass

    return DispatchResult(
        provider=provider,
        case_name=case["name"],
        ok=has_query,
        tool_name=tool_name,
        has_args=has_args,
        has_query=has_query,
        args_raw=(args_raw or "")[:200],
        latency_ms=latency_ms,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Tool-dispatch eval: K2.6 vs Cerebras")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Use fixture responses instead of live API calls",
    )
    parser.add_argument(
        "--providers",
        default="nim_k26,cerebras",
        help="Comma-separated provider names (default: nim_k26,cerebras)",
    )
    args = parser.parse_args()

    provider_names = [p.strip() for p in args.providers.split(",")]
    results: list[DispatchResult] = []

    for pname in provider_names:
        cfg = PROVIDERS.get(pname)
        if not cfg:
            print(f"[WARN] Unknown provider: {pname}, skipping")
            continue

        api_key = os.environ.get(cfg["env_key"], "")
        if not api_key and not args.dry_run:
            print(f"[SKIP] {pname}: {cfg['env_key']} not set")
            for case in TEST_CASES:
                results.append(
                    DispatchResult(
                        provider=pname,
                        case_name=case["name"],
                        ok=False,
                        error=f"{cfg['env_key']} not set",
                    )
                )
            continue

        for case in TEST_CASES:
            t0 = time.monotonic()
            if args.dry_run:
                response = _dry_run_dispatch(case)
            else:
                response = _live_dispatch(cfg, case, api_key)
            latency_ms = int((time.monotonic() - t0) * 1000)

            result = _evaluate_response(pname, case, response, latency_ms)
            results.append(result)

    # Print results table
    print()
    print("=" * 90)
    print(f"{'Provider':<12} {'Case':<25} {'OK':<5} {'Tool':<18} {'Args':<6} {'Query':<6} {'ms':<8} {'Error'}")
    print("-" * 90)
    for r in results:
        print(
            f"{r.provider:<12} {r.case_name:<25} "
            f"{'PASS' if r.ok else 'FAIL':<5} "
            f"{(r.tool_name or '-'):<18} "
            f"{'Y' if r.has_args else 'N':<6} "
            f"{'Y' if r.has_query else 'N':<6} "
            f"{r.latency_ms:<8} "
            f"{(r.error or '')[:40]}"
        )
    print("=" * 90)

    # Summary per provider
    print()
    print("SUMMARY:")
    for pname in provider_names:
        provider_results = [r for r in results if r.provider == pname]
        if not provider_results:
            continue
        total = len(provider_results)
        passed = sum(1 for r in provider_results if r.ok)
        dispatch_rate = passed / total if total else 0
        avg_latency = (
            sum(r.latency_ms for r in provider_results) / total if total else 0
        )
        status = "PASS" if dispatch_rate >= 0.67 else "FAIL"
        print(
            f"  {pname}: {passed}/{total} dispatched "
            f"({dispatch_rate:.0%}) | avg {avg_latency:.0f}ms | {status}"
        )

    print()

    # Exit code: 0 if at least one provider passes ≥67% of cases
    any_pass = any(
        sum(1 for r in results if r.provider == pname and r.ok)
        / max(1, sum(1 for r in results if r.provider == pname))
        >= 0.67
        for pname in provider_names
    )
    sys.exit(0 if any_pass else 1)


if __name__ == "__main__":
    main()
