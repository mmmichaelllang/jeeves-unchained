# jeeves-unchained | for-AI-parsing | optimize=compliance

Full project docs (phase table, model split, flags, secrets, Gmail OAuth, schema) live in README:

@README.md

---

<state>
branch: sprint-17-write-nim-or-fallback | sprint: 17 (write tier-3 fallback + TPM bloat fix + repetition/pablum overhaul) | date: 2026-05-05 | tests: 223 in-scope tests passing (pre-existing llama_index sandbox failures unchanged)
last-push: PR #79 (sprint 16) merged. Sprint 17 PR forthcoming.
sprint-17-repetition-pablum-2026-05-05: User-reported 3-finding overhaul — same articles repeating across runs, duplicated sections, pablum-vs-description. F2.a: new _dedup_urls_across_blocks post-stitch pass + _canonical_url helper (strips utm_*, fbclid, gclid, fragments, normalises trailing slash + scheme/host case). Wired AFTER _dedup_paragraphs_across_blocks; drops <p> blocks whose every URL citation is cited more richly elsewhere — guarantees no unique URL is lost. F1.b: tavily_search now accepts time_range; exa_search now accepts start_published_date YYYY-MM-DD; FRESHNESS WINDOW directive in CONTEXT_HEADER mandates qdr:w / time_range='week' / start_published_date={seven_days_ago} for non-breaking sectors. F1.c: {seven_days_ago} placeholder injected into prompt context (computed from cfg.run_date). F1.a: covered_sources_by_host(session) — host → titles map; threaded through _run_sector_loop → run_sector → _build_user_prompt; surfaces "Source-rotation hints" block (cap 30 hosts × 3 titles) so agent can prefer different article from same source day-over-day. F3.b/e: Sparse sector rule HARD overrides word counts; per-h3 paragraph floor relaxed to "proportional to source"; 5000-word briefing floor dropped — length is now ceiling not floor; per-part word targets logged as ceilings (warn on >140%, not on <60%); SYNTHESIS CLOSE → OPTIONAL; WIT QUOTA → permitted not required. _EDITOR_WORD_FLOOR_RATIO 0.80→0.70. F3.a: Description-first rule — first 60% of every article writeup must describe (what/who/when/where), interpretation second; explicit GOOD/BAD example added. New tests: test_url_dedup.py (10 cases), test_dedup.py +2 covered_sources_by_host cases.
sprint-17-tpm-bloat-fix-2026-05-04: 04:15 daily run had ALL 9 parts skip Groq (input ~12-15k tok > 12k TPM ceiling) → fall through to NIM → NIM hallucinates full briefings on Part1/Part9 (h3 budget collapse, structural orphan, signoff missing). Root cause: research_sectors.collect_headlines_from_sector pulls "first two sentences" of findings (200-300 chars/entry); 250 such entries × 80 chars/entry = 20k+ chars per user prompt. Fixes: (1) DEDUP_PROMPT_HEADLINES_CAP 250→150 in config.py. (2) DEDUP_PROMPT_TOPICS_CAP 80→60. (3) Per-headline truncation to 80 chars in _trim_session_for_prompt. After: all parts run 7.9-10.9k tokens (was 12-15k), Groq handles all 9. (4) OpenRouter narrative editor: dropped openrouter/auto (402s without credits, requested 16384 tok), added qwen-2.5-72b-instruct + mistral-small-3.1-24b free fallbacks, max_tokens 16384→8192, defensive None-guard for resp.choices/message/content (nemotron 200-with-None crashes), 4s sleep on 429 between OR fallback attempts.
sprint-14-added: generate_briefing 4-tuple return (html, quality_warnings, groq_parts, nim_parts) | postprocess_html quality_warnings kwarg | _write_run_manifest moved to scripts/write.py | _re NameError fixed | RunManifest.from_briefing_result dynamic total_parts | quality sprint (33 files, ~3500 lines) | test_research_sectors excluded quality_warnings
hotfix-2026-05-03: classify_with_kimi NIM circuit breaker + 60s timeout (was 180s) — prevents 60min cancellation when NIM hangs every call. First batch tries NIM with 1 timeout-retry (30s sleep); on second failure → Groq AND trips nim_dead so remaining batches skip NIM. Rate-limit retries kept at 60s+120s (3 attempts) since 429s clear within window.
sprint-15-tott-fix-2026-05-04: 2026-05-04 silent TOTT loss in briefing (Part 9 model wrote prose summary instead of intro+placeholder; both injection paths failed → verbatim text silently dropped). Two-layer hardening: (1) _ensure_tott_scaffolding() pre-injects intro+placeholder+read-link before raw_part assignment so refine + post-stitch see them; (2) _inject_newyorker_verbatim FORCE-INJECT path: when intro marker missing AND signoff present, splice TOTT block before <div class="signoff"> rather than returning unchanged. Quality-warning surface: part9_tott_scaffolding_injected.
sprint-15-playwright-rewrite-2026-05-04: jeeves/tools/playwright_extractor.py major hardening (5 parallel research agents → consensus picks). Patchright import preferred (Runtime.enable CDP-leak fix, --enable-automation stripped, navigator.webdriver removed) with vanilla playwright fallback. Module-level singleton browser+context+atexit (~1.5-2s saved per fetch × 50 fetches = 75-100s/run). Context-level route-block handler drops image/font/media/stylesheet/ad-hosts/paywall-scripts (40-60% per-page savings). JSON-LD articleBody ground truth via page.evaluate. Trafilatura main-content extraction. MutationObserver settle detection replaces hardcoded wait_for_timeout(1500). Multilingual autoconsent JS. CrystallizeResult Pydantic schema (REPRODUCE VERBATIM mandate). gemma-2-9b dropped from OR chain. _score_extraction soft-failure detection. JS-disabled second context for static-render hosts. LLM crystallizer DEFAULT OFF — opt-in via JEEVES_PW_USE_LLM_CRYSTALLIZE=1.
sprint-15-deps-2026-05-04: pyproject playwright extra now ships patchright>=1.40 + playwright>=1.40 (patchright preferred at import time). daily.yml: `playwright install --with-deps --only-shell chromium`.
sprint-17-write-tier3-2026-05-04: 14:00 daily run failed in write phase — Groq Part 1 input (~11977 tok) over TPM ceiling routed to NIM, NIM hung 12m22s before raising APITimeoutError. Fix: (1) _NIM_WRITE_TIMEOUT_S=60s (down from 180s — matches classify_with_kimi sprint-15 hotfix). (2) Module-level _NIM_WRITE_DEAD circuit breaker — once NIM times out for any part, remaining parts skip NIM and go directly to OR. (3) New 3-tier escalation: Groq → NIM → OpenRouter free-tier chain (llama-3.3-70b-instruct → qwen-2.5-72b-instruct). _try_nim_then_or() helper. _invoke_or_write() last-resort. _is_nim_timeout() classifier. Dedup verified working correctly — covered_headlines preserves recency order (today HEAD, correspondence refs TAIL). 285 covered_headlines, 36 covered_urls, only 1 cross-sector URL (intellectual_journals→enriched_articles, by design).
sprint-16-audit-fixes-2026-05-04: 8 audit fixes from forensic codebase review. (1) Restored _classify_batch_with_openrouter chain in correspondence.py (NIM+Groq+OR three-tier fallback, fires when both NIM AND Groq fail). (2) firecrawl_extractor.py ledger.increment → ledger.record + quality_score field. (3) scripts/research.py extract_correspondence_references preserves recency order (was destroying it via sorted()). (4) jeeves/email.py SMTP retry 4 attempts × 30/60/120s backoff on transient failures, permanent codes (535/550-554) NOT retried. (5) scripts/archive_old_sessions.py + .github/workflows/archive_sessions.yml weekly Sundays 06:00 UTC archives sessions/ >90 days old to sessions/archive/<YYYY>/<MM>/. (6) QuotaLedger.snapshot_used_counts() encapsulation helper + check_allow now under lock. (7) write.py _parse_all_asides cached at module level + _recently_used_asides cached per-run (saves 36 file reads + redundant regex). BANNED_TRANSITIONS substring → word-boundary regex (no more "Returning" matching "Turning to"). NIM refine warning includes exception MESSAGE. (8) session_io.load_session_by_date raises ValueError on corruption (was misleading FileNotFoundError). Banner URL deduplicated. httpx module-level clients get atexit.register(close).
</state>

