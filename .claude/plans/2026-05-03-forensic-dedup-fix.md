# Forensic Plan — Briefing Concatenation + Dedup Failure
**Date:** 2026-05-03
**Trigger:** Saturday email reads as 3+ stitched drafts. Same on briefing-2026-05-01.html and briefing-2026-05-02.html.
**Author:** Claude (caveman mode)

---

## Evidence Captured

### Disk forensics
| File | Lines | `<h3>` count | Verdict |
|---|---|---|---|
| briefing-2026-04-26.html | 89 | 0 | clean (single doc, pre-sectional) |
| briefing-2026-04-27.html | 54 | 0 | clean |
| briefing-2026-04-28.html | 79 | 0 | clean |
| briefing-2026-05-01.html | **288** | **28** | broken (3+ stitched drafts) |
| briefing-2026-05-02.html | **192** | **22** | broken (3 stitched drafts) |

**Regression boundary:** between commit `98fa7db sprint 12 forensic newsletter quality overhaul` (May 1) and present.

### briefing-2026-05-02.html structural map
- **Block A (1-72):** complete HTML doc — DOCTYPE, banner, 8 sectional `<h3>`, `<p class="signoff">And so, Mister Lang, I conclude our daily briefing.</p>`, `<!-- COVERAGE_LOG: ... -->`, `</body></html>`
- **Block B (74-112):** 8 more `<h3>` sections, no DOCTYPE/style/banner. Two `<h3>The Specific Enquiries</h3>` back-to-back at 100/102
- **Block C (114-157):** 8 MORE `<h3>` sections (Domestic Sphere → From the Library Stacks)
- **Block D (159-187):** TOTT verbatim block (correct)
- **Lines 188-189:** `<p><a>Read at The New Yorker</a></p>` × 2 (duplicate)
- **Block E (190-192):** real `<div class="signoff">Your reluctantly faithful Butler, Jeeves</div>`

### Same pattern on 05-01 — 3+ block concatenation, duplicate Read link.

---

## Root-Cause Hypotheses (ranked by evidence)

### H1 [HIGHEST] — Part 1 model emits a complete briefing; `_stitch_parts` glues Parts 2-9 AFTER its `</html>`
**Code:** `jeeves/write.py:1451 _stitch_parts`, `jeeves/write.py:1328 _strip_continuation_wrapper`
- `_strip_continuation_wrapper` runs on `parts[1:]` only. Part 1 is never stripped.
- If Groq Part 1 disobeys "PART 1 of 9" and emits a complete HTML doc (DOCTYPE → banner → all sections → signoff → `</body></html>`), the stitcher's existence check `if "</body>" not in low: combined += "</body>"` passes silently — closing tags already present.
- Parts 2-9 then concatenate AFTER `</body></html>` as orphan content.
- Block A signature matches: non-canonical `<p class="signoff">And so, Mister Lang, I conclude our daily briefing.</p>` (canonical is `<div class="signoff">Your reluctantly faithful Butler, Jeeves</div>`).
- `_repair_container_structure` (line 1360) splices orphans inside container, but only when `</body>` exists exactly once at the end.

### H2 [HIGH] — NIM refine pass on Part 1 returns a complete HTML doc
**Code:** `jeeves/write.py:1682 _invoke_nim_refine`, used at line 3260.
- Refine system prompt says "output the corrected HTML." If model wraps fragment in DOCTYPE/body, refined Part 1 contains a complete doc.
- No structural validation post-refine.

### H3 [HIGH] — OpenRouter narrative editor returns concatenated original + edited HTML
**Code:** `jeeves/write.py:2888 _invoke_openrouter_narrative_edit`
- Quality gates check word count `≥80%` of input, NO ceiling check. Bloated 200% output passes.
- Block C might be the editor's "edit" appended after preserving Block A+B.

