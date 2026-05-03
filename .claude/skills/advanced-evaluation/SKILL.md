---
name: advanced-evaluation
description: LLM-as-judge eval harness for jeeves-unchained briefing quality. Use when evaluating whether briefing outputs meet the quality standards in write_system.md: no "announcing the menu", no banned phrases, proper aside usage, Part 1 hook quality. Run against sessions/briefing-*.html files.
---

# Advanced Evaluation — Jeeves

## Quality Dimensions to Score

### 1. Part 1 Hook Quality (pass/fail)
FAIL if Part 1 opens with any of:
- "In this briefing..."
- "Today we'll cover..."
- "In today's issue..."
- "This week's briefing..."
- Any sentence that lists what sections will follow before the first substantive sentence
- Any sentence starting with "Welcome" or "Good morning"

### 2. Banned Phrase Detection (pass/fail per part)
Check each part for phrases from the aside pool. If a banned phrase appears as prose (not as an aside in its designated `<span class="aside">` wrapper), flag it.

### 3. Aside Structure Validity (count)
Count: `<span class="aside">` tags per part. Each part should have ≥1 aside. Zero asides = yellow flag (logged, not failed).

### 4. Word Count per Part (info only)
Extract prose word count per part. Log parts below 60% of target:
- Part 1: 350 words target
- Parts 2-8: 300 words each target  
- Part 9 (New Yorker): not scored (verbatim)

### 5. Repetition Detection (pass/fail)
Check if any proper noun or named entity appears in 3+ different parts. Signals that within-run topic dedup failed.

## Output Format
Tab-separated table to stdout:
```
PART    HOOK    BANNED  ASIDES  WORDS   REPEAT
part1   PASS    0       2       412     -
part2   -       1       1       287     -
```
Exit code 1 if any FAIL; 0 if all pass or only warnings.
