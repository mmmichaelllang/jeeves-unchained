# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 3 (write) — three-model pipeline: 9 sequential Groq drafts + 9 concurrent NIM quality-editor passes + 1 OpenRouter Gemma 4 final narrative + profanity editor.** Per user direction: safety and quality over speed. Wall-clock ~10m 30s (Groq path) or ~9–13m (NIM-fallback path).

- **Draft stage (Groq llama-3.3-70b-versatile, 9 calls with conditional 65s TPM sleeps)**. Drafts are now **profanity-free** — the system prompt says "Horrific Slips (draft: zero)". Part 9 outputs `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->` (model doesn't see `newyorker.text` in its payload; pipeline injects verbatim after stitching).
- **NIM editor stage (meta/llama-3.3-70b-instruct, 9 calls in background threads)**. Fixes: banned words, banned transitions, bare URLs, apologetic follow-ups. Adds ~30s wall-clock.
- **Auto-fallback**: if Groq TPD is exhausted, `_invoke_write_llm` retries on NIM. `generate_briefing` only sleeps 65s if the *previous* call used Groq. `write.yml` timeout is 60 min.
- **Verbatim New Yorker injection** (`_inject_newyorker_verbatim`): after stitching, replaces `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->` with actual `session.newyorker.text` in `<p>` tags. If placeholder is missing (model deviated), fallback injects after the intro sentence "reading from this week's Talk of the Town". Sentinels: `<!-- NEWYORKER_START --> / <!-- NEWYORKER_END -->`.
- **OpenRouter Gemma 4 final narrative + profanity editor** (`_invoke_openrouter_narrative_edit`): two-part job — (A) editorial surgery (14 rules: filler deletion, transitions, narrative cohesion, show-don't-tell, butler voice, sourcing, numbers, repetition, reality check, paragraph rhythm, opening sentences, British wit amplification, specificity, end-of-section summaries); (B) adds **exactly five** earned profane asides from the pre-approved pool. Recently-used asides (day-over-day) passed in so editor picks fresh phrases. max_tokens=16384. Falls back silently if key absent, API fails, or truncated. `OPENROUTER_API_KEY` in GitHub Secrets; model override via `OPENROUTER_MODEL_ID` (default `google/gemma-4-27b-it:free`).
- **max_tokens=4096 default**: aligns with NIM's native output cap. Daily Groq budget ~82k, under 100k free-tier ceiling.
- **Dedup advancement protocol** (PART4, PART6, PART7): for recurring series (triadic ontology, AI systems, wearable AI subcategories, UAP), the model identifies the specific title, checks `covered_headlines`, opens with one backward-reference if covered, then covers the NEXT uncovered item in full. Never re-explains from scratch.
- **Research sectors read article content**: `CONTEXT_HEADER` now mandates reading actual article body (exa full text, or `tavily_extract` for serper hits) before writing findings. Enforced per-sector in `local_news`, `global_news`, `intellectual_journals`, `wearable_ai`.
- **89 tests** in `tests/test_write_postprocess.py` cover the full write pipeline.

## Where we left off (2026-04-24, end of session)

- **PR #25 merged to `main`** — all quality improvements from this session: Talk of Town verbatim fix, profanity moved to OpenRouter, narrative cohesion rules, dedup advancement for triadic/ai_systems/wearable_ai/UAP, British wit amplification, article reading in research, 5 additional OpenRouter editorial rules (paragraph rhythm, opening sentences, British wit, specificity, end-of-section summaries).
- **Action required: add `OPENROUTER_API_KEY` to GitHub Secrets** if not yet done — narrative editor is silently skipped without it.
- **Next step: run `write.yml` with `skip_send=true`** and verify: (a) `<!-- NEWYORKER_START -->` in artifact; (b) exactly ~5 profane asides (not 9–14); (c) `OpenRouter narrative edit` log line; (d) no filler phrases; (e) no banned transitions.
- **All phases are live on `main`** (Phases 2, 3, 4 fully wired). Phase 4 → Phase 2 handoff at cron `30 12 * * *`. Write at `40 13 * * *`.
- `GMAIL_OAUTH_TOKEN_JSON` in GH Secrets. Auto-refreshes at runtime.

## Dev branch

- **Current**: `claude/caveman-style-responses-G1q1c` (merged as PR #25)
- Prior major work merged from: `claude/debug-ci-pipeline-TR6xz` (#22 + #23), `claude/gmail-auth-bootstrap-9eYme` (#16–#21), `claude/jeeves-unchained-rewrite-auKzK` (#5)

## Gotchas the README doesn't flag

- **`--dry-run` vs `--use-fixture` on `scripts/correspondence.py`** — both checkboxes exist in `workflow_dispatch`. `--dry-run` short-circuits to a static HTML template (no Kimi, no Groq, no Gmail); `--use-fixture` uses a canned inbox but still calls the real models. If both are ticked, dry-run wins (`scripts/correspondence.py:63`). To smoke-test the real model path from the UI: tick **only** `use_fixture` + `skip_send`.
- **Profane butler asides are intentional — but now added by OpenRouter, not Groq.** Groq drafts are now profanity-free ("Horrific Slips (draft: zero)" in `write_system.md`). The OpenRouter final editor adds exactly five earned asides from the pre-approved pool (`_NARRATIVE_EDIT_SYSTEM_BASE` Part B). The correspondence phase still uses Groq for its profane asides (`jeeves/prompts/correspondence_write.md:18-22`). Post-processing counts profane phrases and warns if fewer than 5 — so a missing `OPENROUTER_API_KEY` will produce a profanity-count warning daily.
- **`(DRY RUN)` in the `<h1>` is a tell.** Only `render_mock_correspondence()` (`jeeves/correspondence.py:396-416`) hardcodes that suffix. If you see it in the artifact, the run took the dry-run branch regardless of what you thought you clicked.
- **Artifact naming convention.** `sessions/*.local.json` and `*.local.html` are gitignored dry-run artifacts. `sessions/session-*.json`, `sessions/correspondence-*.json`, `sessions/briefing-*.html` are the real ones that the workflows commit back to the repo.
- **The Phase 4 handoff JSON is consumed by Phase 2.** `correspondence.yml` runs first in the daily chain (cron `0 12 * * *`), committing `sessions/correspondence-<date>.json`. `research.yml` (`30 12 * * *`) picks it up into `session.correspondence`. Don't break the file name / schema contract without updating both sides.
- **Phase 3 write is a THREE-MODEL pipeline.** (1) Groq drafts sequentially with conditional 65s sleeps. (2) NIM refine runs concurrently in background threads. (3) After stitching + New Yorker injection, OpenRouter Gemma 4 runs once on the full document. Expected logs: 9 `invoking Groq ... [partN]` + 9 `NIM refine [partN]` + 1 `OpenRouter narrative edit`. If the Gemma line is absent, `OPENROUTER_API_KEY` is missing — the briefing still ships without it. If NIM refine lines are absent, `NVIDIA_API_KEY` is missing.
- **NIM serves two roles in write**: (a) quality-editor pass on every Groq draft (`_invoke_nim_refine`, uses `_REFINE_SYSTEM` prompt at temp=0.2), (b) draft fallback when Groq TPD is exhausted (`_invoke_nim_write`, full write system prompt). Both use `meta/llama-3.3-70b-instruct` on `integrate.api.nvidia.com/v1` — same key and endpoint as research-phase Kimi. Override with `NIM_WRITE_MODEL_ID`.
- **OpenRouter Gemma 4 is the final narrative editor.** `_invoke_openrouter_narrative_edit` (`jeeves/write.py`) runs on the full stitched document. It is intentionally the LAST step before `postprocess_html` so it sees the final assembled text including verbatim New Yorker content. Key fallback chain: no key → skip; API error → use unedited; truncated response (no `</html>`) → use unedited. The New Yorker block is protected by `<!-- NEWYORKER_START -->` / `<!-- NEWYORKER_END -->` sentinels in the system prompt.
- **New Yorker verbatim is a code guarantee with two-level fallback.** Part 9 does NOT receive `newyorker.text` in its payload (stripped in `generate_briefing` before building the user prompt) so the model cannot copy it. It must output `<!-- NEWYORKER_CONTENT_PLACEHOLDER -->`. `_inject_newyorker_verbatim` replaces that placeholder with the real text. If the placeholder is missing, a second fallback injects the text immediately after the "reading from this week's Talk of the Town" intro sentence. If that sentence is also absent, a WARNING is logged and the text is skipped.
- **Aside dedup now lives at OpenRouter, not in the Groq loop.** Since Groq drafts contain zero profane asides, the within-run `used_this_run` tracking in `generate_briefing` is vestigial (harmless but no longer functional). Day-over-day dedup is enforced by passing `_recently_used_asides(cfg)` into `_invoke_openrouter_narrative_edit` as `recently_used_asides=` — the OpenRouter system prompt gets a "DO NOT reuse" list before it places the five asides.
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
