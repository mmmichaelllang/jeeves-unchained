# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 3 (write) — two-model pipeline: 9 sequential Groq drafts + 9 concurrent NIM quality-editor passes.** Per user direction: safety and quality over speed; both providers always exercised so neither is a dormant fallback. Wall-clock ~10m 30s.

- **Draft stage (Groq llama-3.3-70b-versatile, 9 calls with 65s TPM sleeps)**. Each part has scoped PART_INSTRUCTIONS + CONTINUATION_RULES (rules 1–9: no greeting, stay in lane, no meta, banned words, banned transitions, no apologies, raw HTML only, no bare URLs, no weather). Part 9 is verbatim New Yorker pass-through with a slimmer system prompt (asides pool stripped).
- **Editor stage (NIM meta/llama-3.3-70b-instruct, 9 calls in background threads)**. Each Groq draft immediately spawns an `_invoke_nim_refine` thread that runs during the next 65s Groq sleep. The editor uses a short focused prompt (`_REFINE_SYSTEM`, ~500 chars) at temperature=0.2 and fixes: banned words, banned transitions, bare URLs, apologetic follow-ups, untethered asides. Adds only ~30s wall-clock (the last part's refine join waits).
- **Auto-fallback**: if Groq's daily TPD quota is exhausted, `_invoke_write_llm` catches "tokens per day" and transparently retries the draft on NIM. If NIM refine fails, the raw Groq draft is used (logged warning).
- **Conditional TPM sleep (PR #22)**: `_invoke_write_llm` returns `(text, used_groq: bool)`. `generate_briefing` tracks `last_used_groq` and only sleeps 65s before the next part if Groq was used. When NIM handles the draft (TPD exhausted), the sleep is skipped — NIM has no 12k TPM limit. Full-NIM path: ~9–13 min vs. the old ~37 min worst case. `write.yml` timeout also bumped 30→60 min.
- **max_tokens=4096 default**: aligns with NIM's native output cap for llama-3.3-70b-instruct. Daily Groq budget: ~73k tokens (9 × ~8k) + correspondence ~9k = ~82k, under the 100k free-tier ceiling. Raising above ~5000 blows the daily budget.
- **Full asides pool** in `jeeves/prompts/write_system.md` (~55 phrases). Two layers of anti-repetition:
  - *Day-over-day*: `_recently_used_asides(cfg, days=4)` scans `sessions/briefing-*.html` from the last 4 days.
  - *Within-run*: `generate_briefing` tracks phrases each part used via `_parse_all_asides()`, passes accumulated list to subsequent parts' system prompt via `run_used_asides=`.
- **Part-specific dedup directives** (three-tier: exact → skip; overlap → one-sentence skim with "As previously noted, Sir"; new → full depth): PART4 for choral+toddler, PART6 for research series (e.g. Karl-Alber Studies on Triadic Ontology).
- `_system_prompt_for_parts` strips both `## HTML scaffold` and `## Briefing structure` blocks (`re.MULTILINE` + `^## ` lookahead).
- **82 tests** in `tests/test_write_postprocess.py` cover the full write pipeline including refine/fallback behavior and the NIM-skips-sleep path.

## Where we left off (2026-04-24, late)

- **PRs #16–#22 all merged to `main`.** The 9-call render (#16, #17, #18), six quality fixes (#18), TPD budget fix + NIM auto-fallback + concurrent NIM quality-editor pass (#20), CLAUDE.md updates (#19, #21), 180s NIM timeout (#21), and the CI cancellation fix (#22 — conditional TPM sleep + 60 min timeout).
- **Next step: re-run `write.yml` with `skip_send=true`** to verify the full pipeline completes. Expected log pattern when Groq TPD is exhausted: `NIM fallback active — skipping TPM sleep before partN` for parts 2–9, total wall-clock ~10–15 min. When Groq TPD is healthy: 9 × `invoking Groq ... [partN]` lines with 65s gaps interleaved with 9 × `NIM refine [partN]` lines, ~10m 30s. Final quality checks: (a) single greeting in Part 1; (b) ≥5 thematic profane asides, no apologies, no repeats; (c) no banned words/transitions; (d) no bare URLs; (e) Talk of the Town verbatim; (f) choral/toddler/research items respect `dedup.covered_headlines`; (g) weather once in Part 1 only.
- **All phases are live on `main`** (Phases 2, 3, 4 fully wired). Phase 4 handoff JSON feeds Phase 2 at cron `30 12 * * *`. Write runs at `40 13 * * *`.
- **Phase 2 per-sector loop** (`jeeves/research_sectors.py`, `scripts/research.py::_run_sector_loop`) — 12 sectors × own FunctionAgent, ~40 min wall-clock. Merged in PR #12.
- **Phase 4 integrated narrative** — no rigid `<h2>` subsections, no family roll-call boilerplate, day-over-day continuity via `_load_prior_briefing_text`. Merged in PR #13.
- **Three-tier dedup** — articles + events + `email | sender` entries in `dedup.covered_headlines`. Merged in PR #15.
- `GMAIL_OAUTH_TOKEN_JSON` in GH Secrets. Auto-refreshes at runtime.

## Dev branch

- **Current**: `claude/debug-ci-pipeline-TR6xz` (merged as PR #22)
- Prior major work merged from: `claude/gmail-auth-bootstrap-9eYme` (#16–#21), `claude/jeeves-unchained-rewrite-auKzK` (#5)

## Gotchas the README doesn't flag

- **`--dry-run` vs `--use-fixture` on `scripts/correspondence.py`** — both checkboxes exist in `workflow_dispatch`. `--dry-run` short-circuits to a static HTML template (no Kimi, no Groq, no Gmail); `--use-fixture` uses a canned inbox but still calls the real models. If both are ticked, dry-run wins (`scripts/correspondence.py:63`). To smoke-test the real model path from the UI: tick **only** `use_fixture` + `skip_send`.
- **Profane butler asides are intentional.** The Groq system prompt (`jeeves/prompts/correspondence_write.md:18-22`) mandates ≥5 slips per briefing from a pre-approved list ("clusterfuck of biblical proportions, Sir", "fucking disaster-class", etc.). Do not sanitize these — post-processing counts them and warns if the briefing has fewer than 5.
- **`(DRY RUN)` in the `<h1>` is a tell.** Only `render_mock_correspondence()` (`jeeves/correspondence.py:396-416`) hardcodes that suffix. If you see it in the artifact, the run took the dry-run branch regardless of what you thought you clicked.
- **Artifact naming convention.** `sessions/*.local.json` and `*.local.html` are gitignored dry-run artifacts. `sessions/session-*.json`, `sessions/correspondence-*.json`, `sessions/briefing-*.html` are the real ones that the workflows commit back to the repo.
- **The Phase 4 handoff JSON is consumed by Phase 2.** `correspondence.yml` runs first in the daily chain (cron `0 12 * * *`), committing `sessions/correspondence-<date>.json`. `research.yml` (`30 12 * * *`) picks it up into `session.correspondence`. Don't break the file name / schema contract without updating both sides.
- **Phase 3 write is a TWO-MODEL pipeline, not one.** `generate_briefing` runs 9 sequential Groq drafts with 65s sleeps, AND spawns a NIM quality-editor thread after each draft (runs during the next Groq sleep). Expected logs: 9 `invoking Groq ... [partN]` lines interleaved with 9 `NIM refine [partN]` lines. Wall-clock ~10m 30s. If you see only Groq calls in the logs, `NVIDIA_API_KEY` is missing (refine silently skipped) — confirm the secret is in the workflow env.
- **NIM serves two roles in write**: (a) quality-editor pass on every Groq draft (`_invoke_nim_refine`, uses `_REFINE_SYSTEM` prompt at temp=0.2), (b) draft fallback when Groq TPD is exhausted (`_invoke_nim_write`, full write system prompt). Both use `meta/llama-3.3-70b-instruct` on `integrate.api.nvidia.com/v1` — same key and endpoint as research-phase Kimi. Override with `NIM_WRITE_MODEL_ID`.
- **Within-run aside dedup lives in code, not the prompt.** `generate_briefing` scans each part's output against `_parse_all_asides()`, accumulates a `used_this_run` list, and injects it into the next part's system prompt via `run_used_asides=`. If you refactor the loop, preserve this — otherwise all 9 parts will independently pick "clusterfuck of biblical proportions" and Jeeves repeats himself.
- **Dedup is headline-matched, not URL-matched in the write phase.** The write prompt explicitly gets `dedup.covered_headlines` but NOT `covered_urls` (see `_trim_session_for_prompt`). If you see the same Karl-Alber volume appear day after day, it means the research phase isn't adding the item's headline to `covered_headlines` — check there, not in write.
- **The 65s TPM sleep is conditional, not unconditional.** `_invoke_write_llm` returns `(text, used_groq: bool)`. `generate_briefing` only sleeps 65s before a call if the *previous* call used Groq. Once Groq TPD is exhausted and NIM takes over, the sleep is skipped for every subsequent inter-part gap. If you refactor the loop, preserve the `last_used_groq` flag — without it the pipeline wastes ~9 minutes of sleep on NIM-fallback runs and will breach the 60-min workflow timeout under extreme NIM latency.
- **Groq TPD (tokens-per-day) limit = input_tokens + max_tokens_requested per call.** The free tier is 100k tokens/day. With max_tokens=8192 × 9 write calls, write alone would need ~110k tokens (input ~37k + output budget ~74k), blowing the daily limit. Default is now max_tokens=4096 per call: each part targets 500–900 words (~700–1200 output tokens), 4096 gives a 3.4× margin and matches NVIDIA NIM's native output cap for meta/llama-3.3-70b-instruct. Total daily write budget: ~73k tokens; plus correspondence Groq call: ~9k; grand total ~82k — within 100k. If you raise max_tokens above ~5000, the production pipeline will fail daily at Part 2 (or fall through to NIM, which has its own throttle).

## Quick nav (file:line pointers)

- `scripts/correspondence.py:59` — `_run` mode dispatch (dry-run / use-fixture / real Gmail)
- `scripts/correspondence.py:97` — `main` + flag parsing + artifact writes
- `jeeves/correspondence.py:396` — `render_mock_correspondence` (the dry-run template)
- `jeeves/correspondence.py` — `classify_with_kimi`, `render_with_groq`, `postprocess_html`, `build_handoff_json`
- `jeeves/prompts/correspondence_write.md` — Groq system prompt (persona, slip list, HTML scaffold, banned words)
- `scripts/gmail_auth.py` — one-shot OAuth flow that mints `GMAIL_OAUTH_TOKEN_JSON`
- `jeeves/gmail.py:41` — `build_gmail_service` (consumes the token JSON, auto-refreshes)
- `.github/workflows/correspondence.yml` — cron + `workflow_dispatch` inputs (`date`, `skip_send`, `use_fixture`, `dry_run`)
- `jeeves/config.py` — `Config.from_env()`, `MissingSecret`
- `jeeves/schema.py` — `SessionModel` + `FIELD_CAPS`

## Session hygiene

Before ending a session where the project state meaningfully advanced (new phase, new branch, new gotcha, pipeline behavior changed), update the **Current focus** and **Where we left off** blocks above. That's the whole mechanism — no hooks, no scripts, just keep this file honest.
