#!/usr/bin/env python3
"""
Probe the FULL agent path used by research-phase, against ONE sector.

Replicates: build_kimi_llm() -> FunctionAgent(tools=all_search_tools) ->
agent.run(spec.instruction). Same code path as production research, just for
one sector instead of all 11. Captures:
  - did the agent dispatch any tool calls?
  - did normalization succeed?
  - did search providers actually return results?
  - did agent converge to a useful response?

Decision tree:
  (1) Tools dispatched + provider hits + non-empty response -> agent path works.
      The 2026-05-13 failure was sector-specific or rate-limited.
  (2) Zero tool calls dispatched                            -> FunctionAgent
      not entering tool-call loop. Streaming/protocol bug, NOT model_router.
  (3) Tool calls dispatched but provider errors             -> upstream API.
  (4) Normalization exception                               -> patch llm.py.

Usage (run from repo root, .venv activated):
  source .venv/bin/activate
  NVIDIA_API_KEY=nvapi-... SERPER_API_KEY=... python3 probe_agent_path.py

Or set the variables before running. Optional sector arg:
  python3 probe_agent_path.py local_news

Default sector: ai_systems (deep sector, exercises max_tokens=4096 path).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import sys
import time
import traceback


def main() -> int:
    sector_name = sys.argv[1] if len(sys.argv) > 1 else "ai_systems"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # Quiet the noisy ones, keep jeeves visible.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    log = logging.getLogger("probe_agent")

    # Verify secrets up front so we fail fast.
    required = ["NVIDIA_API_KEY", "SERPER_API_KEY"]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f"ERROR: missing env vars: {missing}")
        print("At minimum set NVIDIA_API_KEY + SERPER_API_KEY. Tavily/Exa/Gemini optional.")
        return 2

    try:
        from jeeves.config import Config
        from jeeves.llm import build_kimi_llm
        from jeeves.research_sectors import SECTOR_SPECS, _build_user_prompt
        from jeeves.tools import all_search_tools
        from jeeves.tools.quota import QuotaLedger
        from llama_index.core.agent.workflow import FunctionAgent
    except ImportError as e:
        print(f"ERROR: jeeves import failed: {e}")
        print("Run with .venv activated:  source .venv/bin/activate")
        return 3

    spec = next((s for s in SECTOR_SPECS if s.name == sector_name), None)
    if spec is None:
        print(f"ERROR: sector {sector_name!r} not found.")
        print(f"Available: {[s.name for s in SECTOR_SPECS]}")
        return 4

    log.info("Loading config + building Kimi LLM...")
    cfg = Config.from_env(dry_run=False)
    llm = build_kimi_llm(cfg, max_tokens=4096)
    log.info("LLM ready: %s", type(llm).__name__)

    log.info("Building search tools (with fresh quota ledger)...")
    from pathlib import Path
    # Use a probe-only ledger file so production .quota-state.json untouched.
    ledger_path = Path("/tmp/jeeves_probe_quota.json")
    if ledger_path.exists():
        ledger_path.unlink()
    ledger = QuotaLedger(ledger_path)
    pre_state = json.loads(json.dumps(ledger._state.get("daily", {})))
    prior_urls: set[str] = set()
    tools = all_search_tools(cfg=cfg, ledger=ledger, prior_urls=prior_urls)
    log.info("Tool count: %d  names: %s", len(tools), [t.metadata.name for t in tools][:8])

    # Mirror research_sectors._run_sector_loop user prompt building.
    import datetime as _dt
    run_date = _dt.date.today().isoformat()
    user_prompt = _build_user_prompt(spec, run_date, [])
    log.info("user_prompt len=%d", len(user_prompt))

    agent = FunctionAgent(llm=llm, tools=tools)
    log.info("Agent ready. Dispatching sector %s ...", spec.name)
    t0 = time.monotonic()

    async def _run():
        return await agent.run(user_prompt)

    try:
        result = asyncio.run(_run())
    except Exception as e:
        dt = time.monotonic() - t0
        log.error("AGENT RAISED after %.1fs: %s: %s", dt, type(e).__name__, e)
        traceback.print_exc()
        post_state = json.loads(json.dumps(ledger._state.get("daily", {})))
        print()
        print("Quota delta during failed run:")
        for k, v in post_state.items():
            if k == "date":
                continue
            delta = v - pre_state.get(k, 0)
            if delta:
                print(f"  {k:25s} +{delta}")
        if str(e).startswith("Expected at least one tool call"):
            print()
            print("DECISION: (4) normalizer rejected response with empty tool_calls.")
            print("  Kimi returned a message but additional_kwargs.tool_calls was empty.")
            print("  Either the streaming path doesn't fill tool_calls, or this is the")
            print("  agent terminating after final response (which is normal).")
        else:
            print("DECISION: (5) unexpected exception. See traceback above.")
        return 0
    dt = time.monotonic() - t0
    log.info("AGENT COMPLETED in %.1fs", dt)

    print()
    print(f"Result type: {type(result).__name__}")
    response_text = str(getattr(result, "response", result))
    print(f"Response text len: {len(response_text)}")
    print(f"Response text[:600]: {response_text[:600]!r}")

    post_state = json.loads(json.dumps(ledger._state.get("daily", {})))
    print()
    print("Quota delta during run:")
    any_increment = False
    for k, v in post_state.items():
        if k == "date":
            continue
        delta = v - pre_state.get(k, 0)
        if delta:
            print(f"  {k:25s} +{delta}")
            any_increment = True
    if not any_increment:
        print("  (zero tool calls landed in any provider)")

    print()
    print("=" * 60)
    if not any_increment:
        print("DECISION: (2) FunctionAgent did NOT dispatch any tools.")
        print("  Despite Kimi K2.6 protocol working in isolation (probe 1),")
        print("  the agent loop never invokes them in production code path.")
        print()
        print("  Likely causes:")
        print("    - llama-index FunctionAgent streaming + Kimi tool_calls shape mismatch")
        print("    - get_tool_calls_from_response signature broken under new flags")
        print("    - astream_chat_with_tools normalization eats tool_calls")
        print()
        print("  Next step: turn on DEBUG logging in jeeves.llm, run again, inspect")
        print("  the actual tool_calls shape in the response message.")
    elif any_increment and len(response_text) > 200:
        print("DECISION: (1) AGENT PATH WORKS. 2026-05-13 was sector-specific or transient.")
        print("  Trigger a fresh daily.yml run to confirm. Stop chasing protocol theory.")
    elif any_increment and len(response_text) <= 200:
        print("DECISION: (3) Tools called but agent emitted no findings. Investigate")
        print("  whether providers returned errors or empty results.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
