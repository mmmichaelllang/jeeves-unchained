# systematic-debugging — 2026-05-05

Three quality complaints from user on briefing output:

1. Same articles/sources recurring across runs. Wants source-rotation: covered article X from source Y yesterday → today pick next-best article from same source Y.
2. Duplicated sections within briefing.
3. Too much commentary/pablum, not enough article description.

Phase 1 evidence below. No fixes applied. Severity is editorial-impact, not crash risk.

---

## Finding 1 — Same articles repeat. Dedup is exact-string only. No source rotation. No freshness filter on deep-sector search.

**Severity: HIGH** (recurring user-visible degradation, runs daily)

**Evidence chain.**

- `jeeves/dedup.py:8-45` — `covered_urls()` and `covered_headlines()` return only URL strings and headline strings. No source/publisher tracking. No topic clustering.
- `scripts/research.py:417-440` — caller walks 7 prior sessions, builds `prior_urls_ordered` (newest-first, 150 cap) and `prior_hl` (set). Both passed to `_run_sector_loop` and from there to `_build_user_prompt`.
- `jeeves/research_sectors.py:381-385` — prior URLs land in CONTEXT_HEADER as `Prior coverage URLs (already briefed, do not revisit)`. Instruction: `if you encounter any URL in the prior list above, skip it.` That blocks exact same URL. Does not block same article via different URL, same article via Google AMP, or different article on same topic from same source.
- `jeeves/tools/tavily.py:15-71` — `tavily_search` wrapper passes `query`, `max_results`, `search_depth`. Does NOT pass `time_range` (day/week/month) or `start_published_date`. Tavily SDK supports both. Default Tavily ranking favors evergreen high-authority pages → same top results day after day for same query.
- `jeeves/tools/serper.py:25-71` — `serper_search` wrapper exposes `tbs` parameter (`qdr:d`, `qdr:w`). System prompt at `research_sectors.py:1014-1018` only suggests `tbs='qdr:d'` for `breaking/local/time-filtered` sectors. Deep sectors (`intellectual_journals`, `triadic_ontology`, `ai_systems`, `uap`, `wearable_ai`) run with no freshness filter → SERP returns evergreen articles → same articles re-rank into top-N daily.
- `jeeves/research_sectors.py:60-352` — sector instructions hardcode fixed search queries per sector. No date stamp, no week-of-year, no rotation seed. `serper_search(query='world news today')` returns near-identical SERP across runs.
- `jeeves/research_sectors.py:362` — only `literary_pick` instruction contains `Do NOT pick a title already in dedup.covered_headlines. Vary the selection day-over-day.` No equivalent on `intellectual_journals`, `wearable_ai`, `triadic_ontology`, `ai_systems`, `global_news`. Even where present, enforcement = model compliance.
- No `covered_sources` field anywhere. No `host_to_last_url` map. No instruction telling agent: `if you see source X, find a different article from source X than the one in covered_urls`.

**Root cause.** Dedup is keyed on URL/headline strings. SERP/Tavily results are deterministic for fixed queries against unchanging corpora over short windows. Without a freshness filter, agents resurface the same evergreen pieces daily; URL dedup catches a small fraction (only when literal URL matches). User's stated need — *next most relevant article from same source* — is not represented in the data model at all. Agent has no concept of "source X was used yesterday for article A; pick article B from source X today."

**Files implicated:**
- `jeeves/dedup.py` (data model gap)
- `jeeves/tools/tavily.py:41-46` (missing `time_range`)
- `jeeves/tools/serper.py:44-46` (tbs not enforced for deep sectors)
- `jeeves/tools/exa.py` (need to verify; likely missing `start_published_date`)
- `jeeves/research_sectors.py:60-352` (fixed queries, no rotation seed; `literary_pick`-style "vary day-over-day" not propagated to other sectors)
- `scripts/research.py:417-440` (prior_urls only — no prior_sources/prior_titles_per_source)

---

## Finding 2 — Duplicated sections. Cross-part dedup is text-shape only, not URL/concept-based.

**Severity: HIGH** (visible content regression; partially mitigated already, gaps remain)

**Evidence chain.**

