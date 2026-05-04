# Briefing Repetition + Dedup Fix Plan

Date: 2026-05-03
Trigger: user reports briefing has 3 full passes concatenated; first pass inferior to later pass.

## Evidence

User's 2026-05-03 production briefing has THREE full briefings stitched into one HTML document:
- Pass 1 (thin): no source links, generic prose, occasional hallucinations ("Dr." with no name, $99 wrong price for Friend AI)
- Pass 2 (rich): all source URLs, named officials, real specs — this is the real output
- Pass 3 (thin): generic recap, drops Pass 2's links

`<h3>Domestic Sphere</h3>` appears 3x. `<h3>Beyond the Geofence</h3>` 3x. `<h3>Specific Enquiries</h3>` 3x. Same pattern in 2026-05-02 and earlier briefings.

File line 169-170 has `</body></html>` MID-DOCUMENT followed by 130+ lines of more content. `_enforce_single_close_tag` should have prevented this.

## Pre-Mortem (Challenge)

### Core assumptions in current architecture

1. **Each PART_INSTRUCTIONS block keeps the model on its assigned scope.** Confidence: LOW. Impact: CRITICAL. Model evidently emits full briefing covering all 9 sections from at least 2 different parts.
2. **`_strip_continuation_wrapper` removes all trailing close-tags from middle parts.** Confidence: MEDIUM. Impact: HIGH. Trailing-anchor regex `</html>\s*$` only catches close-tags at exact end of fragment. Embedded `</html>` mid-fragment slips through.
3. **`_enforce_single_close_tag` keeps only LAST occurrence.** Confidence: HIGH. Impact: HIGH. Works on the explicit string, but if model emits unusual whitespace (e.g., `</body  >`) regex misses it. Re-check actual production output.
4. **`_dedup_paragraphs_across_blocks` catches near-duplicate paragraphs.** Confidence: LOW. Impact: HIGH. Fingerprint is first 80 chars exact prefix match. Two paragraphs about Trump-Iran with different opening words BOTH stay in. Real-world repetition is semantic, not lexical.
5. **`_extract_written_topics` regex captures all topic mentions.** Confidence: LOW. Impact: HIGH. Regex `[A-Z][a-z]+\s[A-Z][a-z]+` requires 2-word capitalized sequence. Misses "Trump", "Iran", "Mali", "UN", "OPEC", "AARO". Within-run dedup blind to most proper nouns.
6. **`run_used_topics` capped at 30 most recent (tail slice) is sufficient.** Confidence: LOW. Impact: HIGH. Topics from earliest parts evicted as run grows. By Part 7+ the dedup signal for Part 4 topics is gone.
7. **`cross_sector_dupes` is wired into write phase.** Confidence: ZERO. Impact: HIGH. Computed in research phase, NEVER read by write phase. Dead code.
8. **NIM refine and OpenRouter narrative editor preserve fragment scope.** Confidence: LOW. Impact: CRITICAL. Refine prompt may instruct edit but model could expand fragment into full briefing. Need to inspect _REFINE_SYSTEM and editor system prompt.

### Vulnerability map

CRITICAL (act before next run):
- **#1 + #2:** Some part is emitting full briefing. Stitcher's middle-part defenses don't strip embedded full-briefing content — only trailing close-tags. Root cause must be located: which part(s)? raw draft? refined? OpenRouter pass?
- **#7:** Dedup signal computed but discarded. Free 30-50% reduction in repetition just by wiring it.

HIGH:
- **#5 + #6:** Topic extractor's blind spots + tail-slice cap together mean within-run dedup never sees most named entities. Even if model wanted to comply, prompt doesn't tell it Trump was already covered.
- **#4:** Paragraph-dedup is exact-prefix; useless against rewrites. Drops first occurrence keeps later — but user says first is inferior; need to drop LATER duplicates and keep best, not first.

### Dependency chain

[Part emits full briefing] → [_strip_continuation_wrapper fails to strip embedded body] → [stitcher concatenates duplicates] → [_dedup_paragraphs_across_blocks fails on lexical mismatch] → [_repair_container_structure thinks orphan zone is fine] → [user sees 3 passes]

Weakest link: the FIRST step. If parts stay scoped, no downstream cleanup needed.

### Reversibility

All changes reversible via git. No external state. Each fix testable in isolation against the 2026-05-03 raw drafts (sessions/session-2026-05-03.json).

## Root-Cause Hypotheses (priority order)

