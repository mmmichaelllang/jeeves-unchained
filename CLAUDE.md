# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 3 (write) — nine-call render with within-run + day-over-day anti-repetition, verbatim New Yorker pass-through, strong dedup directives.** Per user direction: full pipeline (all seven sector descriptions + full ~55-phrase profane-aside pool) stays intact; only the user payload is split to fit Groq's free-tier 12k TPM. The write phase makes NINE sequential Groq calls with 65s sleeps between each (~10 min wall-clock, accepted per "3AM cron, safety and quality over speed" direction).

- **9-part split** (`PART_PLAN` in `jeeves/write.py`): correspondence+weather → local_news → career → family+global_news → intellectual_journals+enriched_articles → triadic+ai_systems → uap+wearable_ai → vault_insight (solo) → newyorker (solo + sign-off + closing tags). Part 9 is a verbatim pass-through — it gets a slimmer system prompt (`_NO_ASIDE_PARTS={"part9"}`) with the Horrific Slips + asides pool stripped, freeing token budget for the 4000-char article.
- **Full asides pool** in `jeeves/prompts/write_system.md` (~55 phrases). **Natural anchor text rule** mandates all URLs embedded in prose anchors — never bare `https://` in body text.
- **CONTINUATION_RULES** block prepended to parts 2–9: (1) no greeting, (2) stay in lane, (3) no meta-commentary, (4) banned words, (5) banned transitions, (6) no apologies after profane asides, (7) raw HTML paragraphs only, (8) no bare URLs in prose, (9) no weather (Part 1 owns it exclusively).
- **No-apology rule**: "I do beg your pardon, Sir" / "pardon my language" / "if you'll excuse the expression" / "if I may say so" are banned from both `write_system.md` Horrific Slips rule AND `BANNED_WORDS` (post-processor flags them).
- **Anti-repetition — TWO layers**:
  - *Day-over-day*: `_recently_used_asides(cfg, days=4)` scans `sessions/briefing-*.html` from the last 4 days, flags every pre-approved phrase Jeeves has actually deployed.
  - *Within-run*: `generate_briefing` tracks which pool phrases each part used via `_parse_all_asides()` scan after each call, feeds the accumulated list into the next part's system prompt via the `run_used_asides` param on `_system_prompt_for_parts`.
  - Both sources merge into one "Recently used asides — DO NOT reuse" block at the bottom of the per-part system prompt. Full pool stays visible so Jeeves still picks thematically; the avoid list just vetoes stale phrases.
- **Part-specific dedup directives** (three-tier: exact → skip; overlap → one-sentence skim with "As previously noted, Sir"; new → full depth):
  - PART4: choral auditions + toddler activities must check `dedup.covered_headlines` before writing.
  - PART6: research series (e.g. Karl-Alber Studies on Triadic Ontology and Trinitarian Philosophy) must check `dedup.covered_headlines` — no re-covering the same volume day after day.
- `_system_prompt_for_parts` strips both `## HTML scaffold` and `## Briefing structure` blocks (each PART_INSTRUCTIONS provides its own explicit scaffold). Regex uses `re.MULTILINE` + `^## ` lookahead so `### Sector` subheadings don't terminate the match early.
- **76 tests** in `tests/test_write_postprocess.py` cover the full write pipeline.

## Where we left off (2026-04-24, late)

- **PRs #16, #17, #18 all merged to `main`.** The 9-call render, the Briefing-structure regex bugfix, the no-apologies rule, the CONTINUATION_RULES block, the within-run aside dedup, the no-bare-URL rule, the choral/toddler + research-series dedup directives — all live on main.
- **Next step: re-run `write.yml` with `skip_send=true`** to verify the full 9-part render with all six quality fixes lands cleanly. Expected: 9 × `invoking Groq ... [partN]` log lines with 65s gaps, ~10 min wall-clock, stitched briefing. Verify: (a) one "Good morning, Mister Lang" only (Part 1); (b) ≥5 thematic profane asides with no apologies and no repeats within the briefing; (c) no banned words/transitions; (d) no bare URLs in prose; (e) Talk of the Town verbatim from `newyorker.text`; (f) choral/toddler/research items respect `dedup.covered_headlines`; (g) weather mentioned once, in Part 1 only.
- **All phases are live on `main`** (Phases 2, 3, 4 fully wired). Phase 4 handoff JSON feeds Phase 2 at cron `30 12 * * *`. Write runs at `40 13 * * *`.
- **Phase 2 per-sector loop** (`jeeves/research_sectors.py`, `scripts/research.py::_run_sector_loop`) — 12 sectors × own FunctionAgent, ~40 min wall-clock. Merged in PR #12.
- **Phase 4 integrated narrative** — no rigid `<h2>` subsections, no family roll-call boilerplate, day-over-day continuity via `_load_prior_briefing_text`. Merged in PR #13.
- **Three-tier dedup** — articles + events + `email | sender` entries in `dedup.covered_headlines`. Merged in PR #15.
- `GMAIL_OAUTH_TOKEN_JSON` in GH Secrets. Auto-refreshes at runtime.

