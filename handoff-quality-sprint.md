# JEEVES-UNCHAINED Quality Sprint — Session Handoff

**Date produced:** 2026-05-02  
**Model to use for implementation:** Claude Opus (latest)  
**Estimated scope:** ~200 lines of changes across 2 files + write_system.md

---

## What was asked

Comprehensive analysis of why JEEVES-UNCHAINED output is lower quality than
JEEVES-MEMORY, fix the dedup problem that causes repeated stories across the
briefing, assess Playwright MCP for scraping, and produce this handoff.

---

## What was investigated

- Read both outputs side-by-side (provided in conversation)
- Read `jeeves/dedup.py`, `jeeves/schema.py`, `jeeves/session_io.py`
- Read `jeeves/research_sectors.py` (1200+ lines), specifically:
  - `CONTEXT_HEADER` template (line ~355)
  - `collect_headlines_from_sector` (line 1146) and `_first_sentence` (line 1133)
  - `generate_briefing_session` dedup accumulation (line 175)
  - `load_prior_sessions(days=7)` usage (line 359)
- Read `jeeves/write.py` (3461 lines), specifically:
  - `DEDUP_PROMPT_HEADLINES_CAP = 80` (line 141)
  - `_trim_session_for_prompt` (line 144)
  - `generate_briefing` loop with `used_this_run` aside tracking (line 3032)
  - `PART8_INSTRUCTIONS` — Library Stacks (line 1019)
  - All `PART*_INSTRUCTIONS` part scope assignments
- Read `jeeves/prompts/write_system.md` section list
- Inspected `sessions/session-2026-05-01.json` → 205 covered_headlines, 49 covered_urls

---

## Root causes of repetition (3 independent failure layers)

### Layer 1: Research → research semantic dedup missing

`collect_headlines_from_sector` (research_sectors.py:1146) uses
`_first_sentence(max_chars=150)` to build dedup labels. This truncates at 150
chars and only catches headline-keyed dict fields. Result: the same ProPublica
story appears in `global_news`, `intellectual_journals`, AND
`enriched_articles` because the URL is different per tool call and the headline
string doesn't match across sectors. No cross-sector topic identity check exists.

### Layer 2: Write-phase dedup cap is too small and wrong-directional

`DEDUP_PROMPT_HEADLINES_CAP = 80` (write.py:141). Session has 205 entries
from 7 prior days → Groq sees only 39% of prior coverage. MORE critically:
`covered_headlines` contains *prior-day* entries only. Today's researched
content (in session JSON fields) is NOT in covered_headlines at write time.
So Part 4 writing UAP, Part 6 writing triadic/AI, Part 7 writing wearable AI
each see the full session JSON and re-cover the same material with no knowledge
of what earlier parts wrote.

### Layer 3: No within-write-run topic tracking

`generate_briefing` (write.py:3031) tracks `used_this_run` for *asides only*
(profane phrases). Zero equivalent tracking for topic coverage. Parts 1–8
each receive the full session JSON subset; nothing prevents Part 3 covering
the Iran ceasefire that Part 1 already wrote, or Part 6 re-explaining the
triadic ontology paper that appeared in intellectual_journals (Part 3).

### Layer 4: Library Stacks filler (secondary)

`PART8_INSTRUCTIONS` says: if `vault_insight.available !== true`, output
`<p></p>` and stop. But JEEVES-UNCHAINED output shows generic filler paragraphs
about "the library's collection is a treasure trove." This means Groq is
violating the PART8 scope rule. The instruction exists but the model ignores it.

---

## What Playwright MCP would and wouldn't fix

**Would NOT fix the quality gap.** Confirmed by inspection: the depth difference
between JEEVES MEMORY and JEEVES-UNCHAINED is NOT a scraping quality problem.
JEEVES-UNCHAINED already calls tavily_extract, Jina (r.jina.ai), and exa
(full-text) in its research phase. The Marginalian and NYRB items show genuine
depth when they appear. The quality problem is:
- Repetition obscuring the depth that exists
- Generic AI-voice phrases surviving NIM refine
- Empty Library Stacks section generating filler
- No synthesis/connection between sectors in write

**Playwright MCP costs:** browser process per site, 3–10× slower than HTTP,
anti-scraping detection common on target news sites, paywalled content
(LRB/NYRB) not accessible anyway without authenticated sessions.

**Verdict: Don't switch.** Keep Jina as article extractor. If desired, add
Playwright as a targeted fallback for `lrb.co.uk`/`nybooks.com` only after
Jina fails — do not replace the full stack.

---

## Comprehensive fix plan (implement in this order)

### Fix 1: Raise DEDUP_PROMPT_HEADLINES_CAP (write.py, 1 line)

