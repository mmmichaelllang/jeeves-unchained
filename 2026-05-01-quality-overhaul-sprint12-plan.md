# Sprint 12 — Newsletter quality overhaul (forensic, end-to-end)

**Date**: 2026-05-01
**Branch target**: `claude/quality-overhaul-sprint12`
**Tests target**: 190 → 220+ (≥30 new tests for new guarantees)

---

## Forensic findings from `briefing-2026-05-01.html`

Compared against the last good briefing (`briefing-2026-04-25.html`):

| Metric | 2026-04-25 (good) | 2026-05-01 (bad) | Cause |
|---|---|---|---|
| Banner `<img>` | absent (old scaffold) | absent (new scaffold has CSS but model dropped tag) | model non-compliance, no deterministic injection |
| Anchor links in body | 26 | 5 | source-URL map narrow; model omitted anchors mid-prose; no post-validation |
| `<p>` tags | 29 | 21 | OpenRouter editor over-deletes; per-part content thin |
| File size (bytes) | 14,166 | 11,514 | content collapse |
| TOTT terminal text | full last paragraph | `…wore a pin-st [TRUNCATED]` | `FIELD_CAPS["newyorker.text"] = 4000` truncates the article in `apply_field_caps()` BEFORE write phase reads it |
| Profane asides | natural inline ("turned into a steaming bucket of dog-shit, Sir") | standalone template ("the Kremlin's Mali pledge **is, a proper omnishambles**...") | OpenRouter editor reverted to template-slot pattern despite B2 rules |
| Signoff | "Your reluctantly faithful Butler" | **"Your faithfully Butler"** | regex `[Yy]ours faithfully,?` does not match `Your faithfully` (no `s`); model emitted typo and post-process missed it |
| Structure | one continuous narrative | 6 `<h3>` sections + 2 orphan `<p>` outside `.container` | per-part splits leak `</div>` past sentinel; `_strip_continuation_wrapper` does not strip stray closers |
| Word count (body prose) | ~3,200 | ~700 | cumulative effect of all above |

**Bottom line**: every layer (FIELD_CAPS, prompts, post-processing, OpenRouter editor) has a hole. Fix layer-by-layer with deterministic post-processing as the last line of defense.

---

## Root causes mapped to fixes

### A. Banner image disappears every run

**Root cause**: `PART1_INSTRUCTIONS` (line 234, `jeeves/write.py`) and `write_system.md` line 113 both contain the literal `<img class="banner" src="https://i.imgur.com/UqSFELh.png" alt="">`. Groq output is non-deterministic — model occasionally drops the tag. CSS exists (`.banner`) but the element does not. No fallback.

**Fix**: deterministic post-stitch banner injection in `_inject_banner(html)`. Run it as the LAST step before `_inject_newyorker_verbatim` (so banner sits at top regardless of what Part 1 emitted).

```python
_BANNER_HTML = '<img class="banner" src="https://i.imgur.com/UqSFELh.png" alt="">'
_BANNER_RE = re.compile(r'<img\b[^>]*class="banner"[^>]*>', re.IGNORECASE)
_CONTAINER_OPEN_RE = re.compile(r'(<div[^>]*class="container"[^>]*>)', re.IGNORECASE)

def _inject_banner(html: str) -> str:
    if _BANNER_RE.search(html):
        # Already present — but verify URL matches; if not, replace.
        return _BANNER_RE.sub(_BANNER_HTML, html, count=1)
    m = _CONTAINER_OPEN_RE.search(html)
    if not m:
        log.warning("banner injection: no .container open tag found")
        return html
    insert_at = m.end()
    return html[:insert_at] + "\n" + _BANNER_HTML + html[insert_at:]
```

Insertion point: immediately after `<div class="container">`, before `<div class="mh-date">`. Replaces existing if URL drifted.

**Tests** (3): banner injected when missing; banner replaced when URL wrong; banner left alone when correct.

---

### B. Talk of the Town truncated at 4000 chars

