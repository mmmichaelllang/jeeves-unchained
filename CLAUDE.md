# CLAUDE.md — session memory for `jeeves-unchained`

Claude Code auto-reads this file at session start. It is the handoff between working sessions — if you're Claude and you just started a fresh session, everything below is what your previous self knew.

Full project docs (phase table, model split, flags, secrets, Gmail OAuth provisioning, schema) live in the README and are imported here:

@README.md

---

## Current focus

**Phase 2/3/4 — eighth sprint complete (PR #54, 2026-04-28).** Guaranteed JSON repair for sector output failures: deterministic normalisation layer + LLM repair retry. All 172 tests green.

**Research architecture (as of 2026-04-28, post PRs #43–#46):**
- Sequential sector execution (`_SECTOR_SEMAPHORE=1`) — NIM free tier can't handle concurrent Kimi agents.
- Per-sector `FunctionAgent` (Kimi K2.5 on NIM) with `max_tokens=4096` for deep sectors (triadic_ontology, ai_systems, uap) to prevent NIM streaming drops.
- `build_kimi_llm` sets `max_retries=0` on the underlying openai client — disables SDK retry amplification. `run_sector` owns all retry decisions.
- **NIM 429 rate-limit recovery**: `_is_nim_rate_limit()` detects `"429"/"too many requests"` in the exception. Sector sleeps 60s then retries (up to 2 attempts: `[60, 120]`s). NIM network errors use existing `[10, 30, 60]`s retry path.
- IMMEDIATE FIRST ACTION directives in triadic_ontology and ai_systems force Kimi to call exa_search before generating any reasoning, preventing the "23s reasoning → NIM stream drop" crash.
- Quota guard: `_quota_snapshot` / `_quota_increased` reject sectors where no search provider was called (hallucination prevention).
- **Deep-sector forced retry**: when quota guard fires for `shape=="deep"` sectors (triadic_ontology, ai_systems, uap), `_deep_sector_forced_retry` runs a stripped-down agent with a pre-specified exa_search call. `_DEEP_FALLBACK_QUERIES` provides the query strings.
- `_REDIRECT_ARTIFACT_HOSTS` filter in `collect_urls_from_sector` strips Gemini grounding API redirect URLs from `covered_urls` and the `enriched_articles` seed.
- CONTEXT_HEADER enforces mandatory Round 1 (search) → Round 2 (read/extract) research discipline.
- `intellectual_journals` mandates 3 parallel exa searches targeting separate outlet groups (LRB/Aeon, NYRB/ProPublica, Marginalian/Big Think) with a DIVERSITY RULE requiring ≥3 different publications.
- `global_news` requires SOURCE DIVERSITY (BBC/Guardian/Al Jazeera must appear) and bans Gemini redirect URLs from the output.
- `enriched_articles` has an explicit PRIORITY ORDER (global → intellectual → wearable → deep → local news last); Reuters warned as 401 source; failed fetches must be replaced; text field capped at 500 chars in JSON output.
- **Empty-query guards**: serper, tavily_search, tavily_extract, exa return plain **strings** (not dicts) when called with empty args. LlamaIndex's `_parse_tool_output` calls `str()` on dict returns, producing Python repr with single quotes that NIM's JSON parser rejects with "Unterminated string" 400.
- **`function.arguments` normalization**: `get_tool_calls_from_response` in `llm.py` now also sets `tool_call.function.arguments = "{}"` when arguments are None/empty, so the raw history entry that LlamaIndex records is a valid JSON string that NIM's pydantic validator accepts.
- **`ToolCallBlock.tool_kwargs` normalization (PR #48, corrected in PR #50)**: `KimiNVIDIA._normalize_tool_kwargs` converts any `ToolCallBlock.tool_kwargs={}` (empty dict) → `"{}"` (string) and any `dict` → `json.dumps(dict)`. This is the *true* fix for the "Extra data: line 1 column 3 (char 2)" 400 crash. CRITICAL: it must be called from `astream_chat_with_tools` (not `achat_with_tools`) because `FunctionAgent.take_step()` always uses `streaming=True` (set in `BaseWorkflowAgent` default) and calls `astream_chat_with_tools`. The `achat_with_tools` override in PR #48 was never executed in production.
- **`talk_of_the_town` returns JSON string** (PR #48): `_run()` now returns `json.dumps(base)` at every exit, not `dict`. This prevents LlamaIndex's `str(dict)` Python-repr conversion (single quotes) from landing in the NIM context as invalid JSON.
- **`talk_of_the_town` Jina fallback** (PR #48): when ld+json `articleBody` is absent/short, `_jina_fetch()` fetches clean markdown via `r.jina.ai` (free tier, no key). `_clean_jina_text()` applies stop markers, photo-credit removal, newsletter boilerplate stripping, and markdown artifact cleanup. Raw HTML `_fallback_paragraphs` is now a last resort only.
- **`talk_of_the_town` byline/date** (PR #48): `_extract_byline()` normalises the ld+json `author` field; `datePublished` is also captured. Both appear in the returned JSON and the newyorker sector output.
- **`enriched_articles` text cap enforced in code** (PR #48): `_parse_sector_output` truncates each entry's `text` field to 500 chars after a successful JSON parse, regardless of model compliance. `max_tokens=2048` for enriched shape prevents the 4+ min NIM responses that were causing JSON truncation.
- **None id/name skip**: degenerate tool calls with `tool_call.id=None` or `function.name=None` are skipped (logged at WARNING) rather than propagating a pydantic `ToolSelection` crash.
- **Gemini daily cap**: `DAILY_HARD_CAPS["gemini_grounded"] = 12` (corrected from 1490 which assumed paid Search Grounding tier; actual free-tier limit is 20 generate_content RPD for gemini-2.5-flash). On a 429 response, `gemini_grounded.py` immediately exhausts the daily counter so subsequent sectors skip Gemini automatically.
- `family` instruction has 3 explicit mandatory parallel searches with specific query strings.
- **All search tools return JSON strings (PR #50)**: `serper.py`, `tavily.py`, `exa.py`, `enrichment.py` now return `json.dumps(...)` at all exit points (success and error). This prevents LlamaIndex's `str(dict)` Python-repr conversion from producing single-quoted strings that NIM cannot parse. The empty-query guards already returned plain strings; now SUCCESS paths do too.
- **172 tests** across `tests/test_write_postprocess.py`, `tests/test_research_sectors.py`, `tests/test_llm_factories.py`, `tests/test_correspondence.py`, `tests/test_quota_ledger.py`, and `tests/test_schema.py` (added in PRs #50–#54).
- **Guaranteed JSON repair (PR #54)**: `_parse_sector_output` returns `_ParseFailed` sentinel (not `spec.default`) on structural failures. Before escalating to LLM: `_try_normalize_json` attempts four deterministic fixes in order — Python repr→JSON (single-quotes, `True`/`False`/`None`), trailing-comma removal, truncation recovery (salvages complete items from NIM stream-dropped arrays), bare-object-to-array coercion. Only truly unrecoverable output reaches `_json_repair_retry` (no-tools agent reformats malformed raw, or synthesises from sector instruction when raw is empty).

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
- `_system_prompt_for_parts` strips both `## HTML scaffold` and `## Briefing structure` blocks (`re.MULTILINE` + `^## ` lookahead).

## Where we left off (2026-04-28)

- **PRs #43–#54, all merged.** All phases live on `main` (Phases 2, 3, 4 fully wired). Cron: correspondence `0 12`, research `30 12`, write `40 13`.
- **Action required: add `OPENROUTER_API_KEY` to GitHub Secrets** before the next write run.
- **172 tests green** as of this sprint.

### Eighth sprint (PR #54) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #54 | `career` sector returned Python repr `{'key': 'value'}` (single-quoted) → `JSONDecodeError` → silent empty default | `_python_repr_to_json` normalisation; `_parse_sector_output` calls `_try_normalize_json` before returning `_ParseFailed` |
| #54 | `enriched_articles` after 429 recovery: all tool calls had `None` id/name → no JSON output → silent empty default | `_ParseFailed` sentinel + `_json_repair_retry` (no-tools agent synthesises JSON from sector instruction when raw is empty) |
| #54 | NIM stream-dropped arrays (truncated mid-item) parsed as `JSONDecodeError` → silent empty default | `_recover_truncated_array` finds last `}`, closes the bracket, salvages complete items |
| #54 | Trailing commas on last array/object element (`[...,]`) → `JSONDecodeError` | `_remove_trailing_commas` regex applied before giving up |
| #54 | Model returns bare `{...}` dict for list/enriched shape → bracket search fails → empty default | Bare-object-to-array coercion wraps in `[...]` (checked first for `is_array=True`) |
| #54 | `response is None` → silent empty default | Now triggers `_json_repair_retry` with empty `_ParseFailed` instead |
| #54 | No tests for any of the above | 16 new tests; 172 total |

### Seventh forensic sprint (PR #53 cont.) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #53 | `render_mock_briefing` inserted `session.weather`, `session.newyorker.text`, and `session.newyorker.url` directly into HTML without escaping — LLM-sourced article text or URLs could contain `<script>` or attribute-breaking `"` | `html.escape()` applied via imported `import html as _html`; URL uses `quote=True` |
| #53 | COVERAGE_LOG JSON was embedded in `<!-- ... -->` comments without guarding against `-->` inside headline/URL strings — could prematurely close the comment and expose raw JSON as visible HTML | Added `_safe_json_for_comment()` helper that replaces `-->` with `-->` before embedding; used in both COVERAGE_LOG write sites |
| #53 | No regression tests for the above | Added `test_safe_json_for_comment_escapes_html_comment_close`, `test_render_mock_briefing_escapes_html_in_session_fields` |

### Sixth forensic sprint (PR #53) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #53 | `QuotaLedger.save()` serialised `_state` without holding `_lock` — a concurrent `record()` call could produce a corrupted quota file | Serialise inside `with self._lock:` block; write outside lock |
| #53 | `QuotaLedger.snapshot()` read `_state` without `_lock` — snapshot could be inconsistent under concurrent mutation | `json.dumps`/`json.loads` copy now inside `with self._lock:` |
| #53 | `load_session_by_date` called `json.loads` with no error guard — a zero-byte or truncated session file raised `JSONDecodeError` instead of the graceful `FileNotFoundError` expected by callers | Wrapped in `try/except (JSONDecodeError, ValueError)` → re-raises `FileNotFoundError("empty or corrupted")` |
| #53 | No regression tests for the above | Added `test_snapshot_is_deep_copy`, `test_save_roundtrip_is_consistent`, `test_load_session_raises_on_empty_file`, `test_load_session_raises_on_truncated_json` |

### Fifth forensic sprint (PR #53) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #53 | `render_mock_correspondence` inserts Gmail sender/summary directly into HTML without escaping — XSS/injection if attacker-controlled email arrives | `html.escape()` applied to `c.sender`, `c.summary`, `c.classification` |
| #53 | `list_message_ids` breaks on empty batch even when `nextPageToken` exists — could miss messages at page boundaries | Removed `or not batch` from break condition; now breaks only on absent `nextPageToken` |
| #53 | `parse_rfc2822_date` dead code in `gmail.py` — never called anywhere in the codebase | Removed function and unused `datetime` import |
| #53 | No regression test for HTML escaping in mock renderer | Added `test_render_mock_correspondence_escapes_html_in_user_fields` |

### Fourth research debug sprint (PRs #50–#51) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #50 | All sectors still failing after PR #48 — `_normalize_tool_kwargs` never ran | `FunctionAgent` uses `streaming=True` → calls `astream_chat_with_tools`, not `achat_with_tools`. Added `astream_chat_with_tools` override to `KimiNVIDIA`. |
| #50 | `local_news`/`enriched_articles` "Unterminated string" from `fetch_article_text` returning dict | `fetch_article_text` now returns `json.dumps(base)` at all exits (str, not dict) |
| #50 | ALL sectors risk "Unterminated string" from serper/tavily/exa returning dicts | `serper.py`, `tavily.py` (search+extract), `exa.py` success and error paths now return `json.dumps(...)` |
| #51 | `global_news`/any Gemini-using sector "Unterminated string" from `gemini_grounded` returning dict | `gemini_grounded.py` all exit paths (cap hit, 429, API error, success) now return `json.dumps(...)`. Added empty-question guard. |

### Third research debug sprint (PRs #48–#49) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #48 | `weather`/`local_news`/`career`/`newyorker` NIM 400 "Extra data: line 1 column 3 (char 2)" every run | `KimiNVIDIA._normalize_tool_kwargs` + (wrong) `achat_with_tools` override — logic correct, method wrong (fixed in #50) |
| #48 | `talk_of_the_town` returning Python dict → `str(dict)` repr (single quotes) in NIM context | `_run()` now returns `json.dumps(base)` at all exit points |
| #48 | `career` NIM 400 pydantic "Input should be a valid string" for `tool_call.id=None` | `_normalize_tool_kwargs` now strips `additional_kwargs["tool_calls"]` entries where `id=None` before each NIM send |
| #49 | `enriched_articles` 4+ min NIM response → JSON truncated at ~12.5KB | `max_tokens=2048` for enriched shape; `_parse_sector_output` truncates `text` fields to 500 chars after parse |
| #49 | `newyorker` sector missing byline and date | `talk_of_the_town._run()` now extracts `byline`/`date` from ld+json; Jina AI reader added as fallback before raw HTML extraction; content cleaning (stop markers, credits, markdown noise) applied to Jina text |

### Second research debug sprint (PRs #43–#46) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #43 | `family` 400 crash (serper/tavily/exa called with None args → corrupt NIM context) | Empty-query guards in all 3 tools; family instruction rewritten with 3 explicit parallel searches |
| #43 | `triadic_ontology` quota guard fires every run (Kimi answers from training data) | `_deep_sector_forced_retry` with forced-first-tool-call system prompt; `_DEEP_FALLBACK_QUERIES` |
| #43 | Gemini 429 cascade (cap was 1490, actual free tier is 20 RPD) | `DAILY_HARD_CAPS["gemini_grounded"] = 12`; 429 response exhausts daily counter immediately |
| #43 | `enriched_articles` Reuters 401 slots | Instruction names Reuters as 401 source; failed fetches must be replaced |
| #44 | NIM 429 cascade after sector 3 (SDK retried 3× with 0.45s backoffs, amplifying rate limit) | `max_retries=0` in `build_kimi_llm`; `_is_nim_rate_limit()` + 60/120s sector-level backoff |
| #45 | `local_news`/`enriched_articles` NIM 400 "Unterminated string" (empty-query guard returned dict → `str()` → Python repr → NIM parser failure) | Guards now return plain strings; `TextBlock(text=str(dict))` produces single-quote repr, not JSON |
| #46 | `weather` NIM 400 pydantic validation (`function.arguments=None/dict` in history, NIM requires string) | `get_tool_calls_from_response` normalizes `tool_call.function.arguments = "{}"` on None/empty |
| #46 | `local_news` pydantic `ToolSelection` crash (Kimi emits `tool_call.id=None, function.name=None`) | Skip degenerate tool calls with None id/name rather than propagating crash |
| #46 | `enriched_articles` 8-min NIM stream + JSON parse failure (model output full article texts with unescaped chars) | Instruction caps `text` field to 500 chars in JSON output |

### First research debug sprint (PRs #37–#42) — what was fixed

| PR | Problem | Fix |
|---|---|---|
| #37 | All sectors returned defaults in <1 min (NIM 429 from semaphore=3) | `_SECTOR_SEMAPHORE=1` (sequential) |
| #38 | Kimi answering from training data (no tool calls) | URL validation filter drops uncited items; "STALE" prompt |
| #39 | `agent._system_prompt` attribute error on retry | Extract `_system_prompt` as local variable before `FunctionAgent()` |
| #40 | `intellectual_journals`, `wearable_ai` passing quota guard on training-data answers | Quota snapshot diff guard + per-sector `pre_quota` snapshot |
| #41 | triadic_ontology/ai_systems NIM stream crash (23s response → peer closed) | IMMEDIATE FIRST ACTION directive; `max_tokens=4096` for deep sectors; 3 retries (10/30/60s) |
| #42 | Session quality: monoculture journals, Reuters-only global news, Gemini redirect URLs | 3-parallel exa for journals; diversity rules; `_REDIRECT_ARTIFACT_HOSTS` filter; 2-round discipline |

## Dev branch

- **Current**: `claude/forensic-fixes-sprint6` (PR #53 open — quota ledger locking, session truncation guard)
- Prior sprint: `claude/forensic-fixes-sprint6` (PR #53 merged — HTML injection fix, pagination fix, dead code removal)
- Prior sprint: `claude/fix-gemini-json-return-sprint5` (PRs #50–#52 all merged)
- Prior sprint: `claude/fix-jeeves-research-workflow-Jo1u5` (PRs #43–#49 all merged)
- Prior major work: `claude/improve-dedup-triadic-studies-rEgcE` (#34), `claude/never-empty-news-fallbacks-rEgcE` (#33), `claude/forensic-audit-fixes-rEgcE` (#32)

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
- **Gemini grounding API returns ephemeral redirect URLs** (`vertexaisearch.cloud.google.com/grounding-api-redirect/...`) as citation sources. These expire within hours and can't be deduped by URL. `collect_urls_from_sector` filters them via `_REDIRECT_ARTIFACT_HOSTS`. The `global_news` sector instruction additionally tells Kimi to look up canonical article URLs via `serper_search` instead of including the redirect directly. Do not add redirect domains back to the URL extraction logic.
- **Empty-query guards must return strings, not dicts.** LlamaIndex's `FunctionTool._parse_tool_output()` falls through to `TextBlock(text=str(raw_output))` for any return value that isn't a `TextBlock`/`ImageBlock`/`BaseNode`. `str()` on a Python dict produces repr with single quotes (`{'error': '...'}`), which is not valid JSON. NIM receives this as a tool call result and tries to parse it, failing with `"Unterminated string starting at: line 1 column 11 (char 10)"`. All empty-query/empty-input guards in the search tools return plain strings.
- **`function.arguments` must be a JSON string in history, not None/dict.** Kimi occasionally emits tool calls with `function.arguments=None` or `{}` (dict). `get_tool_calls_from_response` coerces the parsed args to `{}`, but if the RAW `tool_call.function.arguments` is left as None/dict, LlamaIndex records that in the conversation history. On the next NIM call, NIM's pydantic validator rejects it (`Input should be a valid string [type=string_type, input_value={}, input_type=dict]`), returning 400. Fix: also set `tool_call.function.arguments = "{}"` when normalizing.
- **`ToolCallBlock.tool_kwargs` is what `to_openai_message_dict` actually serializes — NOT `additional_kwargs`.** When Kimi emits `function.arguments=None`, LlamaIndex's `from_openai_message()` stores `ToolCallBlock(tool_kwargs=tool_call.function.arguments or {})` — an empty **dict**, not `"{}"`. On the next NIM call, `to_openai_message_dict` emits `"arguments": {}` (JSON object), which NIM rejects with "Extra data: line 1 column 3 (char 2)" 400. The PR #46 fix to `additional_kwargs` was insufficient because `to_openai_message_dict` checks `already_has_tool_calls=True` and uses the `ToolCallBlock` path, bypassing `additional_kwargs` entirely. The real fix: `_normalize_tool_kwargs` converts `ToolCallBlock.tool_kwargs={}` → `"{}"` in-place. CRITICAL: must be called from `astream_chat_with_tools`, NOT `achat_with_tools`. `FunctionAgent.take_step()` always uses `streaming=True` (default in `BaseWorkflowAgent`) and calls `self.llm.astream_chat_with_tools(...)`. The `achat_with_tools` path is NEVER reached in production — an override there is completely dead code.
- **ALL search tool returns must be JSON strings, not Python dicts.** `LlamaIndex.FunctionTool._parse_tool_output()` (line 307) falls through to `TextBlock(text=str(raw_output))` for any non-ContentBlock return. `str(dict)` → Python repr with single quotes (`{'key': 'value'}`). NIM receives this as tool message content and rejects it with "Unterminated string" 400 when it tries to parse the content as JSON. Fix: `serper.py`, `tavily.py`, `exa.py`, `enrichment.py` all return `json.dumps(...)` at every exit point. Empty-query guards already returned strings; success/error dict paths were the remaining bug.
- **Kimi emits degenerate tool calls with None id/name.** Occasionally `tool_call.id=None` and `function.name=None` come through. Creating `ToolSelection(tool_id=None, tool_name=None)` raises pydantic ValidationError and kills the sector. `get_tool_calls_from_response` skips these with a WARNING. However, LlamaIndex still records the raw assistant message (including the id=None entries) in the chat history. On the next NIM call, NIM's pydantic validator rejects `id=None` with 400 "Input should be a valid string for ChatCompletionMessageFunctionToolCallParam.id", crashing the sector. Fix: `_normalize_tool_kwargs` also strips any `additional_kwargs["tool_calls"]` entries where `id=None` before each NIM send.
- **Gemini 2.5 Flash free tier is 20 generate_content RPD, not 1500 grounded-search calls/day.** The original `DAILY_HARD_CAPS["gemini_grounded"] = 1490` assumed the paid Search Grounding quota. The actual free-tier metric is `GenerateRequestsPerDayPerProjectPerModel-FreeTier` with `quotaValue=20`. Cap is now 12. When a 429 arrives, `gemini_grounded.py` immediately sets the counter to the cap so all subsequent sectors skip Gemini without another 429 hit.
- **NIM 429 rate-limit amplification.** With `max_retries=2` (SDK default), each logical NIM call that 429s becomes 3 actual requests (1 original + 2 retries at 0.45s/0.95s). This burns 3× the rate-limit budget. By sector 4, the limit is saturated and every subsequent sector crashes on its first call. Fix: `max_retries=0` in `build_kimi_llm`; `run_sector` handles all retries with proper 60s backoff.
- **`triadic_ontology` needs forced-search retry every run.** Kimi consistently answers this obscure topic from training data, triggering the quota guard. `_deep_sector_forced_retry` fires automatically; total overhead ~10s. No further action needed — this is expected behavior.
- **`uap` occasionally needs a 60s NIM 429 sleep.** This is correct behavior from the rate-limit backoff. If it 429s twice (60s + 120s = 3 min overhead), that's still within the 15-min research window.
- **Research sector exa_py calls are invisible in httpx logs.** The `exa_py` SDK uses `requests` not `httpx`, so exa searches don't appear in the `httpx INFO HTTP Request:` log lines. When counting tool calls from logs, infer exa was called from: (a) time gaps between NIM calls, (b) quota ledger delta for "exa", (c) the presence of exa.ai URLs in the session JSON.
- **NIM streaming drop threshold is ~20-25 seconds of continuous output.** Kimi K2.5 uses extended chain-of-thought tokens. If the model generates >~2000 output tokens continuously, NIM drops the streaming connection with "peer closed connection without sending complete message body". Mitigations: `max_tokens=4096` for deep sectors (halves the output budget), IMMEDIATE FIRST ACTION (forces tool call before reasoning), `text_max_chars=3000` (limits exa payload feeding into reasoning). Do NOT raise any of these limits for deep sectors without testing.
- **`intellectual_journals` must span ≥3 different publications.** The DIVERSITY RULE in the instruction is enforced by the prompt, not code. If you see all results from NYRB + New Yorker again, the 3-parallel exa search instruction wasn't followed — check that Kimi saw all three numbered calls in the instruction and dispatched them.
- **The research quota guard uses provider `used` count delta, not absolute counts.** `_quota_snapshot` is taken before the agent runs; `_quota_increased` checks if any provider's count increased. If a sector's agent calls only the `fetch_new_yorker_talk_of_the_town` tool (not in quota), the guard fires and returns the default. `_NO_QUOTA_CHECK = frozenset({"newyorker"})` exempts that sector. If you add a new sector that uses only non-quota tools, add it to `_NO_QUOTA_CHECK`.
- **The 65s TPM sleep is conditional, not unconditional.** `_invoke_write_llm` returns `(text, used_groq: bool)`. `generate_briefing` only sleeps 65s before a call if the *previous* call used Groq. Once Groq TPD is exhausted and NIM takes over, the sleep is skipped for every subsequent inter-part gap. If you refactor the loop, preserve the `last_used_groq` flag — without it the pipeline wastes ~9 minutes of sleep on NIM-fallback runs and will breach the 60-min workflow timeout under extreme NIM latency.
- **Groq TPD (tokens-per-day) limit = input_tokens + max_tokens_requested per call.** The free tier is 100k tokens/day. With max_tokens=8192 × 9 write calls, write alone would need ~110k tokens (input ~37k + output budget ~74k), blowing the daily limit. Default is now max_tokens=4096 per call: each part targets 500–900 words (~700–1200 output tokens), 4096 gives a 3.4× margin and matches NVIDIA NIM's native output cap for meta/llama-3.3-70b-instruct. Total daily write budget: ~73k tokens; plus correspondence Groq call: ~9k; grand total ~82k — within 100k. If you raise max_tokens above ~5000, the production pipeline will fail daily at Part 2 (or fall through to NIM, which has its own throttle).

## Dev branch

- **Current**: `main` (all PRs merged, clean)
- Prior: `claude/guaranteed-sector-retry-sprint8` (PR #54 merged)
- Prior: `claude/forensic-fixes-sprint6` (PR #53 merged)

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
