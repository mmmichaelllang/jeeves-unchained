#!/usr/bin/env python3
"""Automated briefing quality scorer.

Usage:
    python scripts/eval_briefing.py [briefing.html ...]

If no files given, scores the 3 most recent sessions/briefing-*.html files.

Exit code: 0 = all pass (or only warnings), 1 = at least one FAIL.
"""
from __future__ import annotations

import glob
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = REPO_ROOT / "sessions"
WRITE_SYSTEM_MD = REPO_ROOT / "jeeves" / "prompts" / "write_system.md"

# ---------------------------------------------------------------------------
# Part word-count targets (from write.py _PART_WORD_TARGETS)
# ---------------------------------------------------------------------------
PART_TARGETS: dict[str, int] = {
    "part1": 200,
    "part2": 60,
    "part3": 60,
    "part4": 350,
    "part5": 350,
    "part6": 350,
    "part7": 250,
    "part8": 0,
    "part9": 30,
}
WORD_COUNT_WARN_RATIO = 0.60  # warn if words < 60% of target

# ---------------------------------------------------------------------------
# Part labels mapped to h2/h3 section anchors present in the HTML.
# Part 1 starts at the very beginning (after DOCTYPE/head).
# Parts are delimited by the h2 gold-bar headers the model emits.
# ---------------------------------------------------------------------------
# These are the canonical h2 labels from write_system.md + part instructions.
# Part 1 = Sector 1 (greeting + correspondence + weather). No h2 precedes it.
# The h2 headers mark the START of a new major section (part boundary).
H3_PART_MAP: list[tuple[str, str]] = [
    # (h3 text fragment, part label that STARTS at this h3)
    # The canonical h3 headers from write_system.md.
    # Part 1 = greeting block before first h3.
    # Part 2 = "The Domestic Sphere" (local news).
    # Part 3 = "The Calendar" (career + choir + toddler).
    # Part 4 = "The Wider World" (global news).
    # Part 5 = "The Reading Room" (intellectual journals).
    # Part 6 = "The Specific Enquiries" (triadic ontology + AI + UAP).
    # Part 7 = "The Commercial Ledger" (wearable AI + teacher tools).
    # Part 8 = "From the Library Stacks" (vault_insight).
    # Part 9 = newyorker div (identified separately).
    ("The Domestic Sphere", "part2"),
    ("Beyond the Geofence", "part2b"),   # continuation of part2 in some runs
    ("The Calendar", "part3"),
    ("The Wider World", "part4"),
    ("The Reading Room", "part5"),
    ("The Specific Enquiries", "part6"),
    ("The Commercial Ledger", "part7"),
    ("From the Library Stacks", "part8"),
    ("The Library Stacks", "part8"),     # alternate header variant
]
# Part 9 is identified by the .newyorker div.

# Hook-fail patterns for Part 1 (case-insensitive)
HOOK_FAIL_PATTERNS: list[re.Pattern] = [
    re.compile(r"\bIn this briefing\b", re.IGNORECASE),
    re.compile(r"\bToday we(?:'ll| will) cover\b", re.IGNORECASE),
    re.compile(r"\bIn today'?s? issue\b", re.IGNORECASE),
    re.compile(r"\bThis week'?s? briefing\b", re.IGNORECASE),
    re.compile(r"\bWelcome\b", re.IGNORECASE),
    re.compile(r"\bGood morning\b", re.IGNORECASE),
    re.compile(r"\bToday'?s? briefing covers\b", re.IGNORECASE),
    re.compile(r"\bToday I(?:'ll| will)\b", re.IGNORECASE),
    re.compile(r"\bthis morning(?:'s)? briefing\b", re.IGNORECASE),
]

# Banned transition phrases (from write_system.md)
BANNED_TRANSITIONS: list[str] = [
    "Moving on,",
    "Next,",
    "Turning to,",
    "Turning now to",
    "As we turn to",
    "Turning our attention to",
    "In other news,",
    "Closer to home,",
    "Meanwhile,",
    "Sir, you may wish to know,",
    "I note with interest,",
]