**Root cause**: `jeeves/schema.py:143`: `FIELD_CAPS["newyorker.text"] = 4000`. `apply_field_caps()` (line 195) appends ` [TRUNCATED]` and writes to disk. Write phase reads the truncated text, `_inject_newyorker_verbatim` injects it verbatim. The original 4000-char cap was for a Groq write-phase that ingested the full session JSON; **that model has been retired** — Part 9 strips `newyorker.text` from its payload entirely (`write.py:2395`). The cap is now load-bearing on nothing except making TOTT shorter.

**Fix**: raise `FIELD_CAPS["newyorker.text"]` to **40000** (Talk of the Town pieces top out at ~9000 chars; 40k is safe headroom, still bounds an unbounded write). Update `tests/test_schema.py` to assert the new cap.

**Verification**: re-fetch today's TOTT (Reverend Billy / JPMorgan) and confirm `len(session.newyorker.text)` lands ~6500 chars with no `[TRUNCATED]` suffix.

**Tests** (2): asserting cap = 40000; asserting full Reverend Billy fixture (8000 chars) survives `apply_field_caps()` unmodified.

---

### C. Profane asides emitted as templated standalone paragraphs

**Root cause**: OpenRouter editor produced this pattern five times:
```
<p>the Kremlin's Mali pledge is, a proper omnishambles of the highest, most fucking degree.</p>
```
The pattern is `[lowercase topic] is, [aside].` — a **slot template**, not natural insertion. Rule B2 in `_NARRATIVE_EDIT_SYSTEM_BASE` instructs natural insertion but provides only ONE positive example and the model defaulted to a slot template. Capitalisation is broken (`the Kremlin's` mid-sentence in a fresh `<p>`). Three fix layers needed:

1. **Prompt-level** (`_NARRATIVE_EDIT_SYSTEM_BASE`): add 4 strong negative examples of the template-slot pattern; add 4 strong positive examples showing prose-modification (escalation → punch); add explicit ban on standalone `<p>` paragraphs whose entire body is the aside; require the aside to appear INSIDE a paragraph that already contains 2+ sentences of substantive content.

2. **Post-processing validator** (`_validate_aside_placement(html) -> list[str]`): scan `<p>` tags. If a paragraph is < 25 words AND contains a profane fragment AND does not contain at least 1 sentence-ending period BEFORE the aside fragment → flag. Returns a list of warnings; logged at WARNING level. Surfaced as a new `aside_placement_violations` field on `BriefingResult`.

3. **Auto-merge fallback** (`_merge_orphan_asides(html)`): if a profane-aside-only `<p>` immediately follows a substantive content `<p>` in the same section, merge: append the aside (de-templated, lowercased to mid-sentence form) onto the prior paragraph as ` — [aside]`. This is a deterministic last-resort, not a voice replacement, but prevents the standalone-template artifact reaching the inbox even if the editor regresses.

**Tests** (5): validator flags standalone aside; validator passes natural inline aside; merge fixes adjacent standalone; merge skips when no preceding paragraph; merge does not affect NEWYORKER block.

---

### D. Source-URL anchor coverage collapsed (26 → 5)

**Root cause analysis**:
- `_build_source_url_map` only covers 5 sectors (local_news, global_news, intellectual_journals, wearable_ai, enriched_articles) plus 3 scalars (triadic_ontology, ai_systems, uap).
- `career.openings` items have `url` fields per posting — NOT in map. Yesterday's briefing linked 8 job postings; today's 0.
- `family.choir` and `family.toddler` items often have `url`. Not in map.
- `literary_pick.url` not in map.
- The OpenRouter editor's "delete generic filler" rule sometimes deletes whole link-bearing sentences.
- The model writes "Reuters" once and "BBC" once; `_inject_source_links` only injects the FIRST occurrence per source (intentional anti-clutter), so a paragraph naming "BBC" three times gets one anchor.