<gates>

GATE-session-end:
  trigger: session advanced (new phase/branch/gotcha/behavior change)
  action: update <state> block + any changed gotchas in this file
  mechanism: no hooks/scripts — manual edit only

</gates>

<pipeline>

ENTRY:
  primary: daily.yml cron 0 12 → jobs: correspondence → research → write (sequential)
  manual: correspondence.yml / research.yml / write.yml (workflow_dispatch only; no crons)

PHASE-1 correspondence:
  flow: Gmail → Kimi classify → Groq render → HTML → sessions/correspondence-<date>.json
  profanity: NONE — removed 2026-04-28 (wrong tone for mail brief)
  dry-run-vs-fixture: both ticked → dry-run wins (scripts/correspondence.py:63)
  tell: "(DRY RUN)" in h1 = render_mock_correspondence() took dry-run path

PHASE-2 research:
  agents: Kimi K2.5 FunctionAgents on NIM | _SECTOR_SEMAPHORE=1 (sequential — NIM free tier)
  deep-sectors: triadic_ontology / ai_systems / uap → max_tokens=4096 (stream-drop prevention)
  fetch-chain: httpx+trafilatura → Jina(r.jina.ai) → playwright_extractor (fallback order)
  playwright-first: newyorker (JS-heavy — skip Jina entirely)
  playwright-trigger: Jina len<300 OR paywall markers → escalate to playwright
  playwright-dep: optional; soft-fails if absent; quota tracked under "playwright" key