- `jeeves/write.py:336-346` — PART_PLAN deliberately overlaps payloads:
  - part4 reads `global_news` + `newyorker_hint`.
  - part5 reads `intellectual_journals` + `enriched_articles` (enriched = deepened version of journal URLs by design).
  - part7 reads `wearable_ai` + `newyorker_hint` + `literary_pick`.
  - Same URL can appear in 2+ sectors. Same topic frequently appears in 2+ sectors when surfaced by different search queries.
- `jeeves/research_sectors.py:1298-1330` — `_find_cross_sector_dupes` computes URLs in 2+ sectors. Surfaced in `dedup.cross_sector_dupes`. Enforcement at `jeeves/write.py:552` is **prompt-only** (CONTINUATION_RULES rule 10): "If a URL you are about to cite is in that list AND another part has already cited it ... do NOT re-narrate." Model often ignores.
- `jeeves/write.py:2728-2767` — `_collapse_adjacent_duplicate_h3` strips literal-same-text adjacent `<h3>` headers. Does not catch non-adjacent or differently-worded same-topic sections.
- `jeeves/write.py:2882-2966` — `_dedup_h3_sections_across_blocks` matches by exact lowercased h3 text. Two sections on same topic with different `<h3>` strings (or no h3 at all in parts 1/3/9) survive.
- `jeeves/write.py:2807-2879` — `_dedup_paragraphs_across_blocks` Jaccard 4-word shingles, threshold 0.5. Catches near-identical prose copied into two parts. Different paragraphs describing the same article (different verbs, different intros, but same underlying piece) score well below 0.5 and pass through.
- No URL-keyed cross-part dedup pass exists. If part4 cites `guardian.com/X` and part5 also cites `guardian.com/X` in different prose, both survive postprocess.
- `newyorker_hint` shipped to part4 AND part7 with one-sentence directive (write.py:811-821, 1080-1092). Two parts each told to write one sentence about the same NYR overlap topic → two stub paragraphs in the briefing about the same overlap.

**Root cause.** Architecture splits one document into 9 isolated LLM calls with overlapping sector payloads. Coordination relies on prompt directives ("stay in your lane", "respect cross_sector_dupes"). Deterministic post-stitch dedup is shape-based (h3 text identity, Jaccard prose similarity). It does not key on URL or canonical headline, which is the actual concept of "same content." Result: same story narrated by two parts under different headers in different prose survives every guard.

**Files implicated:**
- `jeeves/write.py:336-346` (PART_PLAN overlap design)
- `jeeves/write.py:2807-2966` (post-stitch dedup is shape-only, not URL-based)
- `jeeves/research_sectors.py:1298-1330` (cross_sector_dupes computed but enforcement is prompt-only)
- `jeeves/write.py:811-821` and `:1080-1092` (newyorker_hint plumbed to two parts with no deterministic pruner)

---

## Finding 3 — Pablum vs description. Word-count floors and "synthesis close" rules force commentary when raw article material is small.

**Severity: MEDIUM-HIGH** (user-visible voice problem, structural)

**Evidence chain.**

- `jeeves/write.py:353-363` — per-part word targets sum to ~3000-4500. `write_system.md:24` adds hard floor of 5000 words for the briefing.
- `jeeves/schema.py:137-159` — FIELD_CAPS bound source material per item:
  - `global_news.findings`: 600 chars (~100 words)
  - `intellectual_journals.findings`: 600 chars
  - `wearable_ai.findings`: 400 chars
  - `enriched_articles.text`: 1200 chars (~200 words)
  - `triadic_ontology.findings`: 1000 chars
- 5 items × 600 chars ≈ 500 words of raw article material per sector vs. 600-800 words asked of the writer per part. Gap fills with commentary by force.
- `jeeves/write.py:766-775` (part3 SYNTHESIS CLOSE), `:936-946` (part5), `:1022-1036` (part6) — every section required to end with a closing observation. Even when the rule says "must name a title/method/author," in practice the model writes connective prose.
- `jeeves/write.py:557-567` (CONTINUATION_RULES rule 11) — "WIT QUOTA. At least one sardonic, wry, or darkly humorous observation per part." Pushes commentary count up.
- `jeeves/prompts/write_system.md:65-67` — every `<h3>` section "must contain at least three substantive paragraphs (≥25 words each, naming specific entities)." With 1-2 articles per sector, the third paragraph is necessarily commentary.
- `jeeves/write.py:3096-3108` (OpenRouter editor A0.3-A0.5) — "word-count floor 80% of input" + "section-density floor: every h3 ends with at least 3 substantive paragraphs (≥25 words, ≥1 entity)." Editor cannot delete commentary down to "just describe the article" without violating its own preservation rules.
- `jeeves/write.py:1715-1852` (`_REFINE_SYSTEM`) and `:3064-3400+` (narrative editor system) — both prompts are massive blocklists of *banned* phrases. Reactive only. No positive directive of the form "raw article description must outweigh interpretation N:1," "first paragraph of every article writeup must be what-and-when before why-it-matters," or "if source material is <300 words, write a shorter section, do not pad."
- The `Sparse sector rule` exists in `write_system.md:21` but is overridden in practice by the per-part word target + "≥3 substantive paragraphs per h3" + "wit quota."

