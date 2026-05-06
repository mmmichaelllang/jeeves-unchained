# Handoff — 2026-05-03 — Review Sprint + P0 Fix

## Branch
`fix/literary-pick-dedup-quality-filter`

## Commits
- `1da43a5` — quality sprint (33 files, ~3500 lines)
- `d1ed54d` — P0/P1 fixes (write.py, llm.py, scripts/write.py, test_write_postprocess.py)

Both pushed to GitHub.

## What Was Done

### P0 Fixed (would NameError every production write run)
`postprocess_html` referenced variables (`quality_warnings`, `cfg`, `groq_part_count`, `nim_fallback_part_count`) that only exist inside `generate_briefing`. Fixed by:
1. `generate_briefing` now returns 4-tuple: `(html, quality_warnings, groq_parts, nim_parts)`
2. `postprocess_html` now accepts `quality_warnings: list[str] | None = None` kwarg
3. `_write_run_manifest` moved to `scripts/write.py` (where `cfg` is available)

### P1 Fixed
- `_re` NameError in `_system_prompt_for_parts` (was using `import re as _re` inside function, removed the import, forgot to rename references → fixed to module-level `re`)
- `build_groq_llm` uses `cfg.groq_api_key or _ensure_groq_key()` (consistent with other builders)
- `RunManifest.from_briefing_result` no longer hardcodes `9` for total_parts
- `_write_run_manifest` dead `suffix` variable fixed

### Tests
368 passing, 37 failing. All 37 failures are pre-existing (`llama_index` not installed in bash sandbox). No regressions.

## Immediate Next Steps

1. **Test the fix**: Run `uv run python scripts/write.py --use-fixture --skip-send` locally or trigger `write.yml` workflow on this branch.

2. **If passing**: Merge `fix/literary-pick-dedup-quality-filter` → `main`, then update `CLAUDE.md`:
   ```
   branch: main | sprint: 14 (review) | ...
   ```
   Add to `<state>`: `P0-fix: postprocess_html quality_warnings kwarg | generate_briefing returns 4-tuple`

3. **P2 work** (not yet done, lower priority):
   - `PART1_INSTRUCTIONS` hardcodes full CSS (duplicates `email_scaffold.html`) — will drift
   - `_compute_quality_score` binary 0/full per dimension (partial credit would be more useful)
   - `run_sector` retry accumulation edge case (net + rl can both accumulate)

4. **P3 work** (nice-to-have):
   - `CONTINUATION_RULES` (~4000 chars) inflates token count per part 2–9
   - `_NO_ASIDE_PARTS` hardcoded instead of derived from PART_PLAN

## Critical: generate_briefing Return Type Change
Any caller doing `html = asyncio.run(generate_briefing(...))` MUST be updated:
```python
html, warnings, groq, nim = asyncio.run(generate_briefing(cfg, session, max_tokens=...))
```
The 4 callers updated: `scripts/write.py` + 4 sites in `tests/test_write_postprocess.py`.

## Key Files
- `jeeves/write.py` — 3730 lines, all the write phase logic
- `jeeves/llm.py` — KimiNVIDIA subclass, Groq/NIM builders
- `scripts/write.py` — CLI entry point, now owns `_write_run_manifest` call
- `tests/test_write_postprocess.py` — updated to unpack 4-tuple