PHASE-3 write:
  draft: Groq llama-3.3-70b-versatile × 9 sequential (65s TPM sleep if prev-call=Groq)
  refine: NIM meta/llama-3.3-70b-instruct × 9 concurrent background threads
  final: OpenRouter narrative editor × 1 on full stitched doc
  openrouter-chain: nvidia/nemotron-3-super-120b-a12b:free → meta-llama/llama-3.3-70b-instruct:free → google/gemma-4-31b-it:free → openrouter/auto
  newyorker: Part9 outputs <!-- NEWYORKER_CONTENT_PLACEHOLDER --> ; _inject_newyorker_verbatim replaces (code guarantee — model never copies text)
  max_tokens: 4096 default (NIM cap; >5000 blows Groq 100k/day TPD)
  dedup-asides: day-over-day (last 4 days briefings) + within-run (accumulated per-part via run_used_asides=)
  dedup-topics: within-run used_topics_this_run; regex extracts proper nouns/quoted titles/named acts

LOG-TELLS:
  openrouter-absent: "OpenRouter narrative edit" line missing → OPENROUTER_API_KEY not in secrets
  nim-refine-absent: "NIM refine [partN]" lines missing → NVIDIA_API_KEY missing
  newyorker-absent: placeholder not found → WARNING logged; text simply missing (no hallucination)

