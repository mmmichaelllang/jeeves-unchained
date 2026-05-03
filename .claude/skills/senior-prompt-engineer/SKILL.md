---
name: senior-prompt-engineer
description: Audit and improve jeeves-unchained LLM system prompts using 8-dimension scoring. Use when touching write_system.md, research_system.md, correspondence_classify.md, or any per-part PART_INSTRUCTIONS in write.py. Covers: clarity, specificity, constraint enforcement, tone calibration, context budget, anti-pattern prevention, fallback behavior, and output format.
---

# Senior Prompt Engineer — Jeeves

## 8-Dimension Scoring Rubric

Rate 1–5 on each dimension. Target ≥4 on all.

1. **Clarity**: Instructions unambiguous? One reading, not two?
2. **Specificity**: Concrete examples of desired vs forbidden output?
3. **Constraint enforcement**: Are prohibitions stated with WHY, not just MUST NOT?
4. **Tone calibration**: Voice instructions match the briefing's style?
5. **Context budget**: Is the prompt as short as it can be while still working?
6. **Anti-pattern prevention**: Are the specific failure modes (announcing the menu, banned phrases) listed?
7. **Fallback behavior**: Does the model know what to do when data is sparse?
8. **Output format**: Is the HTML/markdown structure shown by example?

## Jeeves-Specific Prompt Anti-Patterns to Eliminate

- "Announcing the menu" (Part 1): listing what sections will cover before covering them
- Banned phrases present in output: check write_system.md's aside pool for any that might appear in instructions
- Redundant continuation rules: CONTINUATION_RULES repeated across parts
- Hedging language in quality standards: "try to", "aim to" — replace with declarative voice
- Over-long constraint lists: compress into principles + examples

## Context Budget Rules

System prompt should not grow unboundedly across parts.
- Part 1 system prompt target: ≤1200 tokens
- Part 4+ system prompt target: ≤2200 tokens (including run_used_asides and used_topics)
- Anything over 3000 tokens in the system prompt at Part 4+ is a red flag
