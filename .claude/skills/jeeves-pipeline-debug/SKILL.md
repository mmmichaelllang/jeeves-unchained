---
name: jeeves-pipeline-debug
description: Use when diagnosing a failed or degraded jeeves-unchained pipeline run, reading GitHub Actions logs, or triaging missing output sections. Covers LOG-TELLS, NIM gotchas, Groq TPM math, quota-state.json, phase failure diagnosis, and the fetch-chain fallback order.
metadata:
  triggers: pipeline failed, empty email, missing section, actions log, NIM refine absent, openrouter absent, 429, quota, briefing degraded, session json, correspondence failed
---

# Jeeves Pipeline Debug Guide

## Phase Identification (read the Actions log top-down)

| Log line present | Phase | Status |
|---|---|---|
| `"(DRY RUN)"` in h1 | correspondence | dry-run path taken (expected if --dry-run) |
| `NIM refine [partN]` lines | write | NIM refine running normally |
| `NIM refine [partN]` lines ABSENT | write | NVIDIA_API_KEY missing OR JEEVES_SKIP_NIM_REFINE=1 |
| `OpenRouter narrative edit` line | write | OpenRouter final pass running |
| `OpenRouter narrative edit` ABSENT | write | OPENROUTER_API_KEY not in secrets |
| `WARNING placeholder not found` | write | newyorker text missing (no hallucination — just absent) |
| `sector newyorker: direct Python fetch` | research | normal New Yorker fetch path |

## LOG-TELLS (what each absent log line means)

- **"NIM refine [partN]" absent** → `NVIDIA_API_KEY` not set in repo secrets, or `JEEVES_SKIP_NIM_REFINE=1`
- **"NIM refine exhausted retries"** → NIM free tier 429 loop; check `.quota-state.json`
- **"OpenRouter narrative edit" absent** → `OPENROUTER_API_KEY` not set
- **"sector triadic_ontology: forced-retry"** → expected; Kimi uses training data for this sector (~10s overhead)
- **"sector uap: 60s sleep"** → NIM 429 backoff; expected behavior, within 15-min window
- **"quota guard: no search provider called"** → sector rejected for hallucination; check ledger
- **"correspondence handoff failed schema validation"** → Phase 1→2 contract drift; check CorrespondenceHandoff model

## NIM Gotchas (research phase)

| Symptom | Cause | Fix |
|---|---|---|
| 400 "Extra data: line 1 col 3" | `ToolCallBlock.tool_kwargs={}` (empty dict) | `_normalize_tool_kwargs` converts `{}`→`"{}"` — check if it ran |
| 400 "Input should be valid string" | `function.arguments=None` or `id=None` | `get_tool_calls_from_response` strips degenerate entries |
| Sector crashes immediately | `achat_with_tools` called instead of `astream_chat_with_tools` | FunctionAgent always uses streaming=True; dead code path |
| triadic_ontology always 429 | Kimi rate limit on training-data sectors | Expected; 60/120s backoff is correct |

## Groq TPM Math

- Free tier: 12,000 TPM on `llama-3.3-70b-versatile`
- Typical system prompt: ~7,700 tokens by Part 4
- Available for output: `12,000 - input_tokens - 600` (clamped by `_clamp_groq_max_tokens`)
- Daily limit: 100k tokens/day → pipeline spends ~82k (73k write + 9k correspondence)
- **Hard limit:** `max_tokens ≤ 5,000` or pipeline fails at Part 2 (falls to NIM)
- 65s sleep between Groq calls (rolling 60s TPM window)

## Quota State

Location: `.quota-state.json` in repo root (committed monthly, reset each calendar month)

Keys: `serper`, `tavily_search`, `tavily_extract`, `gemini_grounded`, `exa`, `playwright`

If a provider shows at/near cap → `cheapest_with_capacity()` skips it silently.

Monthly caps (free tiers):
- gemini_grounded: 12/day hard cap (set in code; real RPD=20 but conservative)
- playwright: 5/run (slow ~5-15s each; last resort)

## Fetch-Chain Fallback Order (research phase, per article)

1. `httpx + trafilatura` — fastest, text-only
2. `Jina (r.jina.ai)` — markdown, handles many paywalls
3. `playwright_extractor` — headless Chromium; only when Jina len<300 OR paywall markers
   - `newyorker` skips Jina entirely (JS-heavy) → goes straight to Playwright
   - If `success=false` → Playwright not installed; pick another URL, do not retry

## Session File Locations

- Session JSON: `sessions/session-YYYY-MM-DD.json` (committed to main)
- Briefing HTML: `sessions/briefing-YYYY-MM-DD.html` (committed)
- Correspondence JSON: `sessions/correspondence-YYYY-MM-DD.json`
- Dry-run outputs: `sessions/*.local.json` / `sessions/*.local.html` (gitignored)

## Common Failure Modes

**Empty sector in output:**
1. Check quota ledger — provider may be exhausted
2. Check "quota guard: no search provider called" in logs → sector hallucination guard tripped
3. Check `_empty_reason` field in session JSON if present

**Groq TPD exhausted mid-run:**
- Symptoms: Part 2+ falls to NIM write path; logs show "Groq TPD exhausted"
- Fix: wait for midnight UTC reset, or reduce max_tokens

**newyorker section missing:**
- Check if `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->` was injected
- Check if `_inject_newyorker_verbatim` ran (log: "injecting newyorker verbatim")
- Model never copies New Yorker text directly — placeholder injection is the guarantee

**Briefing under 5,000 words:**
- Likely a thin-sector or dedup overfire situation
- Check `DEDUP_PROMPT_HEADLINES_CAP` (250) — if covered_headlines is truncated, Groq sees stale dedup state

## Skip Flags

- `JEEVES_SKIP_NIM_REFINE=1` — skip NIM quality-editor pass (faster runs, lower quality)
- `--dry-run` (scripts/*.py) — uses fixture data; writes .local.* files only
- `--use-fixture` — loads saved session JSON instead of running research
