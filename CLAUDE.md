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

## Where we left off (2026-04-23)

- `GMAIL_OAUTH_TOKEN_JSON` lives in GH Secrets. Runtime refresh works — `jeeves.gmail INFO gmail sweep` fires cleanly on every dispatch.
- **Phase 4 pipeline is fully live** (PRs #7, #8, #9, #10). `sessions/correspondence-2026-04-23.json` + `.html` are on `main`. Log signature: `classify batch N/M (≤30 msgs)` followed by a Groq render under the 12k TPM ceiling.
- **Phase 2 tool-call bug patched** (this session, not yet shipped). `llama-index-llms-nvidia`'s `get_tool_calls_from_response` does `json.loads(tool_call.function.arguments)` without guarding against `None`. Kimi occasionally emits null args on first-turn tool calls, which raises `TypeError` and kills the FunctionAgent workflow before any search runs. Fix: `jeeves/llm.py::_build_kimi_class` subclasses NVIDIA with a None/empty/invalid-JSON-tolerant override that logs a warning and coerces to `{}`. 5 new unit tests in `tests/test_llm_factories.py`.
- **Phase 2 tool-result caps** (earlier this session, PR #12). After the Kimi tool-call fix shipped, the agent survived turn 1 but filled Kimi's 131k context by turn 5 via unbounded tool results (`tavily_extract` raw_content, `exa_search` 20k-char default). Capped:
  - `tavily_extract`: max 10 URLs/call (was 20), `text` capped at 2500 chars/result.
  - `exa_search`: `text_max_chars` default 20000 → 3000.
  - `enrichment.fetch_article_text`: `text` capped at 3000 chars.
  - `jeeves/llm.py` KimiNVIDIA override: partial-JSON warning downgraded to DEBUG since FunctionAgent's streaming path calls the parser on every mid-stream chunk.
- **Phase 2 per-sector rewrite** (this session, not yet shipped). Caps alone weren't enough — even with per-call caps, 5+ tool-heavy turns × the accumulating conversation history still blew 131k. Architecture shift:
  - New module `jeeves/research_sectors.py` — one `SectorSpec` per researched field (weather / local_news / career / family / global_news / intellectual_journals / wearable_ai / triadic_ontology / ai_systems / uap / newyorker / enriched_articles). Each gets a fresh FunctionAgent with its own 131k budget.
  - No more `emit_session` terminator in the real-agent path. Each sector returns JSON as its final text; the driver aggregates. Dry-run path still uses the old mock + emit_session (untouched).
  - `scripts/research.py::_run_sector_loop` replaces `_run_real_agent`. Iterates specs sequentially. `enriched_articles` runs LAST, seeded with URLs surfaced by prior sectors so the extraction pass targets today's coverage.
  - `research.yml` already has `timeout-minutes: 65`; 12 sectors × ~3 min each ≈ 40 min expected.
  - `vault_insight` is intentionally not a researched sector — it's an offline hook filled by a separate sync and left at its default here.
- **Dedup expansion** (this session, not yet shipped). The write phase's `dedup.covered_urls` + `dedup.covered_headlines` now cover three tiers:
  - **Articles**: URLs harvested from every sector via `collect_urls_from_sector`.
  - **Events**: headlines harvested from `title` / `headline` / `subject` / `role` / `district` / `event` fields via `collect_headlines_from_sector`.
  - **Correspondence**: `email | <sender>` entries parsed from today's correspondence handoff text and folded into `covered_headlines` during the handoff merge.
- **Write prompt dedup tiers** (this session). `jeeves/prompts/write_system.md` replaced its one-line dedup rule with a three-tier directive: exact match → skip entirely; substantive overlap → one-sentence skim opening *"As previously noted, Sir, …"*; genuinely new → full depth.
- **Correspondence brief is now one integrated narrative** (this session, not yet shipped). `jeeves/prompts/correspondence_write.md` no longer mandates rigid `<h2>` subsections (Action Summary / Priority Correspondence / Family Members / Electronic Mail / Platform Note) — it asks for one flowing letter from Jeeves. Family roll-call boilerplate is banned outright: if a family member didn't write, they don't get mentioned. `render_with_groq` now also reads yesterday's `correspondence-<prev>.html`, strips tags, caps at 3000 chars, and passes it as `prior_briefing_text` so the prompt can maintain day-over-day continuity ("As previously noted, Sir, …"). New helper `_load_prior_briefing_text(cfg)` at `jeeves/correspondence.py`.
- Next action: ship this rewrite, re-run `research.yml`, then `write.yml` to close the chain.

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
