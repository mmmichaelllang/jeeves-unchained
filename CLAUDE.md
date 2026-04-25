# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 3 (write) — three-model pipeline: 9 sequential Groq drafts + 9 concurrent NIM quality-editor passes + 1 OpenRouter Gemma 4 final narrative editor.** Per user direction: safety and quality over speed. Wall-clock ~10m 30s (Groq path) or ~9–13m (NIM-fallback path).

- **Draft stage (Groq llama-3.3-70b-versatile, 9 calls with conditional 65s TPM sleeps)**. Each part has scoped PART_INSTRUCTIONS + CONTINUATION_RULES (rules 1–9). Part 9 outputs `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->` rather than trying to copy the article (verbatim injection happens post-stitch — see below).
- **NIM editor stage (meta/llama-3.3-70b-instruct, 9 calls in background threads)**. Each Groq draft immediately spawns `_invoke_nim_refine` during the next sleep. Fixes: banned words, banned transitions, bare URLs, apologetic follow-ups. Adds ~30s wall-clock.
- **Auto-fallback**: if Groq TPD is exhausted, `_invoke_write_llm` retries on NIM. Returns `(text, used_groq: bool)`. `generate_briefing` only sleeps 65s if the *previous* call used Groq — NIM-fallback path skips all sleeps. `write.yml` timeout is 60 min.
- **Verbatim New Yorker injection** (`_inject_newyorker_verbatim`): after stitching, replaces `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->` with actual `session.newyorker.text` in `<p>` tags, wrapped in `<!-- NEWYORKER_START --> / <!-- NEWYORKER_END -->` sentinels. Deterministic — model never copies the text.
- **OpenRouter final narrative editor** (`_invoke_openrouter_narrative_edit`): runs ONCE on the full stitched+injected document. 14 editorial rules (A1–A14) cover filler, transitions, narrative cohesion, paragraph rhythm, weak openers, British wit amplification, vague attribution, and end-of-section summaries. Profanity placement (B1) handled here: exactly five asides, thematic match, no stacking. Skips NEWYORKER sentinels. Tries four models in order: `nvidia/nemotron-3-super-120b-a12b:free` → `meta-llama/llama-3.3-70b-instruct:free` → `google/gemma-4-31b-it:free` → `openrouter/auto` (free router, highest reasoning). Falls back to unedited only if all four fail. Primary overridable via `OPENROUTER_MODEL_ID` env var. `max_tokens=16384`, timeout=360s.
- **max_tokens=4096 default**: aligns with NIM's native output cap. Daily Groq budget: ~73k tokens (9 × ~8k) + correspondence ~9k = ~82k, under the 100k free-tier ceiling. Raising above ~5000 blows the daily budget.
- **Full asides pool** in `jeeves/prompts/write_system.md` (~55 phrases). Two layers of anti-repetition:
  - *Day-over-day*: `_recently_used_asides(cfg, days=4)` scans `sessions/briefing-*.html` from the last 4 days.
  - *Within-run*: `generate_briefing` tracks phrases each part used via `_parse_all_asides()`, passes accumulated list to subsequent parts' system prompt via `run_used_asides=`.
- **Profanity moved to OpenRouter pass.** Drafts (PART1–PART8) write ZERO profane asides. The OpenRouter editor inserts exactly five, thematically placed. `recently_used_asides` is passed so it picks fresh phrases.
- **Per-section dedup advancement protocols** (PART4 toddler, PART6 triadic+ai_systems, PART7 wearable_ai): identify specific title/model/product → check covered_headlines → one backward-reference clause if already covered → pivot to next uncovered item → if all repeat, one sentence and move on. PART4 toddler: lead with new; repeats get embedded clause only; if all repeat, brief seasonal suggestion flagged as Jeeves's own.
- **SYNTHESIS CLOSE (PART3, PART5, PART6)**: replaces earlier CLOSING SUMMARY BAN — closing observation is now *required* but must be specific and non-transferable (names an actual article, posting, or paper). Generic category-level wrap-ups are banned.
- **WIT QUOTA (CONTINUATION_RULES rule 11)**: at least one sardonic/wry/dark-humorous observation per part. Non-profane is fine; must react to specific content, not be a generic quip.
- **OpenRouter Part B**: counts existing profane asides in the draft, adds only enough to reach exactly five total. Prevents 6–7 when Groq drifts despite the zero-asides instruction.
- **Horrific Slips rule**: `[HARD RULE]` level, no "the editor will handle it" escape hatch. Part 9 stripping regex updated to match `[HARD RULE] Horrific Slips` heading.
- **Day-over-day dedup — now actually works**: `collect_headlines_from_sector` extracts the first sentence of any `findings` string (news `Finding` objects have no `title`/`headline` key — this was the root cause of dedup being empty). `_run_sector_loop` seeds `covered_headlines` from the prior session so the write phase synthesizes across days.
- **Exa text depth**: `text_max_chars` raised from 3,000 → 20,000 (~600 words → ~3,000 words per result). Exa hits no longer need a follow-up `tavily_extract`.
- **Never-empty news**: `local_news` and `global_news` sector instructions have mandatory expansion cascade — agent must widen geofence/scope before returning an empty array.
- **Research sectors — mandatory article reading.** CONTEXT_HEADER has a CRITICAL block: exa results carry full text; for serper/tavily hits, call `tavily_extract` before writing findings. Reinforced in local_news, global_news, intellectual_journals, wearable_ai sector instructions.
- `_system_prompt_for_parts` strips both `## HTML scaffold` and `## Briefing structure` blocks (`re.MULTILINE` + `^## ` lookahead).
- **89 tests** in `tests/test_write_postprocess.py` cover the full write pipeline including refine/fallback behavior, NIM-skips-sleep path, New Yorker injection, and narrative editor fallback.