**Fix**:
1. Broaden `_build_source_url_map`:
   - Iterate `session.career.get("openings", [])` and add `(title, url)` and `(school, url)` pairs.
   - Iterate `session.family.get("choir", [])` and `session.family.get("toddler", [])` for `(name, url)` and `(venue, url)`.
   - Add `literary_pick.title` and `literary_pick.author` → `literary_pick.url`.
   - Add `correspondence_url` if present.

2. Allow MULTIPLE injections per source-name when the document has multiple paragraphs naming it: cap at 3 injections per (source_name, url) pair to balance anti-clutter against link density. New `_INJECT_PER_SOURCE = 3`.

3. New diagnostic: `BriefingResult.link_density` = anchors / 1000 prose-words. Log at INFO. Target ≥ 8 anchors per 1000 words; warn below 5.

**Tests** (8): career→title link; family choir→name link; literary_pick→title link; same source linked 3 times across 3 paragraphs; 4th occurrence not linked; link density computed; warning emitted at low density; existing single-link behaviour preserved when only one mention.

---

### E. Voice quality: lifeless, compressed, generic

**Root causes (multiple, layered)**:

1. The **OpenRouter narrative editor's own deletions are over-aggressive**: A1 has 50+ "delete on sight" patterns, no positive countermeasure for under-deletion vs over-deletion. Model defaults to safer = delete more.

2. The **per-part word targets are not enforced**. `PART4_INSTRUCTIONS` says "Aim for ~700-900 words" but model returns ~150. No retry-on-too-short.

3. The **dedup sledgehammer**: 261 covered_headlines pushed to write payload. Synthesis protocol case (a) says "one sentence and move on" for static repeats. With 261 priors, almost every item triggers case (a). Result: thin content.

4. **No mandate for sentence-level wit density**: `WIT QUOTA` rule says "at least one wry observation per part". Hit rate visible in 2026-05-01: 0–1 per part. Target should be 2–3 per part.

5. **`<h3>` sectional headers** introduced by model are NOT instructed, but the CSS for them is in the scaffold so the model uses them. They look magazine-y but combined with thin paragraph content produce skeleton appearance.

**Fixes**:

E.1 **OpenRouter editor — preservation directives** (`_NARRATIVE_EDIT_SYSTEM_BASE` PART A):
- Add `### A0. PRESERVATION RULES — DO NOT VIOLATE` *before* A1: never delete a paragraph that contains 2+ specific named entities (proper nouns, company names, dollar figures, dates); never delete an `<a href>` anchor unless its surrounding sentence is filler; if a section ends up with fewer than 3 substantive paragraphs after edits, restore the most-deleted one.
- Add explicit `### A0.1. WORD COUNT FLOOR`: total document body prose must remain ≥ 80% of input prose. The editor is a sharpener, not a compressor. If the editor's output is < 80% of input by word count, return the input unchanged (better lifeless than empty).
- A retry loop in `_invoke_openrouter_narrative_edit`: if the response is < 80% input length, log warning and return original draft.

E.2 **Sectional `<h3>` — make it explicit, not accidental**:
- Add `## SECTIONAL STRUCTURE` to `write_system.md` listing the 7 canonical section headers (`<h3>The Domestic Sphere</h3>`, etc.) and their content requirements. Each `<h3>` requires ≥3 substantive paragraphs (no orphan headers with 1-paragraph stubs).
- Update PART_INSTRUCTIONS to specify which `<h3>` each part owns.

E.3 **Per-part minimum-word retry**: in `generate_briefing`, after each part, count `<p>` body words. If < 60% of target, log WARNING and re-invoke with stronger directive: "The previous draft was thin (X words; target Y). Expand depth and detail; do not summarise. Output the FULL section." One retry per part max.

E.4 **Wit density floor**: add `WIT_DENSITY_FLOOR = 1` (witty/sardonic phrase per 200 words) to `_NARRATIVE_EDIT_SYSTEM_BASE` rule A12; provide 6 paste-ready dry-Jeeves observations as exemplars.