# Banned words
BANNED_WORDS: list[str] = ["in a vacuum", "tapestry"]


# ---------------------------------------------------------------------------
# Aside pool extraction
# ---------------------------------------------------------------------------

def _load_aside_pool() -> list[str]:
    """Extract pre-approved profane asides from write_system.md.

    Mirrors jeeves/write.py::_parse_all_asides() logic exactly.
    """
    if not WRITE_SYSTEM_MD.exists():
        return []
    text = WRITE_SYSTEM_MD.read_text(encoding="utf-8")
    m = re.search(
        r'^"clusterfuck of biblical proportions[^\n]+$',
        text,
        flags=re.MULTILINE,
    )
    if not m:
        return []
    return re.findall(r'"([^"]+)"', m.group(0))


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """Remove all HTML tags."""
    return re.sub(r"<[^>]+>", "", html)


def _strip_comments(html: str) -> str:
    """Remove HTML comments."""
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _first_prose_sentence(text: str) -> str:
    """Return the first non-empty sentence-ish fragment from plain text."""
    text = text.strip()
    # Take up to first period/exclamation/question or 200 chars
    m = re.search(r"[.!?]", text[:300])
    if m:
        return text[: m.start() + 1]
    return text[:200]


# ---------------------------------------------------------------------------
# Part splitter
# ---------------------------------------------------------------------------