## Where we left off (2026-04-25)

- **PRs #16–#30 all merged to `main`.** Latest: PR #30 (two substantive commits + one workflow commit).
- **All phases are live on `main`** (Phases 2, 3, 4 fully wired). Phase 4 handoff JSON feeds Phase 2 at cron `30 12 * * *`. Write runs at `40 13 * * *`.
- **Action required: add `OPENROUTER_API_KEY` to GitHub Secrets** before the next write run, otherwise the narrative editor step is silently skipped.
- **Action required (optional): add `GOOGLE_CLOUD_PROJECT` + `GOOGLE_APPLICATION_CREDENTIALS_JSON` + `GOOGLE_CLOUD_REGION` to GitHub Secrets** to enable Vertex AI grounded search with Dynamic Retrieval.
- **Next step: manually trigger `correspondence.yml`** (skip_send=true) and verify: (a) Research auto-starts after correspondence completes; (b) Write auto-starts after research completes; (c) `covered_headlines` in the session JSON includes first-sentence labels from news findings; (d) `ai_systems.findings` is a string (not a list) in the saved JSON.

## Dev branch

- **Current**: `claude/improve-dedup-triadic-studies-rEgcE` (merged as PRs #26–#30)
- Prior major work merged from: `claude/caveman-style-responses-G1q1c` (#25), `claude/debug-ci-pipeline-TR6xz` (#22–#23), `claude/gmail-auth-bootstrap-9eYme` (#16–#21), `claude/jeeves-unchained-rewrite-auKzK` (#5)

## Gotchas the README doesn't flag

- **`--dry-run` vs `--use-fixture` on `scripts/correspondence.py`** — both checkboxes exist in `workflow_dispatch`. `--dry-run` short-circuits to a static HTML template (no Kimi, no Groq, no Gmail); `--use-fixture` uses a canned inbox but still calls the real models. If both are ticked, dry-run wins (`scripts/correspondence.py:63`). To smoke-test the real model path from the UI: tick **only** `use_fixture` + `skip_send`.
- **Profane butler asides are intentional.** The Groq system prompt (`jeeves/prompts/correspondence_write.md:18-22`) mandates ≥5 slips per briefing from a pre-approved list ("clusterfuck of biblical proportions, Sir", "fucking disaster-class", etc.). Do not sanitize these — post-processing counts them and warns if the briefing has fewer than 5.
- **`(DRY RUN)` in the `<h1>` is a tell.** Only `render_mock_correspondence()` (`jeeves/correspondence.py:396-416`) hardcodes that suffix. If you see it in the artifact, the run took the dry-run branch regardless of what you thought you clicked.
- **Artifact naming convention.** `sessions/*.local.json` and `*.local.html` are gitignored dry-run artifacts. `sessions/session-*.json`, `sessions/correspondence-*.json`, `sessions/briefing-*.html` are the real ones that the workflows commit back to the repo.
- **The Phase 4 handoff JSON is consumed by Phase 2.** `correspondence.yml` runs first in the daily chain (cron `0 12 * * *`), committing `sessions/correspondence-<date>.json`. `research.yml` (`30 12 * * *`) picks it up into `session.correspondence`. Don't break the file name / schema contract without updating both sides.
- **Phase 3 write is a THREE-MODEL pipeline.** (1) Groq drafts sequentially with conditional 65s sleeps. (2) NIM refine runs concurrently in background threads. (3) After stitching + New Yorker injection, OpenRouter Gemma 4 runs once on the full document. Expected logs: 9 `invoking Groq ... [partN]` + 9 `NIM refine [partN]` + 1 `OpenRouter narrative edit`. If the Gemma line is absent, `OPENROUTER_API_KEY` is missing — the briefing still ships without it. If NIM refine lines are absent, `NVIDIA_API_KEY` is missing.
- **NIM serves two roles in write**: (a) quality-editor pass on every Groq draft (`_invoke_nim_refine`, uses `_REFINE_SYSTEM` prompt at temp=0.2), (b) draft fallback when Groq TPD is exhausted (`_invoke_nim_write`, full write system prompt). Both use `meta/llama-3.3-70b-instruct` on `integrate.api.nvidia.com/v1` — same key and endpoint as research-phase Kimi. Override with `NIM_WRITE_MODEL_ID`.
- **OpenRouter Gemma 4 is the final narrative editor.** `_invoke_openrouter_narrative_edit` (`jeeves/write.py`) runs on the full stitched document. It is intentionally the LAST step before `postprocess_html` so it sees the final assembled text including verbatim New Yorker content. Key fallback chain: no key → skip; API error → use unedited; truncated response (no `</html>`) → use unedited. The New Yorker block is protected by `<!-- NEWYORKER_START -->` / `<!-- NEWYORKER_END -->` sentinels in the system prompt.
- **New Yorker verbatim is now a code guarantee, not a model instruction.** Part 9 outputs `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->`. `_inject_newyorker_verbatim` replaces it with the actual `session.newyorker.text` paragraphs. If the placeholder is absent (model ignored the instruction), a WARNING is logged and the New Yorker text is simply missing — it won't hallucinate content, it just won't appear.
- **Within-run aside dedup lives in code, not the prompt.** `generate_briefing` scans each part's output against `_parse_all_asides()`, accumulates a `used_this_run` list, and injects it into the next part's system prompt via `run_used_asides=`. If you refactor the loop, preserve this — otherwise all 9 parts will independently pick "clusterfuck of biblical proportions" and Jeeves repeats himself.
- **Dedup is headline-matched, not URL-matched in the write phase.** The write prompt explicitly gets `dedup.covered_headlines` but NOT `covered_urls` (see `_trim_session_for_prompt`). If you see the same Karl-Alber volume appear day after day, it means the research phase isn't adding the item's headline to `covered_headlines` — check there, not in write.
- **The 65s TPM sleep is conditional, not unconditional.** `_invoke_write_llm` returns `(text, used_groq: bool)`. `generate_briefing` only sleeps 65s before a call if the *previous* call used Groq. Once Groq TPD is exhausted and NIM takes over, the sleep is skipped for every subsequent inter-part gap. If you refactor the loop, preserve the `last_used_groq` flag — without it the pipeline wastes ~9 minutes of sleep on NIM-fallback runs and will breach the 60-min workflow timeout under extreme NIM latency.
- **Groq TPD (tokens-per-day) limit = input_tokens + max_tokens_requested per call.** The free tier is 100k tokens/day. With max_tokens=8192 × 9 write calls, write alone would need ~110k tokens (input ~37k + output budget ~74k), blowing the daily limit. Default is now max_tokens=4096 per call: each part targets 500–900 words (~700–1200 output tokens), 4096 gives a 3.4× margin and matches NVIDIA NIM's native output cap for meta/llama-3.3-70b-instruct. Total daily write budget: ~73k tokens; plus correspondence Groq call: ~9k; grand total ~82k — within 100k. If you raise max_tokens above ~5000, the production pipeline will fail daily at Part 2 (or fall through to NIM, which has its own throttle).
- **Manual workflow chaining via `gh workflow run`.** `correspondence.yml` and `research.yml` each have a final "Chain to…" step that fires only on `workflow_dispatch` (not cron). Manual correspondence → research auto-starts → write auto-starts (3-hop). Manual research → write auto-starts (2-hop). `dry_run=true` on correspondence suppresses the chain. Both workflows have `actions: write` permission. The chain step is `if: success() && ...` so a failing run never triggers the next phase. The target receives a fresh `workflow_dispatch` event (defaults for all inputs) — do NOT pass `--date` or other flags unless you modify the chain steps, since the auto-triggered run targets "today UTC" by design.

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