**Root cause.** Word-count floors + section-density floor + synthesis-close + wit quota + minimum-3-paragraphs-per-h3 form an interlocking incentive structure that pulls the model away from description toward interpretation when raw article material is small. Banned-phrase lists block specific tics but cannot rebalance the description-vs-commentary ratio because the structural incentives still demand the same total word count.

**Files implicated:**
- `jeeves/write.py:353-363` (_PART_WORD_TARGETS)
- `jeeves/prompts/write_system.md:21-24, 65-67` (sparse-sector rule + 5000-word floor + 3-paragraph-per-h3 floor — internal contradiction)
- `jeeves/write.py:766-775, 936-946, 1022-1036` (SYNTHESIS CLOSE per part)
- `jeeves/write.py:557-567` (WIT QUOTA in CONTINUATION_RULES)
- `jeeves/write.py:3096-3108` (OpenRouter editor section-density floor)
- `jeeves/schema.py:137-159` (FIELD_CAPS — limits raw material)

---

## Cross-cutting observations

- All three issues share a pattern: **prompt directives load-bearing where deterministic code should be.** Source rotation, cross-part URL dedup, description/commentary ratio — all delegated to model compliance through long prose rules and ever-growing banned-phrase lists. Model partial compliance × 9 parts × daily run = visible regression.
- Sprint 17 state notes already acknowledge this trajectory: dedup caps cut 250→150, headlines truncated to 80 chars to fit Groq TPM ceiling — but those are bandwidth fixes, not concept fixes. Adding more rules to the prompt costs tokens without raising compliance.
- The `Iron Law` test: every fix proposal must address a root cause, not a symptom. None of the three findings have been root-caused-and-fixed in the codebase yet — they have been mitigated by prompt enforcement and post-stitch shape-dedup, both of which leak.

---

## Proposed fix candidates (NOT applied — awaiting your selection)

For each finding the proposal is described at the level of *what would change* — not yet at the level of code diffs. After you pick which to address I will produce diffs for each and show them before applying.

### Finding 1 fixes

- **F1.a (data model)** — Add `dedup.covered_sources: dict[str, list[str]]` mapping `host → [titles cited yesterday]`. Build it in `scripts/research.py:417-440` from the same prior_sessions walk. Surface in research prompt: `Source rotation — for these hosts you cited yesterday, find a different topic today: {host: [titles]}`. Cost: ~1-2k extra prompt chars; one new schema field.
- **F1.b (search-tool freshness)** — Wire Tavily SDK's `time_range="week"` (default) and `time_range="day"` (breaking sectors) into `jeeves/tools/tavily.py`. Add a `tbs="qdr:w"` default for deep sectors in the research prompt. Verifies Exa's `start_published_date` similarly. Cost: small wrapper changes; possibly fewer high-quality evergreen results — needs A/B check.
- **F1.c (query rotation seed)** — Inject `cfg.run_date.isoformat()` or week number into a portion of sector queries (e.g., `world news week of {date}`, `AI research published this week`). Forces SERP variation. Cost: query-text changes only.
- **F1.d (sector-instruction propagation)** — Backport the literary_pick line ("Do NOT pick a title already in `dedup.covered_headlines`. Vary the selection day-over-day.") to all per-sector instructions. Cost: one-liner per sector. Lowest leverage of the four — model compliance only.

### Finding 2 fixes