### H4 [HIGH — confirmed] `_inject_newyorker_verbatim` doubles the Read link
**Code:** `jeeves/write.py:1848 _build_newyorker_block`, `jeeves/write.py:1244 PART9_INSTRUCTIONS`
- PART9_INSTRUCTIONS Step 3 tells model to write `<p><a>Read at The New Yorker</a></p>` AFTER placeholder.
- `_build_newyorker_block` ALSO appends Read link (`read_link = f'\n<p><a>Read at The New Yorker</a></p>'`).
- Result: two consecutive Read links every run. Confirmed in 05-01 and 05-02.

### H5 [MEDIUM] — Parts 6+7 both emit `<h3>The Specific Enquiries</h3>`
**Code:** PART_PLAN parts 6+7 → same prose section in prompt.
- Part 6 (triadic_ontology, ai_systems): `<h3>The Specific Enquiries</h3>`
- Part 7 (uap, wearable_ai, literary_pick): `<h3>The Specific Enquiries</h3>` again
- Visible at lines 100/102 of 05-02.

### H6 [MEDIUM] — Within-run dedup ignores cross-block paragraph repetition
**Code:** `_extract_written_topics` proper-noun-only fingerprint.
- Doesn't fingerprint full headlines/paragraphs. Block A vs B vs C all repeat Iran/Strait of Hormuz, MagicSchool AI, UAP disclosure. Topic dedup never fires across draft layers.

---

## Forensic Code-Review — Severity Ledger

| # | Severity | Location | Bug |
|---|---|---|---|
| **B1** | **CRITICAL** | `_stitch_parts:1469` | Blindly joins parts; if Part 1 emits `</html>`, Parts 2-9 land outside the document |
| **B2** | **CRITICAL** | `_strip_continuation_wrapper:1328` | Skipped on Part 1 (`if i > 0`). Should also strip Part 1 trailing `</body></html>` |
| **B3** | **HIGH** | `_repair_container_structure:1360` | Uses `rfind("</body>")` — when multiple `</body>` exist, repair operates on wrong one |
| **B4** | **HIGH** | `_editor_quality_gates:2839` | Word floor (80%) but no ceiling. Bloated edits pass |
| **B5** | **HIGH — CONFIRMED** | `_build_newyorker_block:1851` + `PART9_INSTRUCTIONS:1244` | Both write Read link → 2 copies guaranteed every run |
| **B6** | **MEDIUM — CONFIRMED** | `PART_PLAN:321` + per-part instructions | Parts 6+7 both emit `<h3>The Specific Enquiries</h3>` |
| **B7** | **MEDIUM** | `_invoke_nim_refine:1682` | No structural validation that refined output is fragment, not complete doc |
| **B8** | **MEDIUM** | No part-level structural gate before stitching | Should reject/repair Part 1 containing `</body>`, `</html>`, `<div class="signoff">` |
| **B9** | **LOW** | `postprocess_html:3453` | `_validate_html_structure` logs warning only — never repairs multi-`</body>` documents |
| **B10** | **LOW** | `_extract_written_topics:3331` | Proper-noun-only dedup misses paragraph-level repetition across draft layers |

---

## Fix Plan (Ordered)

### Phase 1 — Stop the bleeding (~2hr)

1. **Strip closing tags from EVERY part** (`_stitch_parts:1451`)
   - Run trailing-tag scrub on every part incl Part 1
   - Part 1 keeps DOCTYPE/head/body OPEN; loses any premature `</body></html>`, `</div>` that closes container
   - Add post-stitch assertion: count `</body>` ≤ 1, `</html>` ≤ 1 — splice out duplicates if not

2. **Fix duplicate Read link** — remove from `_build_newyorker_block:1851`
   - Part 9 already writes it per PART9_INSTRUCTIONS Step 3
   - Don't double-emit deterministically

3. **Dedup adjacent duplicate `<h3>` post-stitch**
   - Walk `<h3>` tags top-to-bottom; collapse two adjacent identical headers
   - Specific case: two `<h3>The Specific Enquiries</h3>`