**File:** `jeeves/write.py` line 141  
**Change:** `DEDUP_PROMPT_HEADLINES_CAP = 80` → `DEDUP_PROMPT_HEADLINES_CAP = 250`  
**Why:** 205 today + prior days = need at least 250. The TPM concern that
motivated 80 is outdated; session JSON itself is the dominant token cost.

---

### Fix 2: Within-write-run topic tracking (write.py)

**File:** `jeeves/write.py`

Add a new function `_extract_written_topics(text: str) -> list[str]` that
extracts key noun phrases from a draft using regex (no NLP library needed):

```python
import re as _re

_TOPIC_SKIP = frozenset({
    "sir", "jeeves", "mister", "lang", "the", "and", "or", "of", "a"
})

def _extract_written_topics(text: str) -> list[str]:
    """Extract ~30 recognizable topic slugs from a rendered HTML draft.
    Used to build within-run topic coverage so subsequent parts don't repeat.
    Targets: capitalized proper-noun sequences, quoted titles, named laws/acts.
    """
    plain = _re.sub(r"<[^>]+>", " ", text)
    # Quoted titles
    titles = _re.findall(r'"([^"]{5,80})"', plain)
    # Capitalized proper noun sequences (2–4 words)
    proper = _re.findall(
        r'\b([A-Z][a-z]{1,}(?:\s[A-Z][a-z]{1,}){1,3})\b', plain
    )
    # Named acts / laws
    acts = _re.findall(r'\b([A-Z][A-Za-z\s]{3,40}(?:Act|Bill|Amendment|Law|Resolution))\b', plain)
    combined = titles + proper + acts
    # Dedupe, lowercase, filter noise
    seen: set[str] = set()
    out: list[str] = []
    for t in combined:
        slug = t.strip().lower()
        if slug in _TOPIC_SKIP or len(slug) < 5:
            continue
        if slug not in seen:
            seen.add(slug)
            out.append(t.strip())
        if len(out) >= 40:
            break
    return out
```

In `generate_briefing` loop (around line 3032), add alongside `used_this_run`:

```python
used_topics_this_run: list[str] = []
```

After each part is written (after the aside-tracking block around line 3094), add:

```python
if label not in _NO_ASIDE_PARTS:
    topics = _extract_written_topics(raw_part)
    used_topics_this_run.extend(t for t in topics if t not in used_topics_this_run)
```

In `_system_prompt_for_parts` signature (line 2927), add parameter:

```python
run_used_topics: list[str] | None = None,
```

In the function body where `run_used_asides` is injected into the prompt, add
after the asides block:

```python
if run_used_topics:
    topic_str = "; ".join(run_used_topics[:40])
    prompt_lines.append(
        f"\n**Topics already written today (one sentence max if repeated):**\n{topic_str}"
    )
```

Pass `run_used_topics=used_topics_this_run` in the `generate_briefing` call to
`_system_prompt_for_parts` (line 3067).

---

### Fix 3: Cross-sector dedup pass in research (research_sectors.py)

**File:** `jeeves/research_sectors.py`

After `enriched_articles` completes and `discovered_headlines` is accumulated
(around line 253), add a cross-sector URL dedup pass that identifies stories
appearing in 2+ sectors:

```python
def _find_cross_sector_dupes(session: dict) -> list[str]:
    """Return list of URL-identified topics appearing in multiple sectors.
    Used to annotate the session so the write phase knows to synthesize
    rather than repeat.
    """
    url_to_sectors: dict[str, list[str]] = {}
    sector_fields = [
        "local_news", "global_news", "intellectual_journals", 
        "wearable_ai", "enriched_articles"
    ]
    for field in sector_fields:
        items = session.get(field) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for url in (item.get("urls") or []):
                if url:
                    url_to_sectors.setdefault(url, []).append(field)
    dupes = []
    for url, sectors in url_to_sectors.items():
        if len(sectors) > 1:
            dupes.append(url)
    return dupes
```

After line 258 (where `session["dedup"]["covered_headlines"]` is set), add:

```python
cross_dupes = _find_cross_sector_dupes(session)
if cross_dupes:
    log.info("Cross-sector duplicate URLs found: %d", len(cross_dupes))
session["dedup"]["cross_sector_dupes"] = cross_dupes
```

Add `cross_sector_dupes: list[str] = Field(default_factory=list)` to
`DeduplicateModel` in `jeeves/schema.py`.

---

### Fix 4: Improve headline extraction quality (research_sectors.py)

**File:** `jeeves/research_sectors.py`

In `collect_headlines_from_sector` (line 1146), change the findings-sentence
extraction to capture 2 sentences instead of 1 for deep/news sectors:

```python
elif k in _FINDINGS_LIKE_KEYS and isinstance(v, str) and v.strip():
    # Use first 2 sentences for deep findings (more recognizable for dedup)
    sentence = _first_sentence(v, max_chars=300)
    if sentence:
        out.append(sentence)
```

And change `_first_sentence` default `max_chars=150` → `max_chars=250` to
preserve more of the title/headline before truncation.

---

### Fix 5: Strengthen NIM refine banned phrases (write.py)

**File:** `jeeves/write.py`

In `_REFINE_SYSTEM` (search for the string around line ~1300), add to the
banned phrases list:

```
- "This development is a positive step"
- "This is a fascinating contribution"
- "I must attend to the rest of the briefing"
- "It will be interesting to see"
- "It will be worth monitoring"
- "This raises important questions about"
- "It will be worth tracking"
- "This highlights the complexities of"
- "demonstrates the city's commitment to"
- "represents a significant step forward"
- "The variety of positions available is quite impressive"
- "I shall continue to monitor the situation"
```

---

### Fix 6: Harden Library Stacks (PART8) against filler (write.py)

**File:** `jeeves/write.py` — `PART8_INSTRUCTIONS` (line 1019)

Add stronger examples of what NOT to write when vault_insight is unavailable.
Currently the instruction says "Output EXACTLY this and nothing else: `<p></p>`"
but Groq ignores it. Add:

```
FORBIDDEN OUTPUTS when vault_insight.available !== true:
- "The library's collection is a treasure trove..."
- "The library's commitment to providing..."  
- "I have been browsing..." (only allowed when available === true)
- Any mention of books, collections, knowledge, resources, or learning
- Any pivot to another topic
- Any explanation that vault insight is unavailable

Test yourself: your entire output before the sentinel should be
exactly 7 characters: `<p></p>`. If it is longer, you have violated scope.
```

---

## Files to change (summary)

| File | Lines affected | Change |
|---|---|---|
| `jeeves/write.py` | 141 | Raise cap 80→250 |
| `jeeves/write.py` | ~3032 | Add `used_topics_this_run: list[str] = []` |
| `jeeves/write.py` | ~3094 | Populate `used_topics_this_run` after each part |
| `jeeves/write.py` | ~2927 | Add `run_used_topics` param to `_system_prompt_for_parts` |
| `jeeves/write.py` | ~2985 | Inject topics into prompt |
| `jeeves/write.py` | ~3067 | Pass `run_used_topics` in call |
| `jeeves/write.py` | New function | `_extract_written_topics()` |
| `jeeves/write.py` | `_REFINE_SYSTEM` | Add 12 banned filler phrases |
| `jeeves/write.py` | `PART8_INSTRUCTIONS` | Harden against vault filler |
| `jeeves/research_sectors.py` | ~1133 | `_first_sentence` max_chars 150→250 |
| `jeeves/research_sectors.py` | ~1160 | Extract 2 sentences not 1 |
| `jeeves/research_sectors.py` | ~258 | Add `_find_cross_sector_dupes()` + store result |
| `jeeves/schema.py` | `DeduplicateModel` | Add `cross_sector_dupes: list[str]` field |

---

## Tests to write

- `test_extract_written_topics_captures_proper_nouns`
- `test_extract_written_topics_captures_quoted_titles`
- `test_find_cross_sector_dupes_identifies_repeated_urls`
- `test_dedup_cap_is_250_in_trim_session`
- `test_first_sentence_respects_250_chars`
- `test_collect_headlines_extracts_two_sentences_for_findings`

Add to `tests/test_write_postprocess.py` and `tests/test_research_sectors.py`.

---

## What NOT to do

- Do NOT switch to Playwright MCP for primary scraping. The depth gap is
  model+synthesis, not scraping quality. Jina handles JS rendering adequately.
- Do NOT remove the `covered_urls` drop from `_trim_session_for_prompt` —
  that was a correct TPM optimization.
- Do NOT raise `max_tokens` above 4096 per Groq call (daily budget constraint).
- Do NOT change the 7-day prior session window — the issue is cross-sector
  within-day duplication, not day-over-day.

---

## Expected outcome

After these fixes:
- ProPublica/FRONTLINE story appears once (in global_news part), not 3 times
- UAP section appears once, not duplicated in "Specific Enquiries"  
- Library Stacks emits `<p></p>` when no vault insight, not generic filler
- Groq parts 4–8 each get a `used_topics_this_run` list that grows as earlier
  parts are written → dramatically fewer full repeats
- Day-over-day: 250 covered_headlines means the model sees full prior coverage

These changes do NOT require a new branch — they can go direct to main
following the sprint 11 pattern. Run `pytest tests/` (190+ tests) before push.