def _split_into_parts(html: str) -> dict[str, str]:
    """Split a stitched briefing HTML into named parts.

    Strategy:
    1. Truncate at first </html> — briefing files may have raw part drafts
       appended after the closing tag as session artifacts; ignore them.
    2. Strip HTML comments.
    3. Extract newyorker div (part9) by class name.
    4. Split remaining HTML on h3 section headers (canonical from write_system.md).
    5. Everything before the first h3 = part1 (greeting block).

    Returns dict: part_label -> HTML fragment.
    """
    # Truncate at first </html> to exclude appended raw drafts.
    html_close = re.search(r"</html>", html, re.IGNORECASE)
    if html_close:
        html = html[: html_close.end()]

    # Remove HTML comments.
    cleaned = _strip_comments(html)

    parts: dict[str, str] = {}

    # --- Extract part9 (newyorker) first — it's a named div block. ---
    ny_match = re.search(
        r'<div[^>]+class=["\']newyorker["\'][^>]*>.*?</div>',
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if ny_match:
        parts["part9"] = ny_match.group(0)
        cleaned = cleaned[: ny_match.start()] + cleaned[ny_match.end() :]

    # --- Extract signoff (remove it from analysis). ---
    cleaned = re.sub(
        r'<div[^>]+class=["\']signoff["\'][^>]*>.*?</div>',
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # --- Split on h3 section headers. ---
    # Build a combined pattern; use named groups to identify which h3 matched.
    h3_texts = [h3_text for h3_text, _ in H3_PART_MAP]
    h3_pattern = re.compile(
        r"<h3[^>]*>\s*(" + "|".join(re.escape(t) for t in h3_texts) + r")\s*</h3>",
        flags=re.IGNORECASE,
    )

    # Find all h3 boundary positions, mapping to part labels.
    boundaries: list[tuple[int, str]] = []  # (char_pos, part_label)
    seen_labels: set[str] = set()
    for m in h3_pattern.finditer(cleaned):
        matched_text = m.group(1)
        for h3_text, part_label in H3_PART_MAP:
            if h3_text.lower() == matched_text.strip().lower():
                # Merge "part2b" (Beyond the Geofence) into part2.
                effective_label = "part2" if part_label == "part2b" else part_label
                if effective_label not in seen_labels:
                    boundaries.append((m.start(), effective_label))
                    seen_labels.add(effective_label)
                break

    # Sort by position.
    boundaries.sort(key=lambda x: x[0])

    if not boundaries:
        # No h3 headers found — treat everything as part1.
        parts.setdefault("part1", cleaned)
        return parts

    # Everything before the first h3 = part1 (greeting + correspondence + weather).
    parts["part1"] = cleaned[: boundaries[0][0]]

    # Slice remaining segments.
    for i, (pos, label) in enumerate(boundaries):
        end = boundaries[i + 1][0] if i + 1 < len(boundaries) else len(cleaned)
        # Merge into existing part if already present (e.g. two "part2" h3s).
        if label in parts:
            parts[label] += cleaned[pos:end]
        else:
            parts[label] = cleaned[pos:end]

    return parts


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _check_hook(part1_html: str) -> tuple[str, list[str]]:
    """Return (PASS|FAIL|N/A, list of failing patterns found)."""
    text = _strip_tags(part1_html)
    first_sentence = _first_prose_sentence(text)
    fails = []
    for pat in HOOK_FAIL_PATTERNS:
        if pat.search(first_sentence):
            fails.append(pat.pattern)
    status = "FAIL" if fails else "PASS"
    return status, fails


def _check_banned_phrases(part_html: str, aside_pool: list[str]) -> tuple[int, list[str]]:
    """Count aside-pool phrases appearing as raw inline prose (not in any span wrapper).

    In the actual jeeves output, profane asides appear as bare inline text
    within <p> tags. The DRAFT should have zero; the final (after editor pass)
    should have exactly 5. We flag counts outside [0, 7] as suspicious, but
    the primary check is: does the phrase appear at all (as plain text)?

    Returns (count, list_of_phrases_found).
    """
    if not aside_pool:
        return 0, []
    text = _strip_tags(part_html)
    found = []
    for phrase in aside_pool:
        if phrase.lower() in text.lower():
            found.append(phrase)
    return len(found), found


def _check_banned_transitions(part_html: str) -> tuple[int, list[str]]:
    """Return (count, list) of banned transitions found."""
    text = _strip_tags(part_html)
    found = []
    for phrase in BANNED_TRANSITIONS:
        if phrase in text:
            found.append(phrase)
    return len(found), found


def _check_banned_words(part_html: str) -> tuple[int, list[str]]:
    """Return (count, list) of banned words/phrases found."""
    text = _strip_tags(part_html).lower()
    found = []
    for phrase in BANNED_WORDS:
        if phrase in text:
            found.append(phrase)
    return len(found), found


def _count_words(part_html: str) -> int:
    """Strip HTML tags and count words."""
    return len(_strip_tags(part_html).split())


def _count_asides(part_html: str, aside_pool: list[str]) -> int:
    """Count how many aside-pool phrases appear in this part's plain text."""
    if not aside_pool:
        return 0
    text = _strip_tags(part_html)
    return sum(1 for phrase in aside_pool if phrase.lower() in text.lower())


def _check_repetition(parts: dict[str, str]) -> list[str]:
    """Find capitalized multi-word proper noun phrases appearing in 3+ parts.

    Skips part9 (verbatim New Yorker — every entity from the article inflates
    the count, same exclusion as in write.py).
    """
    # Extract capitalized two-word+ phrases (likely proper nouns / named entities).
    prop_noun_re = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")

    # Accumulate per-part entity sets (skip part9).
    part_entities: dict[str, set[str]] = {}
    for label, html in parts.items():
        if label == "part9":
            continue
        text = _strip_tags(html)
        entities = set(prop_noun_re.findall(text))
        part_entities[label] = entities

    # Count how many parts each entity appears in.
    entity_part_count: dict[str, int] = {}
    for entities in part_entities.values():
        for ent in entities:
            entity_part_count[ent] = entity_part_count.get(ent, 0) + 1

    repeated = [ent for ent, count in entity_part_count.items() if count >= 3]
    repeated.sort()
    return repeated


# ---------------------------------------------------------------------------
# Per-file scorer
# ---------------------------------------------------------------------------

def _score_file(path: Path, aside_pool: list[str]) -> bool:
    """Score one briefing HTML file. Returns True if all checks pass."""
    html = path.read_text(encoding="utf-8")
    parts = _split_into_parts(html)

    if not parts:
        print(f"  [WARN] No parts found in {path.name}")
        return True

    all_pass = True
    rows: list[dict] = []

    # Repetition check across all parts.
    repeated = _check_repetition(parts)
    repeat_flag = f"WARN({len(repeated)})" if repeated else "-"

    for label in ["part1", "part2", "part3", "part4", "part5", "part6", "part7", "part8", "part9", "part2b"]:
        part_html = parts.get(label, "")
        if not part_html.strip():
            continue

        row: dict = {"part": label}

        # 1. Hook check (part1 only).
        if label == "part1":
            hook_status, hook_fails = _check_hook(part_html)
            row["hook"] = hook_status
            if hook_status == "FAIL":
                all_pass = False
                row["hook_detail"] = hook_fails
        else:
            row["hook"] = "-"

        # 2. Aside-pool phrase count (all parts except part9).
        if label == "part9":
            row["asides_found"] = "-"
            row["banned_in_prose"] = "-"
        else:
            aside_count = _count_asides(part_html, aside_pool)
            row["asides_found"] = str(aside_count)

            # Banned transitions check.
            trans_count, trans_found = _check_banned_transitions(part_html)
            # Banned words check.
            bword_count, bword_found = _check_banned_words(part_html)
            total_banned = trans_count + bword_count
            row["banned_in_prose"] = str(total_banned) if total_banned else "0"
            if total_banned:
                row["banned_detail"] = trans_found + bword_found
                all_pass = False

        # 3. Word count.
        if label == "part9":
            row["words"] = "-"
            row["word_status"] = "-"
        else:
            words = _count_words(part_html)
            target = PART_TARGETS.get(label, 0)
            row["words"] = str(words)
            if target > 0 and words < target * WORD_COUNT_WARN_RATIO:
                row["word_status"] = f"WARN(<{int(target * WORD_COUNT_WARN_RATIO)})"
            else:
                row["word_status"] = "ok"

        # 4. Repetition — same flag for all parts.
        row["repeat"] = repeat_flag if label == "part1" else "-"

        rows.append(row)

    # Print table.
    header = f"{'PART':<8} {'HOOK':<6} {'BANNED':<8} {'ASIDES':<8} {'WORDS':<8} {'WORD_ST':<16} {'REPEAT'}"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['part']:<8} "
            f"{row['hook']:<6} "
            f"{row.get('banned_in_prose', '-'):<8} "
            f"{row.get('asides_found', '-'):<8} "
            f"{row.get('words', '-'):<8} "
            f"{row.get('word_status', '-'):<16} "
            f"{row['repeat']}"
        )

    # Detail lines for failures.
    for row in rows:
        if row.get("hook_detail"):
            print(f"  [FAIL] {row['part']} hook patterns: {row['hook_detail']}")
        if row.get("banned_detail"):
            print(f"  [FAIL] {row['part']} banned phrases: {row['banned_detail']}")

    if repeated:
        print(f"  [WARN] Repeated entities in 3+ parts ({len(repeated)}): {', '.join(repeated[:10])}"
              + (" ..." if len(repeated) > 10 else ""))

    return all_pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    # Discover briefing files.
    if len(sys.argv) > 1:
        files = [Path(p) for p in sys.argv[1:]]
    else:
        pattern = str(SESSIONS_DIR / "briefing-*.html")
        found = sorted(glob.glob(pattern))
        if not found:
            print("No briefing files found.")
            return 0
        files = [Path(p) for p in found[-3:]]  # last 3

    # Load aside pool once.
    aside_pool = _load_aside_pool()
    if not aside_pool:
        print("[WARN] Could not load aside pool from write_system.md — banned-phrase check disabled.")

    overall_pass = True
    for f in files:
        if not f.exists():
            print(f"[ERROR] File not found: {f}")
            overall_pass = False
            continue
        print(f"\n=== {f.name} ===")
        passed = _score_file(f, aside_pool)
        if not passed:
            overall_pass = False

    return 0 if overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