- **F2.a (URL-keyed cross-part dedup)** — New deterministic post-stitch pass. Walk the stitched briefing, find every `<a href>` URL, identify URLs cited in multiple parts, keep the longest/most-anchored paragraph for each, drop the others (or replace them with a one-sentence backward-reference clause). Cost: ~100 LOC; analogous to existing `_dedup_paragraphs_across_blocks` but URL-keyed. Highest leverage on this issue.
- **F2.b (single-sector ownership for newyorker overlap)** — Stop plumbing `newyorker_hint` to both part4 and part7. Pick one (part7 is cleaner — closer to the actual NYR section). Drop the parallel directive in part4. Cost: 5 lines.
- **F2.c (pre-write per-part URL pruning)** — Before each part's user payload is built, walk a session-level `urls_used_so_far` set. Strip items whose URLs already appeared in an earlier part's payload from later parts. Pushes dedup upstream of the LLM rather than relying on prompt rules. Cost: ~30 LOC in `generate_briefing` loop.

### Finding 3 fixes

- **F3.a (description-vs-interpretation ratio rule)** — Add to write_system.md: "For each article cited, the first paragraph must describe what the article reports — what was said, by whom, where, when, what number/finding/quote. Interpretation comes second. If raw description fills < 60% of an article writeup, the writeup is wrong." Add positive examples. Cost: ~300 chars in system prompt; partially offset by removing some banned-phrase lines. Still prompt-based, but rebalances the incentive.
- **F3.b (drop the "≥3 paragraphs per h3" floor when source material is thin)** — Replace `write_system.md:65-67` rigid floor with a conditional: "If a section has <2 articles or <500 chars of source material, write 1-2 substantive paragraphs and stop. Do not pad with closing commentary." Already partially exists as the Sparse sector rule but is overridden. Cost: prompt edit + remove the OpenRouter editor's section-density floor (write.py:3096-3108).
- **F3.c (raise FIELD_CAPS for description sources)** — Increase `intellectual_journals.findings` 600 → 1200, `global_news.findings` 600 → 900, `wearable_ai.findings` 400 → 800. Gives the writer more raw material so commentary isn't structurally required. Cost: schema.py edits; bigger session JSON; verify Groq TPM still fits.
- **F3.d (drop or weaken WIT QUOTA + SYNTHESIS CLOSE)** — Make both *permitted* rather than *required*. "If a wry observation belongs, write it. If not, end on the last specific fact." Cost: prompt edits; removes structural pressure toward commentary.
- **F3.e (per-part word targets become ceilings, not floors)** — Change `_PART_WORD_TARGETS` from minimum-warning targets to maximum-output ceilings. Drop the 5000-word briefing floor. Cost: write.py + write_system.md edits; will produce shorter briefings — confirm acceptable.

---

## Recommended approach if you want to address all three

Highest-leverage subset, in dependency order:

1. **F2.a** (URL-keyed cross-part dedup) — pure code, no prompt changes, fixes Finding 2 deterministically.
2. **F1.b + F1.c** (Tavily/Serper freshness + query rotation seed) — fixes Finding 1 at the source-of-data layer.
3. **F1.a** (covered_sources) — completes Finding 1's "source rotation" semantics that user explicitly asked for.
4. **F3.b + F3.e** (drop the 3-paragraphs-per-h3 floor; word targets become ceilings) — removes the structural pressure behind Finding 3.
5. **F3.a** (description-first ratio rule) — last; positive prompt directive once structural incentives no longer fight it.

Each step verifiable in isolation. F2.a + F1.b + F3.b alone would likely move the needle visibly within one daily run.

---

## Verification plan if fixes applied

- Unit tests: extend `tests/test_dedup.py` with covered_sources cases (F1.a). New test for URL-keyed cross-part dedup (F2.a).
- Integration: one `--dry-run` of write phase using a synthetic session with deliberate cross-sector URL collision — confirm only one paragraph survives (F2.a).
- Live: one daily run + side-by-side word-count comparison against last 5 briefings. Expect: shorter briefings, fewer repeated source citations day-over-day, no duplicate-section regressions.

---

## What I did not do

- Did not apply any fixes (per Phase 3).
- Did not run tests.
- Did not modify prompts or code.
- Did not investigate `tools/exa.py` in depth (skimmed only) — would do so before producing diffs for F1.b.
- Did not measure actual prior-day repeat rates or pablum-vs-description word counts on real briefings — that would strengthen severity grading but is not required to identify the structural causes above.

Pick the findings + proposed fixes you want me to address. I will produce diffs and show them before applying.
