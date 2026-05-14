# jeeves-unchained | for-AI-parsing | optimize=compliance

Full project docs (phase table, model split, flags, secrets, Gmail OAuth, schema) live in README:

@README.md

---

<state>
branch: main (uncommitted local changes for GATE-A) | date: 2026-05-14 | tests: 10/10 passing for test_write_empty_guard.py (8 unit + 2 e2e subprocess).
gate-a-2026-05-14: scripts/write.py NEW helper _session_research_empty + GATE-A in main() (exit 5 when every non-TOTT sector empty). Override: JEEVES_FORCE_WRITE_EMPTY=1. Tests/test_write_empty_guard.py (10 tests). Triggered by 2026-05-13 incident: pipeline shipped 7 fabricated URLs (congress.gov, pentagon.gov, etc.) on an all-empty session. Auditor stripped URLs; briefing still shipped with invented content. GATE-A blocks future occurrences.
k2.6-protocol-vindicated-2026-05-14: 4 independent probes proved K2.6 protocol works in streaming + non-streaming. probe_agent_path_v2.py with full jeeves stack produced 16 real tool dispatches with proper kwargs (serper×8, tavily×6, gemini×1, exa×1). Empty-research on 2026-05-13 was transient (NIM blip or rate-limit), NOT a permanent protocol bug. DO NOT build jeeves/model_router.py as HANDOFF.md Plan A described — solves a phantom problem. The "leak" strings in dedup.covered_headlines (functions.tavily_extract:5 etc.) are K2.6's NATIVE tool_call IDs, not text-form tool calls — handoff misread its own evidence. The "None/empty arguments; coercing to {}" warnings in production logs are mid-stream chunk noise — get_tool_calls_from_response is called per-streaming-chunk and early chunks naturally have empty args. Probes left in repo root: probe_kimi_protocol.py (HTTP non-stream), probe_streaming.py (raw SSE), probe_openai_sdk.py (openai SDK), probe_agent_path.py, probe_agent_path_v2.py (full stack + instrumentation), run_probe.sh (driver). Useful for the next mystery; can move to scripts/diagnostics/ or git-rm before PR.
optional-followups: (1) downgrade jeeves/llm.py "None/empty arguments" warnings to DEBUG — pure mid-stream noise. (2) upstream PR to llama-index-llms-nvidia adding moonshotai/kimi-k2.6 to MODEL_TABLE — non-blocking. (3) jeeves-memory OAuth dead per 2026-05-13 inbox sweep — separate repo, user's problem to fix.
last-push: PR #79 (sprint 16) merged. Sprints 17, 18, 19-slice-D, 19-slice-E PRs still forthcoming. GATE-A PR forthcoming.
sprint-19-slice-e-2026-05-05: tier choreography + observable + eval. NEW jeeves/tools/telemetry.py: emit(event, **fields) → sessions/telemetry-<utc-date>.jsonl, gated by JEEVES_TELEMETRY=1. Module-level lock + atexit close + JEEVES_TELEMETRY_DIR override (used in tests). Default-off; cheap early return when flag absent. NEW jeeves/tools/rate_limits.py: tier-tagged threading.BoundedSemaphore per provider. T1 (8): serper, exa, jina_search. T2 (4): tavily, gemini_grounded, vertex_grounded, jina_rerank. T3 (1): jina_deepsearch, tinyfish, tinyfish_search, playwright, playwright_search, firecrawl. JEEVES_RL_<PROVIDER>=N override read once at first acquire. acquire() context manager — emits semaphore_wait_ms when wait≥50ms. NEW jeeves/tools/search_shadow.py: maybe_run_shadows() fires 1-3 peers in parallel after serper_search returns. Three flags: JEEVES_JINA_SEARCH_SHADOW, JEEVES_TINYFISH_SEARCH_SHADOW, JEEVES_PLAYWRIGHT_SEARCH_SHADOW. ThreadPoolExecutor (8s timeout). Writes sessions/shadow-search-<provider>-<date>.jsonl with primary_urls/shadow_urls/jaccard/latency-delta. Production output bit-identical to vanilla run regardless of shadow result. _SHADOW_RUNNERS uses runner *names* (str) not callables so monkeypatching the module attribute reaches the dispatcher. WIRED telemetry+semaphores into serper.py/tavily.py/exa.py/gemini_grounded.py/vertex_search.py/jina.py (3 fns)/tinyfish.py (extract+search)/playwright_extractor.py (search). Each call emits tool_call telemetry with provider/query/ok/status/results/latency_ms/error. EXTENDED jeeves/tools/__init__.py: TOOL_TAXONOMY constant maps 15 tool names → {role, tier, billing}. Roles: web_search, semantic_search, deep_research, rerank, extract, curated_feed. tools_for_role() helper. No tool renames (NIM tool-pick is description-keyed; renames break tests + research_sectors). EXTENDED jeeves/prompts/research_system.md: taxonomy block + role table inserted before decision tree. NEW scripts/mine_golden_set.py: reads sessions/session-*.json (last N days), harvests (sector, query-template, urls) tuples for 6 mineable sectors (local_news, global_news, intellectual_journals, career, family, wearable_ai). Drops sectors with <2 URLs. Writes tests/fixtures/search_eval_set.yaml. Deterministic + idempotent. First mine extracted 48 cases from 11 days. NEW scripts/eval_search.py: per-(provider, case) recall@10 + latency + cost CSV. Mirrors eval_extractors.py shape. Dry-run mode. Manual yaml reader (pyyaml optional). Per-provider summary: success_rate, mean_recall_at_10, p50/p95 latency, total_cost_usd. NEW tests: test_telemetry.py (4), test_rate_limits.py (6: env override, default tiers, serialisation, slow-acquire telemetry), test_search_shadow.py (6: no-flag default, 3 flag-specific writes, crash isolation, jaccard zero-disjoint), test_mine_golden_set.py (5), test_eval_search.py (5). All hermetic — no real HTTP. Production effect on default vanilla run: zero (telemetry off, all shadow flags unset, semaphores oversized for sequential _SECTOR_SEMAPHORE=1).
sprint-19-search-canaries-2026-05-05: 6-agent /plan synthesis → ship slice D first (skeleton 5 new search-agent tools, all default-off behind individual env flags). NEW jeeves/tools/jina.py: make_jina_search (s.jina.ai, X-Engine=direct, X-Return-Format=json), make_jina_deepsearch (deepsearch.jina.ai/v1/chat/completions, OpenAI-shape, jina-deepsearch-v1, low effort default), make_jina_rerank (api.jina.ai/v1/rerank, jina-reranker-v2-base-multilingual, CSV-string→list coercion for NIM-safe args). Module-level _HTTP_CLIENT (timeout=20s) + _DEEPSEARCH_CLIENT (120s, multi-hop loops). All return json.dumps; 429 → push counter to cap (gemini pattern). EXTENDED jeeves/tools/tinyfish.py: search(query, num=10, include_raw_content=False, site=None) hits /v1/search; same auth/cap/429 pattern as extract_article. EXTENDED jeeves/tools/playwright_extractor.py: search(query, engine='ddg'|'bing'|'brave', num=10) using existing JS-singleton context, parsers per-engine (_parse_ddg with /l/?uddg= unwrap, _parse_bing li.b_algo, _parse_brave .snippet), _PARSERS dict (test gotcha: monkeypatch.setitem not setattr — captured at import time). UPDATED jeeves/tools/quota.py: DEFAULT_STATE += {jina_search:6000/0.20, jina_deepsearch:300/5.00, jina_rerank:3000/0.20, tinyfish_search:250/24.00, playwright_search:9999/0.00}. DAILY_HARD_CAPS += {jina_search:200, jina_deepsearch:20, jina_rerank:100, tinyfish_search:8, playwright_search:60}. NEW _AUX_PROVIDERS set excludes canaries from cheapest_with_capacity (preserved historical "serper is cheapest" semantics — required to not break test_quota_ledger). UPDATED jeeves/tools/__init__.py: 5 new tools registered behind individual JEEVES_USE_{JINA_SEARCH,JINA_DEEPSEARCH,JINA_RERANK,TINYFISH_SEARCH,PLAYWRIGHT_SEARCH}=1 flags, all default-off. CHOOSE WHEN/PREFER OVER X WHEN/DO NOT USE descriptions (per agent 5 plan — Kimi tool-pick is dominated by description text). _make_tinyfish_search_tool + _make_playwright_search_tool wrappers (json.dumps returns, ledger threading, fail-soft). UPDATED config.py: RESEARCH_BUDGET_JINA_SEARCH=10, _JINA_DEEPSEARCH=3, _JINA_RERANK=8, _TINYFISH_SEARCH=8, _PLAYWRIGHT_SEARCH=20. UPDATED prompts/research_system.md: sprint-19 canary block + budget-table rows (annotated "if registered"). NEW tests/test_jina_tools.py + test_tinyfish_search.py + test_playwright_search.py: 20 hermetic tests (no real HTTP, 429 cap-bump verified, NIM-safe coercion verified, env-flag gating verified). Production effect on default vanilla run: zero. Next slice: tier-choreography rate-limit semaphores, eval_search.py harness + golden-set miner from sessions/, three new shadow flags (JEEVES_*_SEARCH_SHADOW=1), naming taxonomy refactor, telemetry JSONL.
sprint-18-tinyfish-2026-05-05: added jeeves/tools/tinyfish.py (firecrawl-shaped client, 0.8 default quality_score). DEFAULT_STATE["tinyfish"] free_cap=100 + DAILY_HARD_CAPS=30. Two flags: JEEVES_TINYFISH_SHADOW=1 fires TinyFish in parallel via ThreadPoolExecutor inside playwright_extractor.extract_article (now a thin wrapper around _extract_article_core), appends comparison record to sessions/shadow-tinyfish-<date>.jsonl. JEEVES_USE_TINYFISH=1 inserts tinyfish between trafilatura and playwright in enrichment.fetch_article_text AND registers tinyfish_extract agent tool. Both default-off — no production effect on a vanilla run. Eval harness: scripts/eval_extractors.py + tests/fixtures/extractor_eval_set.yaml (15 URLs across paywall/SPA/PDF/etc; golden_text fragments TBD on first-run bootstrap). EVAL_GATE.md documents promotion thresholds + rollback triggers. scripts/quota_report.py prints rolling spend including shadow-jsonl counts. tests/conftest.py autouse-stubs tinyfish.extract_article when no key in env. daily.yml plumbs TINYFISH_API_KEY (secret) + JEEVES_*_TINYFISH (vars) and commits shadow jsonl alongside .quota-state.json.
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
  fetch-chain: httpx+trafilatura → Jina(r.jina.ai) → [tinyfish if JEEVES_USE_TINYFISH=1] → playwright_extractor
  playwright-first: newyorker (JS-heavy — skip Jina entirely)
  playwright-trigger: Jina len<300 OR paywall markers → escalate to tinyfish (opt-in) then playwright
  playwright-dep: optional; soft-fails if absent; quota tracked under "playwright" key
  tinyfish-flags: TINYFISH_API_KEY + JEEVES_USE_TINYFISH=1 (active) | JEEVES_TINYFISH_SHADOW=1 (parallel observe-only, writes sessions/shadow-tinyfish-<date>.jsonl)
  tinyfish-quota: DAILY_HARD_CAPS["tinyfish"]=30 | enforced via ledger.check_daily_allow inside extract_article

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
jeeves/tools/playwright_extractor.py  → article-fetch fallback (NEW sprint-13) | extract_article=public wrapper, _extract_article_core=body (sprint-18)
jeeves/tools/tinyfish.py              → TinyFish managed extractor (NEW sprint-18; opt-in via JEEVES_USE_TINYFISH=1)
jeeves/tools/__init__.py              → playwright_extract + tinyfish_extract (sprint-18) registered as Kimi agent tools
scripts/eval_extractors.py            → extractor comparison harness (sprint-18) — reads tests/fixtures/extractor_eval_set.yaml
scripts/quota_report.py               → rolling spend digest incl. shadow-jsonl counts (sprint-18)
EVAL_GATE.md                          → TinyFish promotion thresholds + rollback triggers (sprint-18)
jeeves/prompts/write_system.md        → ~55 aside phrases (profanity pool)
jeeves/prompts/research_system.md     → updated with playwright context
jeeves/config.py                      → Config.from_env(), MissingSecret
jeeves/schema.py                      → SessionModel + FIELD_CAPS
.github/workflows/daily.yml           → PRIMARY cron (0 12); jobs: correspondence→research→write
.github/workflows/correspondence.yml  → manual-dispatch only
</nav>