H1. **Part is rendering full briefing.** PART_INSTRUCTIONS for some part(s) are too permissive or system prompt's "Briefing structure" leaks despite strip in `_system_prompt_for_parts`. Test: dump per-part raw_drafts from a real run; count `<h3>` headers per part. Expected: 1-3 per part. Actual: likely 7-9 in offending part.

H2. **NIM refine is regenerating instead of editing.** _REFINE_SYSTEM may not pin scope. NIM model may interpret "edit" as "rewrite from scratch." Test: compare raw_drafts vs refined for one part — count headers, compare URL set.

H3. **OpenRouter editor returns expansion + original.** Quality gate accepts response as long as it has `</html>` + `<p>` tags. If model returns "edited version" THEN appends "original for reference," gate passes. Test: inspect actual OpenRouter response logs.

H4. **Stitcher is fed both raw and refined.** Bug in `final_parts = [refined.get(label, raw_drafts[label]) for label, _ in PART_PLAN]` — should fall back to raw on missing refined, but if both exist somehow concatenation happens. Read code: confirms correct fallback. Less likely.

H5. **Postprocess concatenates additional content.** `_inject_newyorker_verbatim`, `_inject_source_links`, `_repair_container_structure` may bug. Less likely — these run AFTER the duplication is already in stitched.

Most likely: H1 + H2 combined. Some parts emit full briefings; refine doesn't fix; stitcher concatenates.

## Plan (ordered phases)

### Phase 0 — Diagnose (do this first, do not skip)

P0.1. Add raw-draft dump in `generate_briefing` — write each `raw_drafts[label]` to `sessions/debug-<date>-<label>.html` when env `JEEVES_DEBUG_DRAFTS=1`. Also dump each `refined[label]`.

P0.2. Add per-part header count log: `log.info("[%s] H3 count: %d", label, raw_part.count("<h3"))`. Anomaly = 7+ on a part that should have 1-3.

P0.3. Run a real briefing or use existing session JSON via `python -m jeeves.write` against sessions/session-2026-05-03.json. Inspect dumps to confirm WHICH part(s) are emitting full briefings.

P0.4. **Decision gate:** confirmed cause before continuing. If a part is emitting full briefing → proceed to Phase 1. If refine is rewriting → proceed to Phase 2 first. If OpenRouter is the culprit → Phase 3 first.

### Phase 1 — Stop full-briefing emission per part

P1.1. Audit each `PART*_INSTRUCTIONS` for ambiguity. Add explicit boundary at TOP of each: "OUTPUT SCOPE: emit ONLY content for this part. DO NOT write any `<h3>` other than the one(s) listed below. DO NOT close `<body>` or `<html>`. DO NOT emit a signoff. Output ends with `<!-- PART<N> END -->`."

P1.2. Strengthen `_validate_part_fragment`: count `<h3>` tags and reject (or mark `quality_warnings`) if count > expected for that part. Expected map:
   - part1: 1 (Domestic Sphere is in part2; part1 is intro+weather)
   - part2: 1
   - part3: 1
   - part4: 2 (family + global)
   - part5: 1 (Reading Room)
   - part6: 1 (Specific Enquiries)
   - part7: 2 (UAP Disclosure + Commercial Ledger area)
   - part8: 1
   - part9: 0 (NEWYORKER block, no `<h3>`)

P1.3. Add hard-fail retry: if a part emits more than 2x expected `<h3>` count, regenerate that part once with stricter system prompt prefix. If second attempt also fails, drop and continue with empty fragment + warning.

P1.4. Strengthen `_strip_continuation_wrapper` for middle parts — strip content AFTER any embedded `</body>` or `</html>`, not just trailing.

```python
# Truncate middle-part content at first embedded </body> or </html>
m = re.search(r"</(?:body|html)>", s, re.IGNORECASE)
if m:
    s = s[:m.start()].rstrip()
```

P1.5. Tests: add `tests/test_part_scope_enforcement.py` with synthetic full-briefing-as-fragment input → assert validation flags it AND stitcher truncates it.

### Phase 2 — Fix NIM refine scope

P2.1. Read `_REFINE_SYSTEM` (line ~1820 region). Verify it says: "edit only — do not regenerate, do not expand. Return the same fragment with surgical fixes. If draft is bad, return it unchanged."

P2.2. If refine emits headers not in input draft, reject and use raw. Add comparison: `if set(_h3_texts(refined)) - set(_h3_texts(raw)): use raw + warn`.

P2.3. Cap refine output length at `len(raw_draft) * 1.5`. If exceeded, suspect rewrite — use raw.

