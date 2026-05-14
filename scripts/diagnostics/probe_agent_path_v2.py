#!/usr/bin/env python3
"""
Probe v2: instrument KimiNVIDIA.get_tool_calls_from_response to log
EXACT state of tool_call.function.arguments at every call. Distinguishes:
  - mid-stream chunks (args="" early, populated later)
  - FINAL dispatch (args still "" when agent fires the tool)

Records every invocation to /tmp/jeeves_probe_v2_calls.jsonl with:
  - timestamp
  - tool name
  - args raw value
  - tc id
  - call ordinal in this run

Also wraps a couple of search-tool functions to log when they actually
fire and what kwargs they receive.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import time
import traceback

CALLS_LOG = "/tmp/jeeves_probe_v2_calls.jsonl"
TOOL_DISPATCHES_LOG = "/tmp/jeeves_probe_v2_dispatches.jsonl"


def _append_jsonl(path: str, rec: dict) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def main() -> int:
    sector_name = sys.argv[1] if len(sys.argv) > 1 else "local_news"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("probe_v2")

    # Reset logs
    for p in (CALLS_LOG, TOOL_DISPATCHES_LOG):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    from jeeves.config import Config
    from jeeves.llm import build_kimi_llm
    from jeeves.research_sectors import SECTOR_SPECS, _build_user_prompt
    from jeeves.tools import all_search_tools
    from jeeves.tools.quota import QuotaLedger
    from llama_index.core.agent.workflow import FunctionAgent

    spec = next((s for s in SECTOR_SPECS if s.name == sector_name), None)
    if spec is None:
        print(f"ERROR: sector {sector_name!r} not found.")
        return 4

    log.info("Loading config...")
    cfg = Config.from_env(dry_run=False)
    llm = build_kimi_llm(cfg, max_tokens=4096)
    log.info("LLM: %s", type(llm).__name__)

    # --- INSTRUMENT get_tool_calls_from_response ---
    original_method = type(llm).get_tool_calls_from_response
    call_counter = {"n": 0}

    def instrumented(self, response, error_on_no_tool_call=True):
        call_counter["n"] += 1
        n = call_counter["n"]
        tool_calls = response.message.additional_kwargs.get("tool_calls", [])
        # Snapshot the raw shape
        snapshot = []
        for tc in tool_calls:
            try:
                snapshot.append({
                    "tc_type": type(tc).__module__ + "." + type(tc).__name__,
                    "id": getattr(tc, "id", None),
                    "index": getattr(tc, "index", None),
                    "name": getattr(tc.function, "name", None) if getattr(tc, "function", None) else None,
                    "args": getattr(tc.function, "arguments", None) if getattr(tc, "function", None) else None,
                })
            except Exception as e:
                snapshot.append({"error": f"{type(e).__name__}: {e}"})
        _append_jsonl(CALLS_LOG, {
            "ts": time.time(),
            "ordinal": n,
            "n_tool_calls": len(tool_calls),
            "error_on_no_tool_call": error_on_no_tool_call,
            "delta_present": bool(response.delta),
            "content_len": len(response.message.content or ""),
            "snapshot": snapshot,
        })
        return original_method(self, response, error_on_no_tool_call)

    type(llm).get_tool_calls_from_response = instrumented
    log.info("Instrumented get_tool_calls_from_response")

    # --- BUILD TOOLS ---
    from pathlib import Path
    ledger_path = Path("/tmp/jeeves_probe_v2_quota.json")
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = QuotaLedger(ledger_path)
    prior_urls: set[str] = set()
    tools = all_search_tools(cfg=cfg, ledger=ledger, prior_urls=prior_urls)
    log.info("Tools: %d  names: %s", len(tools), [t.metadata.name for t in tools])

    # --- INSTRUMENT TOOL FUNCTIONS (via FunctionTool.call/acall) ---
    for tool in tools:
        tool_name = tool.metadata.name
        original_call = tool.call
        original_acall = tool.acall

        def make_sync_wrapper(tname, orig):
            def wrapper(*args, **kwargs):
                _append_jsonl(TOOL_DISPATCHES_LOG, {
                    "ts": time.time(),
                    "tool": tname,
                    "mode": "sync",
                    "kwargs": {k: (v[:200] if isinstance(v, str) else v) for k, v in kwargs.items()},
                    "empty_kwargs": len(kwargs) == 0 and not args,
                })
                return orig(*args, **kwargs)
            return wrapper

        def make_async_wrapper(tname, orig):
            async def wrapper(*args, **kwargs):
                _append_jsonl(TOOL_DISPATCHES_LOG, {
                    "ts": time.time(),
                    "tool": tname,
                    "mode": "async",
                    "kwargs": {k: (v[:200] if isinstance(v, str) else v) for k, v in kwargs.items()},
                    "empty_kwargs": len(kwargs) == 0 and not args,
                })
                return await orig(*args, **kwargs)
            return wrapper

        try:
            tool.call = make_sync_wrapper(tool_name, original_call)
            tool.acall = make_async_wrapper(tool_name, original_acall)
        except Exception as e:
            log.warning("Could not wrap %s: %s", tool_name, e)
    log.info("Instrumented %d tools", len(tools))

    # --- BUILD AGENT ---
    import datetime as _dt
    run_date = _dt.date.today().isoformat()
    user_prompt = _build_user_prompt(spec, run_date, [])
    log.info("user_prompt len=%d", len(user_prompt))

    agent = FunctionAgent(llm=llm, tools=tools)
    log.info("Dispatching sector %s ...", spec.name)
    t0 = time.monotonic()

    async def _run():
        # Cap with timeout so we don't sit forever
        return await asyncio.wait_for(agent.run(user_prompt), timeout=180.0)

    try:
        result = asyncio.run(_run())
        dt = time.monotonic() - t0
        log.info("AGENT COMPLETED in %.1fs", dt)
        response_text = str(getattr(result, "response", result))
        log.info("Response len: %d", len(response_text))
        log.info("Response[:600]: %r", response_text[:600])
    except asyncio.TimeoutError:
        log.error("AGENT TIMEOUT after 180s")
    except Exception as e:
        dt = time.monotonic() - t0
        log.error("AGENT RAISED after %.1fs: %s: %s", dt, type(e).__name__, e)
        traceback.print_exc()

    # --- ANALYZE LOGS ---
    print()
    print("=" * 60)
    print("INSTRUMENTATION SUMMARY")
    print("=" * 60)
    try:
        with open(CALLS_LOG) as f:
            calls = [json.loads(l) for l in f if l.strip()]
        print(f"get_tool_calls_from_response called {len(calls)} times")
        empty_args_calls = [
            c for c in calls
            if c["snapshot"] and any(
                s.get("args") in (None, "") for s in c["snapshot"]
            )
        ]
        populated_args_calls = [
            c for c in calls
            if c["snapshot"] and any(
                s.get("args") and s.get("args") not in (None, "") for s in c["snapshot"]
            )
        ]
        print(f"  with empty args: {len(empty_args_calls)}")
        print(f"  with populated args: {len(populated_args_calls)}")
        # Show last 5 calls (where final dispatch happens)
        print()
        print("Last 5 invocations:")
        for c in calls[-5:]:
            for s in c["snapshot"]:
                args_repr = s.get("args")
                if args_repr is not None and len(args_repr) > 80:
                    args_repr = args_repr[:80] + "...(truncated)"
                print(f"  [ord={c['ordinal']}] name={s.get('name')!r}  args={args_repr!r}")
    except Exception as e:
        print(f"calls log analysis failed: {e}")

    try:
        with open(TOOL_DISPATCHES_LOG) as f:
            dispatches = [json.loads(l) for l in f if l.strip()]
        print()
        print(f"Tool dispatches (actually fired): {len(dispatches)}")
        from collections import Counter
        c = Counter(d["tool"] for d in dispatches)
        for tname, cnt in c.most_common():
            print(f"  {tname}: {cnt}")
        empty_dispatch = [d for d in dispatches if d["empty_kwargs"]]
        print(f"  ...of which dispatched with EMPTY kwargs: {len(empty_dispatch)}")
        if empty_dispatch:
            print(f"  empty kwargs tools: {Counter(d['tool'] for d in empty_dispatch).most_common()}")
    except Exception as e:
        print(f"dispatch log analysis failed: {e}")

    # Quota delta
    post_state = json.loads(json.dumps(ledger._state.get("daily", {})))
    print()
    print("Quota deltas:")
    for k, v in post_state.items():
        if k == "date":
            continue
        if v:
            print(f"  {k}: +{v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