E.5 **Dedup synthesis recalibration** (`write_system.md` rule "Synthesis protocol"): reframe case (a) to ALWAYS produce at minimum 2 sentences — a backward-reference clause AND one new connection/aside. The current "one sentence and move on" makes briefings too thin when prior coverage is dense. Eliminating in 1 sentence is fine for 2-3 stale items, not for 50 of them.

**Tests** (12): editor preservation rule (over-deletion → unedited returned); per-part word retry; h3 structure assertion; wit-density count; synthesis case-a 2-sentence floor; etc.

---

### F. Wrong sign-off slipping through

**Root cause**: regex `re.sub(r"[Yy]ours faithfully,?", ...)` does not match `Your faithfully Butler` (note: model dropped the `s` and added "Butler" inline). The post-process check `"yours faithfully" in body_text.lower()` also misses because actual text reads `Your faithfully Butler`.

**Fix**:
- Broaden detection: `_WRONG_SIGNOFFS = re.compile(r'(?:Y(?:ou(?:r|rs)?))\s+faithfully(?:\s+Butler)?,?', re.IGNORECASE)`. Matches `Yours faithfully`, `Your faithfully`, `Your faithfully Butler`, `Yours faithfully Butler`, `yours faithfully`, all variants.
- Replace with literal `Your reluctantly faithful Butler,`.
- Also add `Sincerely`, `Best regards`, `Yours truly`, `Yours sincerely` to the wrong-signoff family.
- Validate: `assert "Your reluctantly faithful Butler" in html` after `postprocess_html`. If absent, hard-fail the postprocess (raise) rather than ship a wrong sign-off.

**Tests** (5): each wrong-signoff variant detected and replaced; correct sign-off preserved; hard-fail when no signoff at all.

---

### G. Orphan `<p>` outside `.container`

**Root cause**: model emits `</div>` mid-stream in PART2 or PART3 empty-feed branch. Sentinel `<!-- PART2 END -->` is stripped but the stray `</div>` remains. Subsequent parts append after the now-closed `.container`. `_strip_continuation_wrapper` strips DOCTYPE/html/head/body/h1/mh-* divs but NOT bare `</div>` closers.

**Fix**:
- `_strip_continuation_wrapper`: add `s = re.sub(r'</div>\s*$', '', s, flags=re.IGNORECASE)` to strip a TRAILING `</div>` from continuation parts (parts 2-9). They are forbidden from closing outer tags; this enforces the rule.
- New `_repair_container_structure(html)`: scan for orphan `<p>` AFTER the LAST `</div>` (which would be the `.container` closer). If found, move them INSIDE the container before the `</div>`. Logged at WARNING.
- Add `_validate_html_structure(html)`: assert exactly one `<div class="container">` open tag and one matching `</div>`; assert `<div class="signoff">` is INSIDE container; assert no `<p>` outside container. Returns list of structural errors. Logged.

**Tests** (6): trailing `</div>` stripped from part output; orphan `<p>` repaired; signoff inside container; multiple violations all detected; valid structure passes; coverage_log comment placement validated.

---

### H. The OpenRouter narrative editor needs guardrails

The editor is responsible for: (1) cleaning filler, (2) inserting 5 asides, (3) narrative cohesion. It is the **single most leverage point** in the whole pipeline — a bad model on this step ruins the brief regardless of how good the drafts are.

**Fixes** (consolidated):

- **Model selection determinism**: log which model actually returned the response so we know which fallback fired.
- **Output validation gates** (BEFORE returning edited):
  - word count ≥ 80% input (E.1)
  - exactly 5 profane asides (count via PROFANE_FRAGMENTS)
  - 0 standalone-template asides (C validator)
  - ≥ 5 `<a href>` anchors per 1000 prose-words
  - signoff intact: "Your reluctantly faithful Butler" present
  - banner intact: `<img class="banner"` present
  - NEWYORKER block byte-identical to input
- If ANY gate fails, fall through to next model in chain. Final fallback: return INPUT unedited (the briefing ships, regressed but functional).