### Phase 3 — Fix OpenRouter editor scope

P3.1. Tighten quality gate: assert edited length is between 0.7x and 1.3x of input. Outside band → reject.

P3.2. Assert h3 count(edited) <= h3 count(input). If editor added headers, reject.

P3.3. Log raw OpenRouter response to disk when env flag set.

### Phase 4 — Wire cross_sector_dupes into write phase

P4.1. In `_trim_session_for_prompt`, surface `dedup.cross_sector_dupes` to prompt (not just keep field).

P4.2. Add `<!-- CROSS_SECTOR_COVERED -->` block to user payload listing URLs already used in earlier sectors.

P4.3. Update relevant PART_INSTRUCTIONS (part5 intellectual_journals + part6 triadic/ai + part7 uap) to reference the list and skim+pivot if a topic's URL appears there.

### Phase 5 — Fix within-run topic dedup

P5.1. Rewrite `_extract_written_topics`:
   - Capture single capitalized words ≥ 4 letters: `\b[A-Z][a-z]{3,}\b`
   - Capture acronyms 2-6 letters: `\b[A-Z]{2,6}\b`
   - Keep multi-word capture
   - Dedup by lowercase
   - Cap at 80 per part (instead of 40)

P5.2. Replace tail-slice `[-DEDUP_PROMPT_TOPICS_CAP:]` with frequency-weighted selection — keep topics that have appeared 2+ times (most likely to recur). Bump cap to 80.

P5.3. Add explicit prompt directive: "### Run topics already covered today — DO NOT cover again. Only acknowledge with one bridging clause if directly relevant: ..."

### Phase 6 — Strengthen post-stitch dedup

P6.1. `_dedup_paragraphs_across_blocks`: switch from 80-char prefix to a semantic shingle (set of 4-word shingles → Jaccard ≥ 0.6 = duplicate).

P6.2. When duplicates detected, KEEP the longer/richer one (more `<a href>` anchors, more chars), DROP the shorter one. Currently keeps first.

P6.3. Add `_dedup_h3_sections_across_blocks`: if same `<h3>` text appears multiple times non-adjacently, keep ONE block (the one with most `<a>` links + most words).

P6.4. Tests: feed Pass 1 + Pass 2 + Pass 3 from briefing-2026-05-03 and assert output keeps Pass 2 only.

### Phase 7 — Verification

P7.1. Run `python -m pytest tests/` — full suite green.

P7.2. Generate dry-run briefing against sessions/session-2026-05-03.json. Confirm:
   - Each `<h3>` appears exactly once
   - Source URLs from session present in prose
   - Word count ≥ 5000
   - No content after `</html>`

P7.3. Trigger one real production run via `workflow_dispatch write.yml`. Inspect output before next scheduled cron.

## Reversibility / Kill Switches

Continue if (after Phase 0 diagnostic):
- Single root cause identified
- A test case isolates the bug

Kill / rethink if:
- Multiple unrelated bugs all triggering same symptom (rare)
- Bug is in upstream model behavior (Groq emitting garbage we can't catch in code) — would need model swap

## Hardening actions

1. Make Phase 0 diagnostic permanent: keep `JEEVES_DEBUG_DRAFTS` env as standard flag. Rotate dumps (keep last 7 days).
2. Add scheduled smoke test: run dry-run briefing weekly with fixture session, assert expected `<h3>` count.
3. Add quality_warnings field for "duplicate_h3_count:N" → escalate to manifest dashboard.

## Estimated effort

- Phase 0 diagnostic: 30 min (logging + one test run)
- Phase 1: 2 hours (instructions + validation + retry + tests)
- Phase 2: 1 hour
- Phase 3: 1 hour
- Phase 4: 1 hour
- Phase 5: 1.5 hours
- Phase 6: 2 hours
- Phase 7: 1 hour

Total: ~10 hours focused work. Phases 1, 2, 6 are the must-haves. Phases 4, 5 reduce repetition density but don't fix the structural triple-pass.

## Hardest questions

- "What if this is fundamentally a Groq model defect — no prompt fix works?" → Test by switching to NIM-only run; if same symptom, blame is on prompts/instructions, not model.
- "Why did sprint 9 dedup overhaul not catch this?" → Sprint 9 fixed RESEARCH-phase dedup (prior_sample staleness). This is a WRITE-phase symptom. Different bug, same word.
- "Are we adding complexity to defend against a single broken run?" → No. Multiple briefings (5/2, 5/3) show same triple-pass pattern. Systemic.
