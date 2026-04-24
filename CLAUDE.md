# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 3 (write) — eight-call render with anti-repetition asides.** Per user direction: full pipeline (all seven sector descriptions + full ~55-phrase profane-aside pool) stays intact; only the user payload is split to fit Groq's free-tier 12k TPM. The write phase now makes EIGHT sequential Groq calls with 65s sleeps between each, so total Phase 3 wall-clock ≈ 9 minutes (accepted per "3AM cron, no time pressure" direction).

- **8-part split** (`PART_PLAN` in `jeeves/write.py`): correspondence+weather → local_news → career → family+global_news → intellectual_journals+enriched_articles → triadic+ai_systems → uap+wearable_ai → vault_insight+newyorker (+signoff+coverage placeholder). All parts estimated <11.5k tokens. `_stitch_parts` assembles the final HTML.
- **Full asides pool restored** in `jeeves/prompts/write_system.md` (the PR #14 trim was reverted). Full list is ~55 phrases.
- **Semantic-match directive strengthened**: the `Horrific Slips` rule now explicitly lists thematic buckets (professional dysfunction / scheduling / weather / technical / personal / geopolitical → matched phrase clusters), bans decorative or floating asides, and bans aside-as-topic-sentence. Every aside must be meaningfully connected to the specific content it's commenting on.
- **Anti-repetition (day-over-day)**: `_recently_used_asides(cfg, days=4)` scans `sessions/briefing-*.html` from the last 4 days and flags every pre-approved phrase Jeeves has actually deployed. `_system_prompt_for_parts(cfg)` appends a "Recently used asides — DO NOT reuse" block listing those phrases. Full pool stays visible so Jeeves can still pick thematically; the avoid-list just vetoes yesterday's favorites. No randomization (random sampling breaks thematic matching).
- `_system_prompt_for_parts` still strips the "## HTML scaffold" block (each PART_INSTRUCTIONS provides its own explicit scaffold, and leaving the generic one in would confuse the model).
- 11 tests in `tests/test_write_postprocess.py` now cover the full anti-repetition mechanism, PART_PLAN coverage, and the stitch path.

## Where we left off (2026-04-24)

- **PR #16 is open, CI green (72/72), ready to merge.** Branch: `claude/gmail-auth-bootstrap-9eYme`. Contains the full 8-call write render + thematic asides + anti-repetition work described in "Current focus" above. User will merge manually.
- **After merging PR #16**, the next step is: trigger `write.yml` with `skip_send=true` (and optionally `use_fixture=true` to avoid needing a fresh research run). Expect 8 × `invoking Groq ... [partN]` log lines with 65s gaps, ~9 min wall-clock, then a stitched briefing HTML artifact. Check for ≥5 profane asides that are thematically matched, ≥5000 words, no banned words/transitions.
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