## Dev branch

- **Current**: `claude/gmail-auth-bootstrap-9eYme`
- Prior major work merged from: `claude/jeeves-unchained-rewrite-auKzK` (see #5)

## Gotchas the README doesn't flag

- **`--dry-run` vs `--use-fixture` on `scripts/correspondence.py`** — both checkboxes exist in `workflow_dispatch`. `--dry-run` short-circuits to a static HTML template (no Kimi, no Groq, no Gmail); `--use-fixture` uses a canned inbox but still calls the real models. If both are ticked, dry-run wins (`scripts/correspondence.py:63`). To smoke-test the real model path from the UI: tick **only** `use_fixture` + `skip_send`.
- **Profane butler asides are intentional.** The Groq system prompt (`jeeves/prompts/correspondence_write.md:18-22`) mandates ≥5 slips per briefing from a pre-approved list ("clusterfuck of biblical proportions, Sir", "fucking disaster-class", etc.). Do not sanitize these — post-processing counts them and warns if the briefing has fewer than 5.
- **`(DRY RUN)` in the `<h1>` is a tell.** Only `render_mock_correspondence()` (`jeeves/correspondence.py:396-416`) hardcodes that suffix. If you see it in the artifact, the run took the dry-run branch regardless of what you thought you clicked.
- **Artifact naming convention.** `sessions/*.local.json` and `*.local.html` are gitignored dry-run artifacts. `sessions/session-*.json`, `sessions/correspondence-*.json`, `sessions/briefing-*.html` are the real ones that the workflows commit back to the repo.
- **The Phase 4 handoff JSON is consumed by Phase 2.** `correspondence.yml` runs first in the daily chain (cron `0 12 * * *`), committing `sessions/correspondence-<date>.json`. `research.yml` (`30 12 * * *`) picks it up into `session.correspondence`. Don't break the file name / schema contract without updating both sides.
- **Phase 3 write is NINE Groq calls, not one.** `scripts/write.py` → `generate_briefing` loops over `PART_PLAN` with 65s sleeps between calls. Expect ~10 min wall-clock. If you're debugging and see only one Groq call in the logs, you're looking at a stale run or the loop exited early. Each part has its own system-prompt scaffold; Part 9 (newyorker) gets a slimmer base prompt (no asides pool, no Horrific Slips directive).
- **Within-run aside dedup lives in code, not the prompt.** `generate_briefing` scans each part's output against `_parse_all_asides()`, accumulates a `used_this_run` list, and injects it into the next part's system prompt via `run_used_asides=`. If you refactor the loop, preserve this — otherwise all 9 parts will independently pick "clusterfuck of biblical proportions" and Jeeves repeats himself.
- **Dedup is headline-matched, not URL-matched in the write phase.** The write prompt explicitly gets `dedup.covered_headlines` but NOT `covered_urls` (see `_trim_session_for_prompt`). If you see the same Karl-Alber volume appear day after day, it means the research phase isn't adding the item's headline to `covered_headlines` — check there, not in write.
- **Groq TPD (tokens-per-day) limit = input_tokens + max_tokens_requested per call.** The free tier is 100k tokens/day. With max_tokens=8192 × 9 write calls, write alone would need ~110k tokens (input ~37k + output budget ~74k), blowing the daily limit. Default is now max_tokens=4096 per call: each part targets 500–900 words (~700–1200 output tokens), 4096 gives a 3.4× margin and matches NVIDIA NIM's native output cap for meta/llama-3.3-70b-instruct (the NIM fallback). Total daily write budget: ~73k tokens; plus correspondence Groq call (already at max_tokens=4096): ~9k; grand total ~82k — within 100k. If you raise max_tokens above ~5000, the production pipeline will fail daily at Part 2.

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