4. **OpenRouter editor word-count CEILING gate** (`_editor_quality_gates:2839`)
   - Add `_EDITOR_WORD_CEILING_RATIO = 1.30` — reject if output > 130% input
   - Catches "echo input + add edit" failure mode

### Phase 2 — Structural validation gate (~3hr)

5. **Pre-stitch part validation** — new `_validate_part_fragment(part_idx, raw_html)`
   - Part 0: must START with `<!DOCTYPE`/`<html>`; must NOT contain `<div class="signoff">`, `<!-- COVERAGE_LOG`, or `</html>` close
   - Parts 1-7: must NOT contain `<!DOCTYPE`, `<html>`, `<head>`, `<body>` open; no signoff div, no coverage log, no `</html>` close
   - Part 8: same + no Library Stacks header collision
   - Part 9: must contain placeholder OR Branch B sentinel; must contain signoff div + `</body></html>`
   - On violation: ERROR log + apply remediation pre-stitch

6. **Promote `_validate_html_structure` warnings to repair** (`postprocess_html:3453`)
   - `container open count != 1` → splice duplicates
   - `<p>` outside container → run `_repair_container_structure` on EACH `</body>` not just last
   - Multi-`</body>` → keep last, splice content from earlier ones inside container

### Phase 3 — Cross-block dedup (~2hr)

7. **Post-stitch paragraph-fingerprint dedup**
   - Hash each `<p>` body's first 80 chars (lowercased, ws-collapsed)
   - If hash appears > 1× outside TOTT block, drop duplicates
   - Log `dedup_paragraph_count` to RunManifest

8. **Fix Parts 6+7 header collision** (`PART7_INSTRUCTIONS`)
   - Add explicit rule: "DO NOT write `<h3>The Specific Enquiries</h3>`. Continue under prior section's header."
   - OR rename Part 7 header (e.g., "The Wider Pulse")

### Phase 4 — Tests + observability (~2hr)

9. **Regression tests** (`tests/test_write_dedup.py` — NEW)
   - `test_part1_complete_doc_gets_truncated`
   - `test_stitched_briefing_has_one_body_one_html`
   - `test_no_duplicate_read_at_newyorker`
   - `test_no_adjacent_duplicate_h3`
   - `test_paragraph_dedup_across_blocks`
   - `test_openrouter_ceiling_gate_rejects_bloated_output`

10. **RunManifest counters**
    - Add `paragraph_dedup_count`, `header_dedup_count`, `closing_tag_strip_count`
    - Surface in `sessions/run-manifest-DATE.json`

### Phase 5 — Validation + ship (~1hr)

11. **Re-run write against session-2026-05-02.json** with fix branch
    - Assert exactly one `</body>`, one `</html>`, one Read link, no adjacent dup `<h3>`
    - Word count drops from ~10k bloated → ~4500 target

12. **Smoke test**
    - `python scripts/write.py --date 2026-05-02 --skip-send`
    - Tests pass (target 250+)

13. **PR + merge**
    - No workflow files touched → PAT scope OK

---

## Acceptance Criteria

- [ ] Exactly one `<!DOCTYPE>`, one `</body>`, one `</html>`
- [ ] No two adjacent `<h3>` with identical text
- [ ] Exactly one `<p><a>Read at The New Yorker</a></p>` in TOTT footer
- [ ] No paragraph (first 80 chars) appears more than once outside TOTT verbatim block
- [ ] Word count 3500-6500 (currently ~10k+ on broken runs)
- [ ] Tests green (target 250+/250+)
- [ ] One full pipeline run produces clean output

---

## Files Touched (estimated)

- `jeeves/write.py` — primary surgery (~400 LOC delta)
- `jeeves/prompts/write_system.md` — PART7 header rule
- `tests/test_write_dedup.py` — NEW (~200 LOC)
- `tests/test_write_postprocess.py` — extended invariants
- `CLAUDE.md` — `<state>` block update post-merge

Estimated: 4 files modified, 1 new, ~10 hours.
