# scripts/diagnostics/ — NIM + Kimi protocol probes

Self-contained scripts for confirming or disconfirming hypotheses about the
NIM / Kimi tool-dispatch path when the daily pipeline misbehaves. They are
NOT part of the production pipeline — runs are manual, results land in
`/tmp` or this directory.

## Probes

| File | Purpose |
|------|---------|
| `probe_kimi_protocol.py` | Non-streaming HTTP call to NIM's K2.6 endpoint. Verifies the model returns structured `tool_calls` with id + name + JSON `arguments`. |
| `probe_streaming.py` | Raw SSE streaming call. Confirms K2.6 emits tool_call deltas in OpenAI-compatible shape (incremental chunks accumulating into a final populated message). |
| `probe_openai_sdk.py` | OpenAI Python SDK against NIM. Validates that SDK's `ChoiceDeltaToolCall` accumulation reaches a well-formed result. Surfaces SDK differences vs the raw HTTP and SSE paths. |
| `probe_agent_path.py` | Minimal call into the jeeves FunctionAgent path on a single sector. |
| `probe_agent_path_v2.py` | Full jeeves stack, instrumented with monkey-patches on `FunctionTool.acall` and `KimiNVIDIA.get_tool_calls_from_response`. Logs every invocation to `/tmp/jeeves_probe_v2_*.jsonl` so you can audit tool dispatches with their kwargs. |
| `run_probe.sh` | Convenience driver that runs each probe with the right env. |

## Forensic evidence

| File | What it is |
|------|------------|
| `run68-research.log` | GitHub Actions log for `daily.yml` run #68 Research job (2026-05-14 19:21-20:34 UTC). Captured 2026-05-15 via `gh run view 25880557217 --log --job 76060301198`. Motivates the circuit-breaker fix shipped in `feat/research-circuit-breakers`. |

## 2026-05-14 run #68 — sector-by-sector failure timeline

Parsed from `run68-research.log`. Job started 19:28:38 UTC, GHA cancelled at
20:34:05 UTC (65min ceiling). Job conclusion: cancelled. Write job: skipped
via `needs:` cancel-cascade.

| Sector | Start | End | Duration | Outcome |
|---|---|---|---|---|
| triadic_ontology | 19:29:12 | 19:34:23 | 5m11s | agent crashed (Request timed out.) |
| ai_systems | 19:34:23 | 19:39:32 | 5m09s | agent crashed (Request timed out.) |
| uap | 19:39:32 | 19:50:22 | 10m50s | agent crashed (Request timed out.) |
| weather | 19:50:23 | 19:56:46 | 6m23s | agent crashed (Request timed out.) |
| local_news | 19:56:46 | 20:00:48 | 4m02s | NIM 429 × all 3 retries |
| career | 20:00:48 | 20:03:49 | 3m01s | NIM 429 × all 3 retries |
| english_lesson_plans | 20:03:49 | 20:06:50 | 3m01s | NIM 429 × all 3 retries |
| family | 20:06:50 | 20:09:51 | 3m01s | NIM 429 × all 3 retries |
| global_news | 20:09:51 | 20:12:52 | 3m01s | NIM 429 × all 3 retries |
| intellectual_journals | 20:12:52 | 20:15:53 | 3m01s | NIM 429 × all 3 retries |
| wearable_ai | 20:15:53 | 20:18:54 | 3m01s | NIM 429 × all 3 retries |
| newyorker | 20:18:54 | 20:18:55 | 0m01s | **success** (direct fetch, no agent) |
| literary_pick | 20:18:55 | 20:21:56 | 3m01s | NIM 429 × all 3 retries |
| enriched_articles | 20:21:56 | 20:25:09 | 3m13s | cancelled mid-fetch (network) |

Total agent-using sectors that succeeded: **0 of 13**. Total time spent on
retry chains that all returned `spec.default`: ~50min.

## Diagnostic conclusion (2026-05-14 vs 2026-05-15)

The 2026-05-14 prior session's `HANDOFF.md` concluded NIM K2.6 was slow,
and prescribed `asyncio.wait_for(_run_one, 300s)` plus a ceiling bump.

After pulling the actual log on 2026-05-15, that prescription is wrong on
two counts:

1. K2.6 is not slow. It is producing **errors**: stream timeouts and 429s.
   Every error path already returns `spec.default` via existing handling.
2. `wait_for(300s)` would clip only uap (10m50s) and weather (6m23s) —
   savings ~7min. The 429 sectors are already under 240s each.

The real cost is the **retry budget on a confirmed-bad endpoint**:
60+120s × 9 = ~27min wasted on the rate-limit retry chain. Plus the
internal NIM stream timeouts at 5-11min each.

The fix that actually addresses this shape: **circuit breakers** that
short-circuit subsequent sectors once one sector confirms NIM is bad.
Landed in `jeeves/research_sectors.py` via PR `feat/research-circuit-breakers`.

## When to run a probe

The probes are useful when:

- A daily pipeline run produces empty briefings or crashes inconsistently.
- A NIM model rename or API change is suspected.
- New tool-dispatch warnings appear in logs without obvious root cause.
- You need to verify a hypothesis about Kimi's tool-call behaviour before
  spending a sprint on a "fix" that addresses a phantom problem (the
  2026-05-14 case study above).

Run order: `probe_kimi_protocol.py` first (cheapest, confirms protocol),
then `probe_streaming.py` (mid), then `probe_agent_path_v2.py` (heaviest,
exercises the full stack).