SESSION-FILES:
  real: sessions/session-*.json | sessions/correspondence-*.json | sessions/briefing-*.html (committed)
  gitignored: sessions/*.local.json | sessions/*.local.html (dry-run only)
  handoff: correspondence-<date>.json consumed by research as session.correspondence — don't break filename/schema contract

</pipeline>

<nim-gotchas>

tool_kwargs:
  bug: ToolCallBlock.tool_kwargs={} (empty dict) → NIM "Extra data: line 1 col 3" 400
  fix: _normalize_tool_kwargs converts {} → "{}" | dict → json.dumps(dict)
  CRITICAL: call from astream_chat_with_tools NOT achat_with_tools (FunctionAgent always streaming=True; achat path is dead code)

function-args:
  bug: tool_call.function.arguments=None/dict → NIM pydantic "Input should be valid string" 400
  fix: get_tool_calls_from_response sets tool_call.function.arguments="{}" on None/empty

tool-returns:
  rule: ALL search tools return json.dumps() — never return dict
  reason: LlamaIndex str(dict) → single-quote repr → NIM "Unterminated string" 400
  files: serper.py / tavily.py / exa.py / enrichment.py / gemini_grounded.py / fetch_article_text

degenerate-calls:
  bug: tool_call.id=None or function.name=None → pydantic crash + NIM 400 on next call
  fix: skip in get_tool_calls_from_response (WARNING) + strip from additional_kwargs["tool_calls"] before NIM send

429-backoff:
  fix: max_retries=0 in build_kimi_llm (disables SDK retry amplification)
  retry: _is_nim_rate_limit() → 60/120s sector-level backoff (run_sector owns all retries)
  triadic_ontology: forced-retry every run (~10s overhead) — Kimi uses training data; expected behavior
  uap: occasional 60/120s sleep — correct behavior; within 15-min research window

stream-drop:
  threshold: ~20-25s continuous output → "peer closed connection"
  mitigations: max_tokens=4096 deep sectors | IMMEDIATE FIRST ACTION directive | text_max_chars=3000
  DO-NOT: raise these limits for deep sectors without testing

</nim-gotchas>

<groq-gotchas>

TPM-clamp:
  fix: _clamp_groq_max_tokens() = min(max_tokens, available=12000-input_tokens-600)
  why: system prompt grows part-over-part; by part4 input≈8500tok; 4096 output breaches 12000 TPM ceiling

TPD-budget:
  limit: 100k tokens/day free tier
  spend: ~73k write (9×~8k) + ~9k correspondence = ~82k total
  HARD-LIMIT: max_tokens≤5000 or pipeline fails daily at Part 2 (falls to NIM)

sleep-logic:
  rule: 65s sleep only before a call where previous call used Groq (preserve last_used_groq flag)
  skip: NIM-fallback path skips all sleeps
  CRITICAL: refactoring write loop must preserve this flag or pipeline breaches 60min daily.yml timeout

</groq-gotchas>

<gemini-gotchas>

cap: DAILY_HARD_CAPS["gemini_grounded"]=12 (free tier RPD=20; paid-tier assumption of 1490 was wrong)
429-exhaust: on 429 → immediately set counter to cap → all subsequent sectors skip Gemini
redirect-urls: vertexaisearch.cloud.google.com/* → ephemeral URLs, cannot dedup by URL
redirect-filter: _REDIRECT_ARTIFACT_HOSTS in collect_urls_from_sector — DO NOT add redirect domains back

</gemini-gotchas>

<json-gotchas>

repair-order: _try_normalize_json (4 deterministic): python-repr→json | trailing-comma | truncation-recovery | bare-obj→array
escalate: truly unrecoverable → _json_repair_retry (LLM reformat; or synthesize from sector instruction when raw is empty)
sentinel: _ParseFailed returned on structural failure (not spec.default)

dedup-write:
  match: headline-matched (NOT URL-matched) via dedup.covered_headlines in write phase
  cap: DEDUP_PROMPT_HEADLINES_CAP=250 (write.py:141) — session has ~205 same-day headlines
  cross-sector: session.dedup.cross_sector_dupes stores URLs appearing in 2+ sectors

</json-gotchas>

<research-gotchas>

quota-guard: snapshot before/after agent run; sector rejected if no search provider called (hallucination prevention)
no-quota-check: frozenset {"newyorker"} — add new non-quota-tool sectors here
exa-invisible: exa_py uses requests not httpx; infer from quota ledger delta or presence of exa.ai URLs in session JSON
journals-diversity: ≥3 different publications required (prompt-enforced, not code-enforced)

</research-gotchas>

<nav>
scripts/correspondence.py:59          → _run mode dispatch (dry-run / use-fixture / real Gmail)
scripts/correspondence.py:63          → dry-run wins when both flags set
scripts/correspondence.py:97          → main + flag parsing + artifact writes
jeeves/correspondence.py:470          → render_mock_correspondence (dry-run template)
jeeves/write.py:141                   → DEDUP_PROMPT_HEADLINES_CAP
jeeves/write.py:1019                  → PART8 Library Stacks
jeeves/write.py:3032                  → used_topics_this_run within-run topic tracking
jeeves/llm.py                         → get_tool_calls_from_response (function.args normalization)
jeeves/tools/playwright_extractor.py  → article-fetch fallback (NEW sprint-13)
jeeves/tools/__init__.py              → playwright_extract registered as Kimi agent tool
jeeves/prompts/write_system.md        → ~55 aside phrases (profanity pool)
jeeves/prompts/research_system.md     → updated with playwright context
jeeves/config.py                      → Config.from_env(), MissingSecret
jeeves/schema.py                      → SessionModel + FIELD_CAPS
.github/workflows/daily.yml           → PRIMARY cron (0 12); jobs: correspondence→research→write
.github/workflows/correspondence.yml  → manual-dispatch only
</nav>