**Tests** (4): each gate independently rejects bad output and falls through; final unedited fallback returns input on all-gates-failed.

---

## Implementation order (do them this sequence; commit per group)

| Step | File(s) | Lines changed | Test additions |
|---|---|---|---|
| 1 | `jeeves/schema.py` | bump `FIELD_CAPS["newyorker.text"]` to 40000 | 2 |
| 2 | `jeeves/write.py` | `_inject_banner()` + call site in `generate_briefing` | 3 |
| 3 | `jeeves/write.py` | `_strip_continuation_wrapper` adds `</div>` strip | 1 |
| 4 | `jeeves/write.py` | `_repair_container_structure()` + `_validate_html_structure()` | 6 |
| 5 | `jeeves/write.py` | `_WRONG_SIGNOFFS` regex + hard-fail in `postprocess_html` | 5 |
| 6 | `jeeves/write.py` | `_build_source_url_map` broadened (career, family, literary_pick) + `_INJECT_PER_SOURCE=3` | 8 |
| 7 | `jeeves/write.py` | `_validate_aside_placement` + `_merge_orphan_asides` | 5 |
| 8 | `jeeves/write.py` | `_NARRATIVE_EDIT_SYSTEM_BASE` rewrite (A0 preservation, A0.1 word floor, A12 wit floor, B2 expansion with negative+positive examples) | 0 (covered by step 11) |
| 9 | `jeeves/write.py` | `_invoke_openrouter_narrative_edit` validation gates + word-floor fallback | 4 |
| 10 | `jeeves/prompts/write_system.md` | sectional `<h3>` structure, dedup synthesis 2-sentence floor | 0 |
| 11 | `jeeves/write.py` | per-part min-word retry in `generate_briefing` | 3 |
| 12 | `jeeves/write.py` | `BriefingResult` adds `aside_placement_violations`, `link_density`, `structure_errors` | 2 |
| 13 | smoke regenerate today's briefing from existing session JSON | — | 1 (golden file) |

Total: 13 commits, 40 new tests, ~600 LOC in `jeeves/write.py`, ~10 LOC in `schema.py`, prompt rewrites.

---

## What stays out of scope

- Research phase changes (sectors, dedup ledger, NIM tooling) — these are working.
- Correspondence phase — quality issues are write-phase only.
- New sectors or new fetchers — not what's broken.
- Workflow YAML — sprint 11 is fine.

---

## Acceptance criteria (a pass for this sprint)

A regenerated 2026-05-01 briefing produced by `python scripts/write.py --date 2026-05-01` MUST satisfy ALL of:

1. ✅ Banner `<img class="banner" src="https://i.imgur.com/UqSFELh.png">` present, immediately after `<div class="container">`.
2. ✅ TOTT (Reverend Billy) text ends with the article's actual final sentence — no `[TRUNCATED]`, no mid-word ellipsis. Length ≥ 6000 chars.
3. ✅ ≥ 25 `<a href>` anchors in body text. Sources include career postings (when present), family choir/toddler venues (when present), and literary_pick.
4. ✅ All 5 profane asides appear inline within substantive paragraphs — no standalone templated `[topic] is, [aside].` paragraphs.
5. ✅ Word count of body prose ≥ 3000.
6. ✅ Sign-off reads exactly "Your reluctantly faithful Butler,".
7. ✅ Zero `<p>` tags outside `<div class="container">…</div>`.
8. ✅ Zero stray `</div>` between `<!-- PART_N END -->` sentinels.
9. ✅ All 220+ tests pass.
10. ✅ Logs include `aside_placement_violations: 0`, `structure_errors: 0`, `link_density: ≥ 8`.

---

## Smoke regen note

The session JSON for 2026-05-01 currently has TOTT truncated. After step 1 is shipped, run a one-shot `scripts/research.py --date 2026-05-01 --only newyorker` to refetch (or hand-edit the JSON to restore from Wayback) before regenerating the briefing. This is a one-time fix; future runs will write full text to the JSON.
