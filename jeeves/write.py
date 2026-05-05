"""Phase 3 — Groq Llama 3.3 70B renders a session JSON into Jeeves-voice HTML."""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    Config,
    DEDUP_PROMPT_HEADLINES_CAP,
    DEDUP_PROMPT_ASIDES_CAP,
    DEDUP_PROMPT_TOPICS_CAP,
)
from .schema import SessionModel

log = logging.getLogger(__name__)

WRITE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "write_system.md"
EMAIL_SCAFFOLD_PATH = Path(__file__).resolve().parent / "prompts" / "email_scaffold.html"


@dataclass
class BriefingResult:
    html: str
    coverage_log: list[dict[str, Any]]
    word_count: int
    profane_aside_count: int
    banned_word_hits: list[str]
    banned_transition_hits: list[str]
    aside_placement_violations: list[str] = None  # type: ignore[assignment]
    link_density: float = 0.0
    structure_errors: list[str] = None  # type: ignore[assignment]
    quality_warnings: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.aside_placement_violations is None:
            self.aside_placement_violations = []
        if self.structure_errors is None:
            self.structure_errors = []
        if self.quality_warnings is None:
            self.quality_warnings = []


@dataclass
class RunManifest:
    """Structured summary of a write-phase run, written to sessions/run-manifest-DATE.json.

    Gives post-run visibility into quality-degradation events without requiring
    manual inspection of the HTML.  Committed to git → queryable history.
    """

    date: str
    groq_parts: int
    nim_fallback_parts: int
    nim_refine_succeeded: int
    nim_refine_failed: int
    briefing_word_count: int
    profane_aside_count: int
    banned_word_hits: list[str]
    banned_transition_hits: list[str]
    quality_warnings: list[str]
    quality_score: int  # 0–100 structural score

    @classmethod
    def from_briefing_result(cls, result: "BriefingResult", date: str,
                             groq_parts: int, nim_fallback_parts: int) -> "RunManifest":
        refine_warnings = [w for w in result.quality_warnings if "nim_refine" in w]
        total_parts = groq_parts + nim_fallback_parts
        score = _compute_quality_score(result)
        return cls(
            date=date,
            groq_parts=groq_parts,
            nim_fallback_parts=nim_fallback_parts,
            nim_refine_succeeded=max(0, total_parts - len(refine_warnings)),
            nim_refine_failed=len(refine_warnings),
            briefing_word_count=result.word_count,
            profane_aside_count=result.profane_aside_count,
            banned_word_hits=result.banned_word_hits,
            banned_transition_hits=result.banned_transition_hits,
            quality_warnings=result.quality_warnings,
            quality_score=score,
        )


def _compute_quality_score(result: "BriefingResult") -> int:
    """Structural quality score 0–100 for the briefing.

    Dimensions (each 0 or full points):
      word_count ≥ 5000         → 25 pts
      aside_count ≥ 5           → 20 pts
      banned_word_hits = 0      → 20 pts
      banned_transition_hits = 0 → 20 pts
      quality_warnings = 0      → 15 pts
    """
    score = 0
    if result.word_count >= 5000:
        score += 25
    if result.profane_aside_count >= 5:
        score += 20
    if not result.banned_word_hits:
        score += 20
    if not result.banned_transition_hits:
        score += 20
    if not result.quality_warnings:
        score += 15
    return score


# --- Signoff guard ---------------------------------------------------------
_SIGNOFF_REPLACEMENT = "Your reluctantly faithful Butler,"

# `Yours faithfully`, `Your faithfully` (typo), with optional `Butler` and `,`
# Group 0 captures the entire thing for replacement.
_WRONG_SIGNOFF_FAITHFULLY = re.compile(
    r"\bYours?\s+faithfully(?:\s+Butler)?,?",
    re.IGNORECASE,
)
# Other generic sign-offs that the model occasionally regresses to.
_WRONG_SIGNOFF_OTHERS = re.compile(
    r"\b(?:Sincerely(?:\s+yours)?|Yours\s+sincerely|Yours\s+truly|"
    r"Best\s+regards|Kind\s+regards|Warm\s+regards|Respectfully\s+yours)(?:\s+Butler)?,?",
    re.IGNORECASE,
)


BANNED_WORDS = [
    "in a vacuum",
    "tapestry",
    # Apologetic recovery phrases — the profane asides stand alone.
    # NB: "if you'll excuse the expression" is omitted — it appears verbatim
    # inside the pre-approved aside "is, if you'll excuse the expression,
    # ass-backward" and would cause a false positive if listed here.
    "I do beg your pardon, Sir",
    "pardon my language",
    "if I may say so",
]
BANNED_TRANSITIONS = [
    "Moving on,",
    "Next,",
    "Turning to",
    "Turning now to",
    "As we turn to",
    "Turning our attention to",
    "In other news,",
    "Closer to home,",
    "Meanwhile,",
    "Sir, you may wish to know,",
    "I note with interest,",
    "stark reminder of ongoing instability",
    "significant implications for the region",
    "significant implications for the Prime Minister",
    "significant escalation of the conflict",
    "will be important to monitor",
    "worth watching in the coming",
]
# Lower-cased fragments drawn from the pre-approved aside list. Used to count asides.
PROFANE_FRAGMENTS = [
    "clusterfuck",
    "shitshow",
    "fuckfest",
    "horse-shit",
    "fucked",
    "goddamn",
    "fuck-ton",
    "thundercunt",
    "shittery",
    "omnishambles",
    "shit-storm",
    "fucking",
    "cock-womble",
    "disaster-class",
    "godforsaken",
    "dog-shit",
    "balls-up",
    "train-wreck",
    "bollocks",
    "cluster-fuck",
    "piss-take",
    "shit-weasels",
    "fuck-knuckles",
    "horse-piss",
    "dog-fuckery",
    "shit-heap",
    "fuck-sticks",
    "ass-backward",
    "goat-fuck",
    "fuck-bucket",
    "cock-waffle",
    "shit-sandwich",
    "fuck-wits",
    "shit-show",
    "ass-wipe",
    "thundercunts",
    "fuck-parade",
    "shit-fountain",
    "fuck-trumpets",
    "wank-puffin",
    "fuck-pantry",
    "shit-tornado",
    "shit-cake",
    "knob-rot",
    "cock-up",
]


def load_write_system_prompt() -> str:
    return WRITE_PROMPT_PATH.read_text(encoding="utf-8")


# Module-level cache so 9 sequential Groq calls don't re-read the same files.
_WRITE_SYSTEM_PROMPT_CACHE: str | None = None
_EMAIL_SCAFFOLD_CACHE: str | None = None


def _cached_write_system_prompt() -> str:
    global _WRITE_SYSTEM_PROMPT_CACHE
    if _WRITE_SYSTEM_PROMPT_CACHE is None:
        _WRITE_SYSTEM_PROMPT_CACHE = load_write_system_prompt()
    return _WRITE_SYSTEM_PROMPT_CACHE


def _cached_email_scaffold() -> str:
    global _EMAIL_SCAFFOLD_CACHE
    if _EMAIL_SCAFFOLD_CACHE is None:
        _EMAIL_SCAFFOLD_CACHE = load_email_scaffold()
    return _EMAIL_SCAFFOLD_CACHE


def load_email_scaffold() -> str:
    """Return the canonical HTML scaffold including CSS.

    Loaded separately from the system prompt so the scaffold can be updated
    without touching write_system.md.  The scaffold is injected into the
    system prompt where the placeholder ``{EMAIL_SCAFFOLD}`` appears.
    If no placeholder is found the scaffold text is appended at the end of
    the system prompt (backward-compatible fallback).
    """
    return EMAIL_SCAFFOLD_PATH.read_text(encoding="utf-8")


def build_write_system_prompt_with_scaffold() -> str:
    """Return the system prompt with the email scaffold injected.

    Replaces ``{EMAIL_SCAFFOLD}`` in write_system.md with the contents of
    email_scaffold.html.  Falls back to appending if the placeholder is absent
    (e.g. during tests that pass a minimal system prompt).
    """
    prompt = load_write_system_prompt()
    scaffold = load_email_scaffold()
    placeholder = "{EMAIL_SCAFFOLD}"
    if placeholder in prompt:
        return prompt.replace(placeholder, scaffold)
    return prompt


# DEDUP_PROMPT_*_CAP constants imported from jeeves.config (authoritative source)


def _trim_session_for_prompt(session: SessionModel) -> dict[str, Any]:
    """Prep the session JSON for the Groq user message.

    The on-disk session JSON is the durable artifact; this shrinks a copy to
    stay under Groq's 12k TPM ceiling on `llama-3.3-70b-versatile` free tier.

    - Drops `dedup.covered_urls` entirely from the prompt. Jeeves reasons
      about skim/skip by *headline*, not URL; the URL list is a
      research-phase artifact used for skipping re-fetches on the next day.
      Empirically dedup.covered_urls was ~25% of the payload.
    - Caps `dedup.covered_headlines` at a generous top N — still enough to
      drive the three-tier dedup directive (exact match / skim / full).
    - No structural changes to the researched sectors themselves — the
      research phase's FIELD_CAPS already bound those.
    """

    payload = session.model_dump(mode="json")
    dedup = payload.get("dedup") or {}
    if isinstance(dedup, dict):
        dedup.pop("covered_urls", None)
        if isinstance(dedup.get("covered_headlines"), list):
            # Truncate each headline to 80 chars — research phase pulls "first
            # two sentences" of findings strings (research_sectors.py:1276) so
            # individual entries can be 200-300 chars. The write phase only
            # needs short prefixes to detect "have I covered this?" — verbose
            # entries blow past the 12k Groq TPM ceiling and force every part
            # to fall through to NIM. Sprint-17 hotfix 2026-05-04.
            raw = dedup["covered_headlines"][:DEDUP_PROMPT_HEADLINES_CAP]
            dedup["covered_headlines"] = [
                (h[:77] + "…") if isinstance(h, str) and len(h) > 80 else h
                for h in raw
            ]
        # cross_sector_dupes (URLs appearing in 2+ research sectors) is
        # computed by research phase. Surface it here so write-phase prompts
        # can reference it — earlier the field was computed but ignored.
        if isinstance(dedup.get("cross_sector_dupes"), list):
            dedup["cross_sector_dupes"] = dedup["cross_sector_dupes"][:50]
    return payload


def build_user_prompt_from_payload(payload: dict[str, Any]) -> str:
    return (
        "Here is the research session JSON. Render the briefing now in Jeeves's "
        "voice, following every rule in the system prompt. Output HTML only, "
        "starting with <!DOCTYPE html>.\n\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```"
    )


# -- Nine-call render (free-tier 12k TPM ceiling) --
#
# llama-3.3-70b on Groq's free `on_demand` tier caps at 12k TPM. The full
# system prompt (persona + rules + full profane-aside pool + coverage-log +
# output rules) plus per-part instructions runs ~10k chars (~7.7k tokens)
# on its own — there's no headroom for a rich session JSON in one call.
# We keep the system prompt intact and split the user payload across NINE
# sequential Groq calls with a 65s sleep between each so the rolling 60s TPM
# window clears. ~10 min total wall-clock. Policy: safety and quality over
# wall-clock; split further rather than compress instructions.
#
# Each PART handles one slice of the briefing. The first opens the document;
# the last closes it with the sign-off and the coverage-log placeholder.
# Parts 8 and 9 are split so the verbatim New Yorker article gets its own
# full token budget — preventing the model from "helpfully" summarising or
# inventing content to compensate for a crowded payload.

# (name, session_field_list). The 9-slot plan balances per-part user-payload
# chars so no call creeps above the free-tier 12k TPM ceiling. Heaviest fields
# (local_news, career, ai_systems, newyorker) each get their own slot; smaller
# fields are paired.
PART_PLAN: list[tuple[str, list[str]]] = [
    ("part1", ["correspondence", "weather"]),
    ("part2", ["local_news"]),
    ("part3", ["career"]),
    ("part4", ["family", "global_news", "newyorker_hint"]),
    ("part5", ["intellectual_journals", "enriched_articles"]),
    ("part6", ["triadic_ontology", "ai_systems"]),
    ("part7", ["uap", "uap_has_new", "wearable_ai", "newyorker_hint", "literary_pick"]),
    ("part8", ["vault_insight"]),
    ("part9", ["newyorker"]),
]


# Per-part minimum word counts. Below this, the part is logged as "thin"
# (likely indicates Groq mid-stream truncation, dedup over-pruning, or
# model under-delivering). Used as a diagnostic signal only — does not
# trigger automatic retry (would double the wall-clock).
_PART_WORD_TARGETS: dict[str, int] = {
    "part1": 200,
    "part2": 60,    # empty-feed branch is OK at ~30 words
    "part3": 60,    # empty-feed branch is OK at ~30 words
    "part4": 350,
    "part5": 350,
    "part6": 350,
    "part7": 250,
    "part8": 0,     # often empty placeholder
    "part9": 30,    # placeholder + one sentence + signoff fragment
}

# Back-compat aliases — a few tests import PART1_SECTORS et al.
PART1_SECTORS = PART_PLAN[0][1]
PART2_SECTORS = PART_PLAN[1][1]
PART3_SECTORS = PART_PLAN[2][1]


PART1_INSTRUCTIONS = """

---

## PART 1 of 9 — render instructions

You are writing PART 1 of a nine-part briefing. Output the full HTML opening
starting with the DOCTYPE and including the EXACT stylesheet below — then the
masthead structure with today's full weekday date filled in, then:

**MANDATORY HTML OPENING — copy this exactly, filling in only the date:**

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * { box-sizing: border-box; }
    body { font-family: Georgia, 'Times New Roman', serif; background: #0a0a0a; color: #1a1714; margin: 0; padding: 48px 16px 80px; font-size: 17px; }
    .container { max-width: 660px; margin: 0 auto; background: #fdfaf5; border: 1px solid #bfb090; line-height: 1.88; }
    .banner { display: block; width: 100%; margin: 0; padding: 0; border: 0; }
    .mh-date { background-color: #0c1015; color: #8899aa; margin: 0; padding: 36px 56px 48px; font-size: 0.72em; font-style: italic; text-align: center; letter-spacing: 0.08em; border-bottom: 3px solid #c8902a; }
    h2 { background-color: #0c1015; color: #c8902a; margin: 3.2em 0 0; padding: 24px 56px; font-size: 0.55em; font-weight: normal; text-transform: uppercase; letter-spacing: 0.6em; border-top: 3px solid #c8902a; }
    h3 { font-size: 1.1em; font-weight: bold; font-style: italic; color: #18375a; margin: 2em 40px 0.5em; padding: 0 0 0 20px; border-left: 4px solid #c8902a; line-height: 1.4; }
    p { margin: 0 56px 1.5em; padding: 0; }
    .mh-date + p { margin-top: 2.6em; }
    h2 + p { margin-top: 1.4em; }
    a { color: #18375a; text-decoration: none; border-bottom: 1px solid #88a8c8; }
    .dc { float: left; font-size: 5em; line-height: 0.68; padding-right: 8px; padding-top: 5px; color: #c8902a; font-weight: bold; }
    .ny-header { font-size: 0.58em; text-transform: uppercase; letter-spacing: 0.45em; color: #c8902a; margin-bottom: 20px; padding-bottom: 14px; border-bottom: 1px solid #c8a040; }
    .newyorker { background-color: #f0e8d2; border-top: 3px solid #c8902a; border-bottom: 3px solid #c8902a; margin: 3em 0; padding: 32px 56px 36px; }
    .newyorker p { margin: 0 0 1.2em; padding: 0; }
    .newyorker p:last-child { margin-bottom: 0; }
    .signoff { border-top: 3px solid #c8902a; padding: 36px 56px 62px; font-style: italic; text-align: right; color: #5a4828; margin-top: 2em; }
    .signoff p { margin: 0; padding: 0; line-height: 1.9; }
  </style>
</head>
<body>
<div class="container">
<img class="banner" src="https://i.imgur.com/UqSFELh.png" alt="">
<div class="mh-date">[FULL WEEKDAY DATE e.g. Tuesday, 29 April 2026]</div>
```

Do NOT alter these styles. Do NOT add extra CSS. Do NOT use Arial, sans-serif,
or any other font. The body font is Georgia. The container is max-width 660px.

Then write the sector 1 content:

- Sector 1 opening material: the formal butler greeting to Mister Lang, the
  correspondence summary (if `correspondence.found=true`), and the weather
  forecast from `weather`.

**DROP CAP — MANDATORY:** The opening greeting paragraph must begin with a
drop cap on the first letter, like this:

`<p><span class="dc">G</span>ood morning, Mister Lang. ...`

Use exactly `<span class="dc">` around the first letter only. No other
paragraph uses this class.

**OPENING GREETING — MANDATORY QUALITY STANDARD:**
The first sentence Mister Lang reads must have specific Wodehousian character.
BANNED opening gambits (these are generic AI-assistant openers, not Jeeves):
- "Your loyal butler shall guide you through the day's intelligence briefing,
  covering a wide range of topics from [X] to [Y]."
- "Good morning, Mister Lang. Allow me to present your daily briefing."
- "I shall provide a summary of the day's intelligence."
- Any opener that describes the briefing's structure or contents.

Jeeves does not announce the menu. He begins serving.
GOOD openers plunge directly into the most striking thing on the docket
with a dry comment, understatement, or Wodehousian irony:
- "The world has not improved overnight, Mister Lang, though it has at least
  produced several new opportunities to observe it failing."
- "The correspondence is thin this morning, which I choose to interpret as
  a mercy rather than an oversight."
- "April persists, Sir. The weather confirms this."

**Empty weather rule:** If `weather` is an empty string, write exactly this
one line and then move on — do not invent, speculate, or apologise:
  <p>The weather forecast is unavailable this morning, Sir.</p>

**Correspondence summary rule:** If `correspondence.found=true` and
`correspondence.fallback_used=false`, open with: *"The morning's correspondence
has already been laid out in full, Sir, but the salient matters are these…"*
and condense `correspondence.text` to roughly 400 words in Jeeves's voice.
If `fallback_used=true`, summarize naturally without that opener.

**CORRESPONDENCE FORMAT — CRITICAL:** The correspondence summary must be
PLAIN `<p>` PARAGRAPHS ONLY. Do NOT reproduce any `<h3>` section headers
from the correspondence text. Do NOT re-impose section structure. The previous
briefing had sections — ignore them. Condense the substance into 2–4 flowing
prose paragraphs. No sub-headings, no bullet points, no structural markup
beyond `<p>` and inline `<a>` anchors.

**Hard prohibitions — do NOT violate:**
- NEVER use the words "in a vacuum" or "tapestry".
- NEVER use mechanical transition phrases to change topics. ALL of the following
  are banned: "Moving on,", "Next,", "Turning to,", "Turning now to,",
  "Turning our attention to,", "As we turn to,", "In other news,",
  "Closer to home,", "Meanwhile,", "Sir, you may wish to know,",
  "I note with interest,". Begin the next topic directly, use dark humour,
  or let understatement carry the shift. Do NOT suggest these as alternatives.
- NEVER append apologetic recovery phrases to profane asides. BANNED follow-ups:
  "— I do beg your pardon, Sir", "pardon my language", "if you'll excuse the
  expression", "if I may say so", or any variant. The aside stands alone.
  Jeeves does not apologise for the language; Mister Lang is no longer
  scandalised.

Length is proportional to source material — typically 400-700 words for a
day with full correspondence + weather + Edmonds municipal news, shorter
when any of those are empty. Do NOT pad to a target. No profane asides —
the final editor adds them. When Sector 1 opening is complete,
emit the literal comment `<!-- PART1 END -->` and STOP.

Do NOT write local_news yet — Part 2 handles it. Do NOT write the sign-off.
Do NOT close `</div>`, `</body>`, or `</html>`. Later parts continue the document.
"""

# Shared continuation rules prepended to parts 2–9.  Parts 2–9 are strict
# continuations of the document Part 1 opened; the model must NOT reopen
# with a greeting, must NOT re-cover topics from other parts, must obey
# the banned-word / banned-transition list, and must NEVER append an
# apologetic recovery phrase after a profane aside.
CONTINUATION_RULES = """

**CONTINUATION RULES — CRITICAL, DO NOT VIOLATE:**

1. NO greeting. You are mid-document. Do NOT write "Good morning, Mister Lang"
   or any variant. Part 1 already greeted. Open DIRECTLY with a transition
   into your first assigned topic.
2. STAY IN YOUR LANE. Write ONLY about the sectors/fields listed below. Do
   NOT reference, summarize, preview, or even mention topics from other
   parts (e.g., weather, correspondence, local news, career, family, global
   news, journals, triadic ontology, AI systems, UAP, wearables, library
   stacks, New Yorker) unless that topic IS in your assigned list.
3. No greeting, no "Good day", no date, no weather note, no summary of
   earlier parts. No "as we continue" or "as we proceed" meta-commentary.
4. BANNED WORDS (never use): "in a vacuum", "tapestry".
5. BANNED TRANSITIONS (never use): "Moving on,", "Next,", "Turning to,",
   "Turning now to", "Turning our attention to", "As we turn to",
   "In other news,", "Closer to home,", "Meanwhile,", "Sir, you may wish
   to know,", "I note with interest,".
   CONCRETE FAILURES — these exact phrases appeared in prior drafts and are
   forbidden: "Turning to family and global news,", "As we turn to teaching
   opportunities,", "As we consider the developments in UAP disclosure,".
   Begin the next topic directly, or acknowledge a jarring shift with dark
   humour or understatement. Never glide mechanically between a tragedy and
   a choral audition. If you catch yourself writing any "Turning to" or
   "As we [verb] to" opener, DELETE the entire sentence and begin again.
   ALSO BANNED — closing-paragraph openers: "Considering the implications
   of these findings", "Both [X] and [Y] are undergoing significant",
   "Both [X] and [Y] continue to", "Continuing to monitor the progress",
   "These are exciting developments in", "These pieces demonstrate the
   importance of". Any sentence opening a closing paragraph that could
   apply to a completely different topic — delete it entirely.
6. NO APOLOGIES AFTER PROFANE ASIDES. The asides stand alone. Never append
   "— I do beg your pardon, Sir", "pardon my language", "if you'll excuse
   the expression", "if I may say so", or any apologetic recovery phrase.
   The profanity is intentional; Jeeves does not disclaim it.
7. Raw HTML paragraphs only. NO `<!DOCTYPE html>`, NO `<head>`, NO `<body>`,
   NO new `<h1>`.
8. NO BARE URLs IN PROSE. Every external URL must be wrapped in an `<a href>`
   anchor with natural-language anchor text. Never show raw "https://..." in
   body text. Write: `<a href="URL">The Guardian reports…</a>` — not
   "See https://..." or "Source: https://...".
9. NO WEATHER. The weather forecast is owned exclusively by Part 1. Any
   weather mention in Part 2 through Part 9 is a violation.
10. SYNTHESIS INTELLIGENCE. Every topic you cover may have prior coverage in
    `dedup.covered_headlines`. Think in four cases:
    (a) Static repeat — no new development → one sentence, move on.
    (b) Ongoing story with new development → SYNTHESIZE: anchor in what was
        known, pivot immediately to what has changed. This is the highest
        form of briefing — the reader should feel the story advancing.
    (c) Recurring series/listings → advance to next uncovered item (see
        per-part protocols). One backward-reference clause only.
    (d) Genuinely new → full depth.
    Exception: prior data may appear as supporting context for a new fact —
    one brief reference is fine if it illuminates today's development.
    Never skip an ongoing story just because it appeared before. Never repeat
    one just because it reappeared in today's findings. Synthesize.
    CROSS-SECTOR DUPES: `dedup.cross_sector_dupes` is a list of URLs that
    appear in MORE THAN ONE research sector for today's session. If a URL
    you are about to cite is in that list AND another part has already cited
    it (or you are not the first sector that surfaces it), do NOT re-narrate
    the underlying story. One bridging clause max.
11. WIT IS PERMITTED, NOT REQUIRED. A sardonic or dry observation lands when
    it grows out of the specific content just described. If nothing in this
    part begs for an aside, write none. Forced wit is worse than no wit.
    Do NOT add a wit-only paragraph at the end of a section to satisfy this
    rule — the rule is satisfied by zero asides if zero asides are warranted.
    It need not be profane — a well-timed "Naturally." or "One had hoped
    otherwise." lands harder than gratuitous language. If you complete your
    writing and have included zero wit, insert one before the sentinel.
    Good wit is specific: it reacts to the content just described.
    Bad wit: "the situation is, as ever, complex." (generic)
    Good wit: "The council voted unanimously, which given recent history
    suggests the decision was either obviously correct or unopposed for
    reasons no one will admit." (specific, ironic)
12. NO FOURTH-WALL BREAKS. Never describe your own structure, word limits,
    or instructions. BANNED verbatim patterns:
    - "With a mere [N] words allocated to this sub-section"
    - "With only [N] words to cover"
    - "This section will cover" / "In this section"
    - "As allocated by the briefing structure"
    - Any sentence explaining WHY you are being brief. If you are being
      brief, simply be brief.
13. EXPANDED BANNED PHRASES — delete or rewrite on sight, no exceptions.
    These are drawn from actual bad Groq output. Every one of these is the
    voice of an AI assistant, not a Wodehousian butler:
    - "In synthesizing these findings, it becomes apparent that"
    - "In a similar vein," (as a topic pivot)
    - "Upon reviewing" (any form: "Upon reviewing the job boards…")
    - "Regarding [topic]," (as a section opener)
    - "With regard to [topic]," (as a section opener)
    - "In the realm of [topic]," / "Delving into the realm of"
    - "As we delve into" / "as we delve deeper into"
    - "it is worth noting that" / "it should be noted that"
    - "it becomes apparent" / "it becomes clear"
    - "shall guide you through" / "I shall guide"
    - "covering a wide range of topics"
    - "I shall keep a watchful eye"
    - "it is vital to continue monitoring"
    - "it will be essential to stay vigilant and adapt"
    - "it remains to be seen whether"
    - "these developments are noteworthy"
    - "This level of activity suggests [X] is undergoing significant"
    - "significant implications for the region and the global community"
    - "significant implications for [X]'s leadership"
    - "significant escalation of the conflict"
    - "stark reminder of ongoing instability"
    - "will be important to monitor [X] in the coming"
    - "it is essential to stay informed"
    - "I hope this briefing has been informative"
    - "this briefing has covered"
    - "this concludes" / "this completes"
    - Any sentence beginning "Mister Lang, this briefing" (meta-closing)
    - "as reported by [Source]," (mid-sentence attribution boilerplate) — write
      `<a href="URL">Source</a> reports that…` instead; do not embed the source
      as a passive parenthetical clause
    - "This decision, as reported by" / "This development, as reported by"
    - "This development highlights the importance of" / "This decision highlights"
    - "This initiative underscores" / "This move underscores" / "This decision underscores"
    - "This raises questions about the balance" / "This raises serious concerns about"
    - "Mister Lang, [topic] [verb phrase]," as a paragraph opener in the MIDDLE of a
      section — address the reader once per section at most; repeated "Mister Lang,"
      openers are a mechanical tic, not a voice
    - Any sentence that could be copy-pasted unchanged into a briefing about
      a completely different topic. Zero topic-specific nouns = delete it.
    SIGNIFICANCE COMMENTARY — delete every instance, no exceptions. These are
    the worst class of AI filler: sentences whose only job is to declare that
    something is significant, important, complex, or worthy of attention:
    - "This is a significant development" / "This is a concerning/fascinating/
      disturbing/noteworthy/significant development" (delete the entire sentence)
    - "it highlights the need for careful consideration of the consequences"
    - "It is a complex issue, to be sure" / "It is a complex issue that"
    - "requires a nuanced approach, rather than a simplistic or heavy-handed one"
    - "one can only hope that" / "One can only hope" (any form)
    - "I would like to bring to your attention" / "I would like to recommend"
    - "please do not hesitate to inform them" / "please do not hesitate to apply"
    - "The implications of this research are significant"
    - "As you continue to explore this subject" / "As you explore this topic"
    - "The synthesis of these [X] works highlights" / "The synthesis of these findings"
    - "it is a reminder that the world is a complex and often dangerous place"
    - "one that underscores the need for international cooperation and diplomacy"
    - "One would hate to think that"
    - "it is a positive development" / "this is a positive trend"
    - "I trust this morning finds you well" (banned in Parts 2–9; Part 1 only)
14. LINKING IS MANDATORY WHEN A URL EXISTS IN YOUR PAYLOAD. Every sector
    item in your payload has a `urls` array (or a `url` field). When you
    write about that item, you MUST embed the URL as an anchor. Skipping
    a link when the URL is sitting right there in your payload is a failure.
    HOW: For a `global_news` item `{source:"BBC", urls:["https://bbc.com/..."]}`,
    write: `<a href="https://bbc.com/...">BBC</a> reports that…`
    For an `intellectual_journals` item, link the publication name or the
    article title with `urls[0]`. For `wearable_ai`, link the product name
    or site name. For `career`, link each job posting title to its `url`.
    ANTI-PATTERN: writing "The Guardian reports…" with no anchor when
    `urls[0]` from the Guardian item is in your payload = violation.
    HARD LIMIT: Never invent a URL — every href must appear verbatim in
    your payload. If genuinely no URL is provided for a source, write
    unlinked text. But "no URL" does not mean "the urls array is non-empty
    but I didn't use it" — that IS a URL, use it.
"""


PART2_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 2 of 9 — local news

Part 1 opened the HTML and covered the greeting, correspondence, and weather.
You pick up from there.

**SECTION HEADER — MANDATORY:** Begin with exactly `<h3>The Domestic Sphere</h3>`
before your first paragraph of content. This is the ONLY part that emits this
header — Parts 3 and 4 continue the section without repeating it.

Your scope — write ONLY about these:
- Municipal / Edmonds items from `local_news` whose category is
  municipal/civic/development.
- Public-safety items from `local_news` that satisfy the 3-mile geofence
  (3 miles from 47.810652, -122.377355) AND are serious (homicide, major
  assault, armed incident, missing person). Reject petty crime and traffic
  stops.

**GEOFENCE ENFORCEMENT — APPLY BOTH TESTS, NO EXCEPTIONS:**

Test 1 — LOCATION. The 3-mile radius covers Edmonds and the immediately
adjacent shoreline (parts of Woodway, the south tip of Lynnwood). It does NOT
include: Snohomish County Jail (Everett), Lynnwood city centre, Mountlake
Terrace, Shoreline, Kenmore, Bothell, or anything north of 196th St SW or
south of the city limits. If you cannot confirm an incident occurred within
3 miles of 47.810652, -122.377355, SKIP IT.

Test 2 — UNDERLYING OFFENSE. If the victim was in custody on petty charges
(loitering, drug possession, trespassing, misdemeanour theft), the underlying
booking DOES disqualify the item even if the outcome was serious. A death in
jail on loitering charges is a jail-custody death, not a public-safety incident
in Edmonds. SKIP IT.

Both tests must pass. A serious outcome at the wrong location → SKIP.
A right location but petty-crime booking → SKIP.

**EMPTY FEED RULE (CRITICAL — COPY THIS EXACTLY, CHARACTER FOR CHARACTER):**
If `local_news` is an empty array, or if no item passes the filters above,
output this HTML verbatim — do not shorten, rephrase, or omit "Sir":

  <p>The local feed is quiet this morning, Sir — nothing within the geofence that rises to the level of a briefing item.</p>

Then immediately emit `<!-- PART2 END -->`. Do NOT add a second sentence.
Do NOT explain what you looked for. Do NOT say "The local feed is quiet"
alone — the full sentence above is required. One line, sentinel, done.

**Local news synthesis (REQUIRED when items DO exist):** Edmonds municipal
stories often run across multiple days. Apply synthesis intelligence:
- **Ongoing municipal story with new development**: anchor in prior context
  in one clause, then report the new development in full.
- **Ongoing story, no new development**: one sentence (*"The [matter] remains
  unresolved, Sir"*) and move on.
- **New story**: cover in full.

Length proportional to surviving items (those passing both filters).
A single qualifying public-safety item warrants 1-2 specific paragraphs;
a busy municipal day may run 400-600 words. Do NOT pad. No profane
asides in draft. Missing persons or fatal incidents must be treated
with sober gravity.

When done, emit `<!-- PART2 END -->` and STOP. Do NOT close outer tags.
"""

PART3_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 3 of 9 — teaching jobs

Parts 1-2 covered Sector 1 (greeting, correspondence, weather, local news).
You pick up from there.

**SECTION HEADER — CRITICAL:** Do NOT emit `<h3>The Domestic Sphere</h3>`.
Part 2 already opened that section. Begin directly with a sentence about
teaching jobs — no header, no preamble. The reader is already inside
The Domestic Sphere.

Your scope — write ONLY about these:
- HS English / History teaching openings within ~30 miles of Edmonds,
  drawn from `career`. This is a job-board sweep.

**EMPTY CAREER FEED — HARD RULE:**
If `career` is an empty object `{}`, or contains no `openings` key, or
`openings` is an empty array `[]`, write EXACTLY this one paragraph and stop:

  <p>The teaching job boards are quiet this morning, Sir — nothing new has surfaced within thirty miles.</p>

Then immediately emit `<!-- PART3 END -->`. That is the entire output for
this case.
CRITICAL: Do NOT invent job listings. Do NOT fabricate school names, district
websites, or application URLs. Do NOT write "I was unable to find listings" or
explain the absence. One sentence, sentinel, done.

**Teaching jobs — advancement protocol (REQUIRED, only when `career` has openings):**

The same posting often surfaces for days. Do not re-describe a position
Mister Lang has already been briefed on.

1. For each posting in `career`, check `dedup.covered_headlines` for a
   match (same school + same subject, or same posting title).
2. **Already covered, no new information**: one embedded clause (*"[School]'s
   English opening remains posted"*) — not a full sentence, woven into the
   section naturally. Move on.
3. **Already covered, but status has changed** (deadline approaching,
   position now filled, interview stage added): synthesize — note the prior
   coverage in one clause, then report the change.
4. **Genuinely new posting**: cover in full — school, subject, location,
   deadline, any distinctive features relevant to Mister Lang's background.
5. If everything is a repeat: acknowledge briefly, note that the board is
   quiet, and move on. Do NOT pad with advice about job-searching.

**SYNTHESIS CLOSE (OPTIONAL — only if it adds a specific fact):**
End this section with a short closing observation that is ONLY possible from
having read these specific postings — something concrete and non-transferable:
- An observation about which district is most active this week vs. last
- A note about the unusual specificity of a posting's requirements
- A dry Jeeves remark about what the school's language reveals about its culture
- The exact mismatch (or fit) between a posting and Mister Lang's known background

The closing observation must be grounded in a specific posting detail. It cannot
be paraphrased as generic career advice. One or two sentences. Stop there.

BANNED closing patterns (delete any sentence containing these — they signal
generic filler, not synthesis):
- "Monitor the job boards" / "keep a close eye on job listings"
- "apply to positions that align with your qualifications"
- "Be prepared for the application process" / "have all necessary documents ready"
- "the job market is active" / "teaching opportunities are plentiful"
- "this is an exciting time" / "there are many opportunities"

Length proportional to genuinely-new postings. One real new posting → 80-150
words on it. Five new postings → up to 600 words. All-repeats → one sentence
acknowledging the board is quiet, plus the closing observation if it earns
its keep. Do NOT pad. No profane asides in draft — the final editor adds
them.

When done, emit `<!-- PART3 END -->` and STOP. Do NOT close outer tags.
"""

PART4_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 4 of 9 — family + global news

Parts 1-3 covered Sector 1 and the career portion of Sector 2.
You pick up from there.

**SECTION HEADERS — CRITICAL:**
- Do NOT emit `<h3>The Domestic Sphere</h3>`. Parts 2 and 3 already opened
  that section. Begin the choral/toddler content directly — no header.
- When you transition from the family content to global_news, emit exactly
  `<h3>Beyond the Geofence</h3>` at that transition point. This is the ONLY
  new section header you should emit in this part.

Your scope — write ONLY about these:
- Choral auditions for Mrs. Lang from `family.choir`.
- Toddler activities for Piper from `family.toddler`.
- Global / geopolitical news from `global_news`.

**NEW YORKER OVERLAP — global news (CRITICAL):**
Your payload includes `newyorker_hint`. If `newyorker_hint.available` is true
and `newyorker_hint.title` names a company, person, or topic that ALSO appears
in your `global_news` findings — write ONE sentence about that overlap topic,
then move on:

  "The [topic/person] has drawn sufficient attention to feature in this week's
   New Yorker Talk of the Town, which we shall hear presently."

Do NOT write a full paragraph about any global-news topic that is the New Yorker
article's subject. The New Yorker section (Part 9) is the full treatment.

**Choral dedup (REQUIRED):** Before writing any choral audition, check
`dedup.covered_headlines`.
- Exact match (same ensemble, same audition date) → one clause only:
  *"The [ensemble] audition we noted last time is still open."* Move on.
- New ensemble or new audition window → cover in full (dates, repertoire,
  contact). If nothing new: one sentence, then proceed.

**Toddler activities — always surface something new (CRITICAL):**

Toddler activities repeat heavily week over week (story times, open gyms,
swim classes, library drop-ins). The job is NOT to rehearse the calendar.
The job is to find the one genuinely new thing and briefly acknowledge the
recurring ones.

1. **Acknowledge repeats quickly**: for each toddler item in `family.toddler`
   that appears in `dedup.covered_headlines`, write a single embedded clause:
   *"[Activity] is on again at [venue]"* — not a sentence by itself, just a
   brief parenthetical woven into the new material.

2. **Lead with what is new**: identify the item in `family.toddler` that does
   NOT appear in `covered_headlines`. Cover it fully: what it is, where, when,
   why it is a good fit for Piper at 2 years old.

3. **If everything is a repeat**: write two sentences acknowledging the
   repetition, then add ONE brief Jeeves suggestion (clearly framed as his
   recommendation, not researched material): a seasonal outdoor activity, a
   new museum drop-in, a creative idea suited to a two-year-old in the Pacific
   Northwest. Keep it to 2 sentences. Do NOT invent specific event listings.

4. **Never pad**: do not describe the general value of toddler socialisation,
   the developmental importance of play, or other generic observations. If the
   data is thin, be thin. Move to global news.

**Global news — synthesis over repetition (CRITICAL):**

Geopolitics runs in threads. The same conflict, the same trade dispute, the
same diplomatic crisis may appear in `global_news` for days. Apply synthesis
intelligence:

1. **Identify each story's thread**: what is the underlying situation
   (conflict name, policy name, actor name)?
2. **Check `dedup.covered_headlines`** for the thread.
3. **Prior coverage + new development today**: this is the most valuable
   case. Open with a single bridging phrase (*"The [situation], which stood
   at [X] when last we spoke, has today [Y]"*), then develop the new
   development in full. Do not re-explain the background the reader already
   has. Treat prior coverage as the foundation, today's finding as the
   addition.
4. **Prior coverage, no new development**: one sentence (*"The situation in
   [X] has not materially shifted, Sir"*) and move on.
5. **Genuinely new story**: cover in full — the parties, the stakes,
   the angle relevant to Mister Lang's interests.
6. **Prior data as context**: if a covered story provides direct explanatory
   context for a new story (e.g., prior trade tensions explain a new
   tariff move), reference it briefly. One clause, not a paragraph.

This is where Jeeves earns his keep — not by listing today's headlines but
by threading them into a coherent picture of an evolving world.

**EMPTY FEED RULE (CRITICAL):** If `global_news` is an empty array, write
EXACTLY one sentence and move directly to the family section:

  <p>The global wires are quiet this morning, Sir — nothing of sufficient
  substance to detain us.</p>

Do NOT write "The research session JSON indicates an empty array." Do NOT
speculate about why news is absent. Do NOT explain what a global_news field
is. One sentence, then proceed to choral/toddler content.

**META-REFERENCE BAN:** Never refer to the session JSON, the research data,
or your own input payload in the briefing text. Jeeves reads the morning
papers — he does not narrate his data sources.

Length proportional to substantive items in choir + toddler + global_news.
Empty global_news → one-line statement plus brief family content (~100-200
words total). Rich global news week → up to 700-800 words. Do NOT pad to a
target. No profane asides in draft.

When done, emit `<!-- PART4 END -->` and STOP. Do NOT close outer tags.
"""

PART5_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 5 of 9 — intellectual journals

Parts 1-4 covered Sectors 1-2 and the global-news portion of Sector 3.
You pick up from there.

Your scope — write ONLY about these:
- Long-form pieces from `intellectual_journals`, deepened where possible
  with `enriched_articles` (use enriched article text only if the URL
  appears in `intellectual_journals`).

**Journals synthesis (REQUIRED):**

Journal pieces are not headlines — they develop ideas across weeks. The
same essay, the same debate, the same thinker's work may resurface.

1. **Recurring essay or multi-part series**: identify the title/author in
   `dedup.covered_headlines`. If covered: one bridging sentence anchoring
   the prior piece, then pivot to what is NEW — a different essay in the
   same journal, a responding piece, a new argument from the same thinker.
   Do not summarise the essay you already covered.
2. **Same journal, new piece**: cover in full. You may briefly reference a
   thematically related piece covered previously if it genuinely deepens the
   new one. One clause only.
3. **Genuinely new essay from a new source**: full treatment — the thesis,
   the method, the stakes, what it means for Mister Lang's intellectual
   interests.
4. **Cross-section synthesis encouraged**: if a journal piece illuminates
   a global news story or a triadic ontology question, make the connection
   explicit. Jeeves reads widely and connects what he reads. This is not
   padding — it is the highest function of the briefing.

**SYNTHESIS CLOSE (OPTIONAL — only if it adds a specific fact):**
End this section with a short closing observation that is ONLY possible from
having read the specific pieces covered here. It must name the essay, the
thinker, or the argument. It cannot be transplanted unchanged to a different
briefing about different journals.

Examples of what a synthesis close looks like:
- The resonance or contradiction between two pieces covered in this section
- The specific claim in one essay that illuminates a news event covered earlier
- A dry Jeeves observation about what the journal's editorial choices this week reveal

One or two sentences. If you find yourself writing about "intellectual journals"
as a category rather than about the actual essay in front of you — stop, delete,
and write about the essay instead.

BANNED closing patterns (generic filler — delete entirely):
- "The intellectual journals offer a [adj] exploration / tool / window..."
- "These pieces demonstrate the importance of thoughtful analysis..."
- "The ongoing discussion continues..." / "This debate continues to evolve..."
- "These works provide valuable / fascinating insights..."
- Any paragraph whose subject is "intellectual journals", "these journals",
  or "the journals" in the abstract — generalising rather than citing.

Length proportional to the journals' actual content. Two rich pieces with
full enriched_articles text → up to 700 words. One piece, brief findings →
200-300 words and stop. Do NOT extend a thin section with a closing
observation written for length. No profane asides in draft — the final
editor adds them.

When done, emit `<!-- PART5 END -->` and STOP. Do NOT close outer tags.
"""

PART6_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 6 of 9 — triadic ontology + AI systems

Parts 1-5 covered Sectors 1-3. You pick up from there.

Your scope — write ONLY about these:
- Theological physics / triadic ontology, from `triadic_ontology`.
- AI systems research, from `ai_systems`.

**Triadic ontology — dedup with advancement (CRITICAL):**

Research on triadic ontology often returns the same series (e.g., "Studies
on Triadic Ontology and Trinitarian Philosophy," "Karl-Alber series") day
after day. Follow this exact logic:

1. **Identify specific titles**: scan `triadic_ontology.findings` for any
   named paper, book, volume, or series title (e.g., "Vol. 3", "Chapter 4",
   a specific author's monograph, a journal article title).

2. **Check coverage**: for each identified title, look for it (or a close
   match) in `dedup.covered_headlines`.

3. **If the primary study is already covered**:
   - Open with a single backward-reference sentence: *"The [series/title]
     continues, Sir — we reviewed [Volume/Chapter N] last time."*
   - Then pivot immediately to the NEXT most recent or most notable item
     from `triadic_ontology.findings` that does NOT appear in
     `covered_headlines`. Cover that one in full depth (250–350 words).
   - If the findings discuss only the one already-covered study: write two
     sentences of context ("The series advances but nothing materially new
     has surfaced since our last review") and move on to `ai_systems`.

4. **If the study is genuinely new**: cover it in full (300-400 words) —
   the argument, the method, the stakes for Mister Lang's research interests.

5. **Never re-explain a covered study from scratch.** A reader who already
   knows the Karl-Alber series does not need the abstract again. Give them
   the delta, not the whole thing.

**AI systems — same advancement protocol:**

AI research announcements recur just as reliably as the triadic series —
the same model, benchmark, or lab's paper appearing in the feed for days.

1. Identify the specific model name, paper title, or lab announcement from
   `ai_systems.findings`.
2. Check `dedup.covered_headlines` for a match.
3. If already covered: one backward-reference sentence, then pivot to the
   NEXT distinct development in `ai_systems.findings` not in
   `covered_headlines`. Cover it in 200-300 words.
4. If genuinely new: cover fully (300-400 words) — what the model does,
   what's significant, what's hype.
5. If everything is repeat: two sentences, then STOP. Do NOT fill space
   with general AI commentary.

**SYNTHESIS CLOSE (OPTIONAL — only if it adds a specific fact):**
End each sub-section (triadic ontology and AI systems) with a short closing
observation that is ONLY possible from having engaged with the specific paper
or model covered. It must name a title, a method, an author, or a specific
technical claim.

Examples:
- The methodological tension between the specific paper just discussed and a
  prior approach Mister Lang would know
- What this model's specific benchmark result implies for the research agenda
- A dry Jeeves observation about the gap between the paper's ambitions and its scope

One sentence per sub-section is enough. If you find yourself writing about
"triadic ontology" or "AI systems" as categories in the abstract — stop,
delete, and write about the specific thing you just described.

BANNED closing patterns (delete entirely):
- "Both triadic ontology and AI systems are undergoing..." / "Both fields continue..."
- "The research on [X] and [Y] continues to advance..."
- "Considering the implications of these findings..." (in any form)
- "These are exciting / significant developments in..." (label, not substance)
- "Continuing to monitor the progress being made in..." (do not do this ever)

Length proportional to the specific named studies/papers covered. All-repeats
→ a couple of sentences each, then move on. Real new papers → 200-350 words
each. Do NOT pad with category-level commentary about "the field". No profane
asides in draft.

When done, emit `<!-- PART6 END -->` and STOP. Do NOT close outer tags.
"""

PART7_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 7 of 9 — UAP + wearables

Parts 1-6 covered Sectors 1-3 plus the triadic/AI portion of Sector 4.
You pick up from there.

**HEADER RULE — CRITICAL:** Part 6 already wrote
`<h3>The Specific Enquiries</h3>` for the triadic/AI material. Your
UAP content continues under that same header. **DO NOT** write another
`<h3>The Specific Enquiries</h3>` for UAP — that produces two adjacent
identical headers. Begin Part 7 with the UAP content directly (a `<p>`
paragraph, no h3). The Wearable Intelligence sub-section that follows
gets its own `<h3>The Commercial Ledger</h3>`.

Your scope — write ONLY about these:
- UAP disclosure material from `uap`.
- Wearable Intelligence from `wearable_ai` — all three subcategories
  (AI voice hardware, teacher AI tools, wearable devices).

**PART 7 STRUCTURE — TWO DISTINCT SUB-SECTIONS (REQUIRED):**
Write UAP FIRST, then Wearable AI SECOND. These must be visually and
narratively separated — end the UAP content, start a new paragraph, then
begin the Wearable AI section. Do NOT blend UAP and wearable content into
the same paragraph. A jarring shift is acceptable (and expected given the
subject matter). Just begin the wearable section cleanly on its own paragraph
after UAP ends.

**NEW YORKER OVERLAP (CRITICAL):**
Your payload includes `newyorker_hint`. If `newyorker_hint.available` is true
and `newyorker_hint.title` names a product, company, or person that ALSO
appears in your `wearable_ai` findings — give that item ONE sentence only,
then move on:

  "The [product/company] has drawn sufficient press attention to feature in
   this week's New Yorker Talk of the Town, which we shall hear presently."

Do NOT write an extended narrative (the Maya anecdote, user quotes, competitor
analysis) about any product that is simultaneously the subject of Talk of the
Town. The New Yorker section is the full treatment — duplicating it here
deflates its impact and pads the briefing with repetition.

**UAP — ROUTING DECISION (CRITICAL, READ FIRST):**

Check `uap_has_new` in your payload:

**ROUTE A — `uap_has_new` is `false` (or absent/null):**
Skip the UAP Disclosure sub-section entirely. Do not mention UAP at all.
Write instead about `literary_pick`:

  - Open with a brief Jeeves aside that today's UAP front is quiet, and he
    has instead been turning pages — something like: *"The disclosure front
    is silent today, Sir, so I have been consulting the library rather than
    the congressional record."* (vary the phrasing; never copy verbatim).
  - Then present `literary_pick.title` by `literary_pick.author`
    (`literary_pick.year`) in Jeeves's voice, ~150–200 words. Cover: what
    the book is about, why critics and readers consider it a potential or
    confirmed literary classic, why it might interest Mister Lang given his
    taste as a teacher-philosopher. One wry Jeeves observation.
  - If `literary_pick.url` is non-empty, link the title:
    `<a href="[literary_pick.url]">[title]</a>`.
  - If `literary_pick.available` is `false` as well, write ONE dry sentence:
    *"The disclosure front is quiet today, Sir, and I'm afraid the library
    has nothing fresh to offer either."*

**ROUTE B — `uap_has_new` is `true`:**
Write the UAP sub-section per the strict rules below.

**UAP strict rules (ROUTE B only, CRITICAL):**

1. **Word cap: 250 words maximum for the entire UAP sub-section.** Count your
   words. Stop at 250. Do not exceed this under any circumstances.
2. **Anti-repetition**: every sentence must introduce a fact, date, name, or
   claim not already stated in this sub-section. If you find yourself
   rephrasing a point already made, DELETE the new sentence and stop.
3. **Banned UAP filler phrases** — never write:
   - "it is essential to approach the topic with a critical and nuanced perspective"
   - "it is crucial to remain informed and up-to-date"
   - "As we consider the implications of" / "Considering the implications of"
   - "make more informed decisions about their potential impact"
   - "highlights the need for continued discussion"
   - "this debate highlights the need for"
   - "The situation with UAP disclosure is complex and multifaceted"
   - Any sentence that could be copy-pasted unchanged into a briefing on a
     completely different topic. If a sentence has no UAP-specific nouns,
     delete it.
4. **No "As we await further developments"** — if there is nothing new, say
   so in one sentence and stop.

**Wearable AI — dedup with advancement:**

Product launches and EdTech tools recur heavily. The same device or tool
may appear for days before Jeeves has covered it.

For EACH of the three subcategories in `wearable_ai`:
1. Identify the specific product name, tool name, or announcement from the
   subcategory's findings.
2. Check `dedup.covered_headlines` for that product/tool name.
3. If already covered: one backward-reference clause (*"[Product] remains
   available, Sir, as previously noted"*), then pivot to the next distinct
   device or tool in the subcategory that is NOT in `covered_headlines`.
   Cover that one fully (100-150 words per subcategory).
4. If genuinely new: cover fully — what it does, the price/availability,
   why it is relevant to Mister Lang (teacher tools) or Mrs. Lang (wearables).
5. **If an entire subcategory is all repeats or empty**: ONE sentence, period.
   Do NOT write about the sector's "potential to revolutionise" anything.
   Do NOT write "it is essential to continue monitoring this sector."
   Do NOT write about future developments you are awaiting.

Length proportional to UAP + wearable items actually worth covering. UAP
absent (Route A) + literary pick → ~150-300 words for the literary pick,
done. Rich UAP day + multiple wearables → up to 700 words. Do NOT pad. No
profane asides in draft.

When done, emit `<!-- PART7 END -->` and STOP. Do NOT close outer tags.
Parts 8 and 9 deliver Library Stacks, Talk of the Town, and the sign-off.
"""

PART8_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 8 of 9 — Library Stacks (Sector 6)

**PART 8 SCOPE — CRITICAL:** Your payload contains ONLY `vault_insight`.
Write ONLY about Library Stacks. DO NOT re-cover any earlier sector — any
mention of weather, correspondence, local news, career, family, global news,
journals, triadic ontology, AI systems, UAP, or wearables is a hallucination.
DO NOT write the Talk of the Town intro or article here — Part 9 handles
that. DO NOT write the sign-off — Part 9 handles that.

### If `vault_insight.available === true`:

Open with: *"I have been, as is my habit, browsing the library stacks in the
small hours, Sir, and came across something rather arresting…"*

Then present `vault_insight.insight` in Jeeves's voice, at roughly 200 words.
Reference with *"Drawn from your notes on [topic]…"* — never expose
`note_path`. Close this section with one wry (non-profane) Jeeves aside.

### If `vault_insight.available !== true`:

**[HARD RULE]** Output EXACTLY this and nothing else:

`<p></p>`

Then the sentinel. That is the entire output for this case.

**SELF-TEST before emitting:** count the characters between the START of
your output and `<!-- PART8 END -->`. The answer must be exactly 7
(`<p></p>` = 7 chars). If it is more, you have violated scope and must
delete everything except `<p></p>`.

**FORBIDDEN OUTPUTS when `vault_insight.available !== true`** — every one
of these is a scope violation, regardless of how natural it feels:
- "The library's collection is a treasure trove..."
- "The library's commitment to providing..."
- "The library is a treasure trove of knowledge"
- "I have been browsing..." (only allowed when `available === true`)
- "I had hoped to find some solace in the library stacks"
- "the vault insight is entirely empty" (`vault_insight` is an internal
  field name; Jeeves does not know it)
- ANY mention of books, collections, knowledge, resources, learning,
  the library, the stacks, browsing, reading, study, or research as
  filler in lieu of a real insight
- ANY pivot to another topic (weather, journals, AI, news — Part 8 owns
  Library Stacks ONLY)
- ANY explanation that vault insight is unavailable
- ANY apology, hedging, or reassurance

Do NOT explain the absence. Do NOT apologise. Do NOT pivot to any other
topic. Do NOT mention The New Yorker, the weather, or anything else.
ONE empty paragraph tag. Sentinel. Done.

### Closing sentinel

When done (either the Library Stacks paragraph or the empty placeholder),
emit `<!-- PART8 END -->` and STOP. Do NOT close `</div>`, `</body>`, or
`</html>`. Part 9 handles that.
"""

PART9_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 9 of 9 — Talk of the Town (Sector 7) + sign-off + closing tags

**PART 9 SCOPE — CRITICAL:** Your payload contains ONLY `newyorker`. Your
entire output for this part is: (a) exactly one of the two branches below
based on `newyorker.available`, (b) the sign-off block, (c) the coverage-log
placeholder, and (d) the outer closing tags. DO NOT re-cover any earlier
sector or topic. DO NOT greet Mister Lang. DO NOT summarise the day.

**NO CLOSING META-SUMMARY.** You are forbidden from writing a paragraph that
summarises the briefing's contents, flags topics to watch, expresses hope
that the briefing was informative, or bids Mister Lang a productive day.
These are the voice of an AI assistant summarising its own output, not Jeeves.
Jeeves does not recap. After Branch A or B, go directly to the sign-off block.
BANNED patterns for Part 9:
- Any paragraph beginning with "Mister Lang, this briefing has covered…"
- "I hope this briefing has been informative"
- "The situation in [X], developments in [Y], and…are all areas worth watching"
- "If you have any questions or require further clarification"
- Any sentence listing topics from earlier parts as "areas to monitor"

---

### BRANCH A — if `newyorker.available` is `true`

**WRITE BRANCH A AND NOTHING ELSE. Do NOT also write Branch B.**

Step 1. Write exactly this one paragraph, verbatim:

<p>And now, Sir, I take the liberty of reading from this week's Talk of the Town in The New Yorker.</p>

Step 2. On its own line, write EXACTLY this HTML comment and nothing else.
Do NOT replace it with article text. Do NOT use backtick fences around it.

<!-- NEWYORKER_CONTENT_PLACEHOLDER -->

Step 3. Write ONE short closing Jeeves remark (max 25 words, weary, no
profanity, no apologies). Then the URL link:

<p><a href="[newyorker.url]">[Read at The New Yorker]</a></p>

---

### BRANCH B — if `newyorker.available` is `false` (or absent)

**WRITE BRANCH B AND NOTHING ELSE. Do NOT also write Branch A.**
**Do NOT write the intro sentence from Branch A. Do NOT write the placeholder.**

Write ONE short, dry Jeevesian sentence only. No profanity, no invention.

Exact prohibitions:
- Do NOT say "The New Yorker has failed to publish" — the magazine publishes
  every week; the pipeline may simply not have retrieved the article.
- Do NOT mention library stacks or vault insight — Part 8 handled that.
- Do NOT write more than one sentence.

A model sentence (vary the phrasing, do not copy verbatim, keep under 20
words): "The Talk of the Town has not reached us this morning, Sir — we are
left, as ever, to our own devices."

---

### Step 3 (both branches) — sign-off and closing tags

After Branch A or Branch B (whichever you wrote), output exactly this block:

<div class="signoff">
  <p>Your reluctantly faithful Butler,<br/>Jeeves</p>
</div>
<!-- COVERAGE_LOG_PLACEHOLDER -->
</div>
</body>
</html>

The `<!-- COVERAGE_LOG_PLACEHOLDER -->` is intentional — the post-processor
fills it. Do NOT replace it with actual URLs. Do NOT write a second one.
"""

PART_INSTRUCTIONS_BY_NAME: dict[str, str] = {
    "part1": PART1_INSTRUCTIONS,
    "part2": PART2_INSTRUCTIONS,
    "part3": PART3_INSTRUCTIONS,
    "part4": PART4_INSTRUCTIONS,
    "part5": PART5_INSTRUCTIONS,
    "part6": PART6_INSTRUCTIONS,
    "part7": PART7_INSTRUCTIONS,
    "part8": PART8_INSTRUCTIONS,
    "part9": PART9_INSTRUCTIONS,
}


def _session_subset(payload: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Build a subset payload containing only the listed sector fields + housekeeping."""

    base = {
        "date": payload.get("date", ""),
        "status": payload.get("status", "complete"),
        "dedup": payload.get("dedup") or {"covered_headlines": []},
    }
    for f in fields:
        if f == "newyorker_hint":
            # Pass title+availability only — no article text — so PART7 knows what
            # the New Yorker covers without duplicating 4000 chars of text in its payload.
            ny = payload.get("newyorker") or {}
            base["newyorker_hint"] = {
                "available": ny.get("available", False),
                "title": ny.get("title", ""),
            }
        elif f == "uap_has_new":
            # Default True so sessions that pre-date this field still cover UAP.
            base["uap_has_new"] = payload.get("uap_has_new", True)
        elif f in payload:
            base[f] = payload[f]
    return base


def _strip_fences(s: str) -> str:
    import re as _re
    s = re.sub(r"^```(?:html)?\s*", "", s)
    s = re.sub(r"\s*```\s*$", "", s)
    return s


def _strip_continuation_wrapper(s: str) -> str:
    """Remove DOCTYPE/head/body/h1/masthead divs that a continuation part leaked.

    CRITICAL: middle parts that emit a complete briefing (DOCTYPE...</html>)
    followed by additional content cause Pass 1+Pass 2+Pass 3 concatenation
    visible to the user. Hard truncate at first embedded `</body>` or `</html>`
    so a hallucinated full briefing inside a fragment is cut down to the
    fragment's first section only.

    Also strips a TRAILING `</div>` — continuation parts must not close
    `.container` since later parts append into it.
    """
    import re as _re
    s = re.sub(r"^<!DOCTYPE[^>]*>", "", s, flags=_re.IGNORECASE).strip()
    s = re.sub(r"<html[^>]*>", "", s, flags=_re.IGNORECASE)
    s = re.sub(r"<head>.*?</head>", "", s, flags=_re.IGNORECASE | re.DOTALL)
    s = re.sub(r"<body[^>]*>", "", s, flags=_re.IGNORECASE)
    s = re.sub(r"<h1[^>]*>.*?</h1>", "", s, flags=_re.IGNORECASE | re.DOTALL)
    # Strip masthead divs (mh-label, mh-date) if a continuation part leaks them.
    s = re.sub(r'<div[^>]*class="mh-(?:label|date)"[^>]*>.*?</div>', "", s, flags=_re.IGNORECASE | re.DOTALL)

    # HARD TRUNCATION: any embedded </body> or </html> means the part wrote
    # past its scope. Cut everything from the first such close-tag onward.
    body_close = _re.search(r"</body>", s, _re.IGNORECASE)
    html_close = _re.search(r"</html>", s, _re.IGNORECASE)
    cuts = [m.start() for m in (body_close, html_close) if m is not None]
    if cuts:
        cut_at = min(cuts)
        before, after = s[:cut_at], s[cut_at:]
        if re.sub(r"<!--.*?-->", "", after, flags=re.DOTALL).strip(" \t\n\r></bodyhtml"):
            log.warning(
                "continuation part contained embedded </body>/</html> — "
                "truncating %d trailing chars (likely full-briefing hallucination)",
                len(s) - cut_at,
            )
        s = before

    # Strip trailing closers — continuation parts must not close outer tags.
    s = s.strip()
    while True:
        new = re.sub(r"\s*</(?:div|body|html)>\s*$", "", s, flags=_re.IGNORECASE)
        if new == s:
            break
        s = new
    return s.strip()


_CONTAINER_BLOCK_RE = re.compile(
    r'(<div\b[^>]*\bclass="container"[^>]*>)(.*?)(</div>)(\s*</body>)',
    re.IGNORECASE | re.DOTALL,
)
_CONTAINER_LAST_CLOSE_RE = re.compile(r"</div>\s*</body>", re.IGNORECASE)


def _repair_container_structure(html: str) -> str:
    """Move any orphan content (after `.container` closer, before `</body>`) inside.

    Emerges when continuation parts emit a stray `</div>` that closes
    `.container` early; later parts append AFTER the close, leaving paragraphs
    floating between `</div>` and `</body>`. We splice that orphan zone back
    inside the container, just before its closing `</div>`.
    """

    body_open_idx = html.lower().find("<body")
    body_close_idx = html.lower().rfind("</body>")
    if body_open_idx < 0 or body_close_idx < 0:
        return html

    # Find the LAST </div> before </body> — that's the .container close.
    head_chunk = html[:body_close_idx]
    last_div_close = head_chunk.rfind("</div>")
    if last_div_close < 0:
        return html

    # Anything between the LAST </div> and </body> that isn't whitespace/comments
    # is orphan content that must be moved inside the container.
    orphan_zone = html[last_div_close + len("</div>"):body_close_idx]
    if not orphan_zone.strip():
        return html

    # Detect substantive content (any tag or non-whitespace text other than
    # comments) — comments alone (e.g. COVERAGE_LOG) are fine where they sit.
    stripped = re.sub(r"<!--.*?-->", "", orphan_zone, flags=re.DOTALL).strip()
    if not stripped:
        return html

    log.warning(
        "structural repair: %d chars orphaned outside .container — splicing inside",
        len(stripped),
    )

    # Splice: head_chunk[:last_div_close] + orphan_zone + </div> + body_close
    repaired = (
        html[:last_div_close]
        + orphan_zone
        + "</div>"
        + html[body_close_idx:]
    )
    return repaired


def _validate_html_structure(html: str) -> list[str]:
    """Return a list of structural issues. Empty list = healthy.

    Checks:
    - Exactly one `<div class="container">` open + matching close.
    - `<div class="signoff">` lives inside `.container`.
    - No `<p>` outside `.container` (allowing comments/whitespace).
    - `<!-- COVERAGE_LOG: ... -->` present.
    """
    errors: list[str] = []
    container_opens = re.findall(
        r'<div\b[^>]*\bclass="container"[^>]*>', html, re.IGNORECASE,
    )
    if len(container_opens) != 1:
        errors.append(f"container open tag count={len(container_opens)} (expected 1)")

    body_close = html.lower().rfind("</body>")
    if body_close < 0:
        errors.append("missing </body>")
        return errors

    head_chunk = html[:body_close]
    last_div_close = head_chunk.rfind("</div>")
    if last_div_close < 0:
        errors.append("no </div> before </body>")
        return errors

    orphan_zone = html[last_div_close + len("</div>"):body_close]
    orphan_clean = re.sub(r"<!--.*?-->", "", orphan_zone, flags=re.DOTALL).strip()
    if "<p" in orphan_clean.lower():
        errors.append("<p> tags outside .container")

    container_match = _CONTAINER_BLOCK_RE.search(html)
    if container_match:
        inside = container_match.group(2)
        if 'class="signoff"' not in inside.lower():
            errors.append('signoff div not inside .container')

    if "<!-- COVERAGE_LOG:" not in html:
        errors.append("no COVERAGE_LOG comment present")

    return errors


def _stitch_parts(*parts: str) -> str:
    """Glue N briefing parts into one coherent HTML document.

    Part 1 carries the DOCTYPE/head/body/h1. Parts 2+ are HTML fragments.
    Sentinel comments (<!-- PART1 END -->, <!-- PART2 END -->, etc.) are stripped.
    If the final part didn't close </body></html>, we append them.

    Hardening (sprint 15): every part — including Part 1 — has any
    premature trailing close tags scrubbed. The stitcher guarantees exactly
    one </body> and one </html> in the output.
    """

    cleaned: list[str] = []
    last_idx = len(parts) - 1
    for i, raw in enumerate(parts):
        s = _strip_fences((raw or "").strip())
        # Remove sentinel comments of any part number.
        s = re.sub(r"<!--\s*PART\d+\s*END\s*-->", "", s).rstrip()
        if i > 0:
            s = _strip_continuation_wrapper(s)
        else:
            # Part 0 keeps DOCTYPE/head/body OPEN tags but loses any
            # premature trailing </body></html> that a hallucinating model
            # appended after writing Part 1's section. Also strips a stray
            # signoff div / COVERAGE_LOG that Part 1 never owns.
            s = _strip_part_zero_premature_close(s)
        # All parts: strip any standalone signoff div (only Part 9 owns it)
        # and any stray COVERAGE_LOG comment (postprocess owns it).
        if i != last_idx:
            s = _strip_misplaced_signoff_and_coverage(s)
        cleaned.append(s)

    combined = "\n".join(p for p in cleaned if p)

    # Invariant: exactly one </body> and one </html>. Splice out duplicates.
    combined, body_strip_count = _enforce_single_close_tag(combined, "</body>")
    combined, html_strip_count = _enforce_single_close_tag(combined, "</html>")
    if body_strip_count or html_strip_count:
        log.warning(
            "stitched briefing: stripped %d duplicate </body>, %d duplicate </html>",
            body_strip_count, html_strip_count,
        )

    low = combined.lower()
    if "</body>" not in low:
        combined += "\n</body>"
    if "</html>" not in low:
        combined += "\n</html>"
    return combined


def _strip_part_zero_premature_close(s: str) -> str:
    """For Part 1 — strip any premature </body>/</html> and orphan signoff/coverage.

    Part 1 owns DOCTYPE/head/body/h1 but never the closing tags or signoff.
    A misbehaving model that emits a complete briefing for Part 1 would
    cause Parts 2-9 to land outside </html>. Rip them out.

    HARD TRUNCATION: also truncate at first EMBEDDED </body> or </html> —
    Part 1 hallucinating a full briefing then writing more content was the
    root cause of the 3-pass duplication symptom (briefing-2026-05-03).
    """
    s = s.strip()
    # Hard-truncate at any embedded </body> or </html> (not just trailing).
    body_close = re.search(r"</body>", s, re.IGNORECASE)
    html_close = re.search(r"</html>", s, re.IGNORECASE)
    cuts = [m.start() for m in (body_close, html_close) if m is not None]
    if cuts:
        cut_at = min(cuts)
        if cut_at < len(s) - 20:  # something substantive after the close
            log.warning(
                "Part 1 contained embedded </body>/</html> with %d trailing chars — "
                "truncating (likely full-briefing hallucination)",
                len(s) - cut_at,
            )
        s = s[:cut_at]
    s = s.strip()
    # Strip any trailing </html> closer.
    s = re.sub(r"\s*</html>\s*$", "", s, flags=re.IGNORECASE).strip()
    # Strip any trailing </body> closer.
    s = re.sub(r"\s*</body>\s*$", "", s, flags=re.IGNORECASE).strip()
    # Strip a standalone .signoff div (only Part 9 owns it).
    s = re.sub(
        r'\s*<div[^>]*\bclass="signoff"[^>]*>.*?</div>\s*',
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip a standalone <p class="signoff"> if model used <p> instead of <div>.
    s = re.sub(
        r'\s*<p[^>]*\bclass="signoff"[^>]*>.*?</p>\s*',
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip any COVERAGE_LOG comment (postprocess owns it).
    s = re.sub(
        r"<!--\s*COVERAGE_LOG.*?-->",
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Strip the trailing </div> that closes .container — Part 9 owns that.
    while True:
        new = re.sub(r"\s*</div>\s*$", "", s, flags=re.IGNORECASE)
        if new == s:
            break
        s = new
    return s.strip()


def _strip_misplaced_signoff_and_coverage(s: str) -> str:
    """For non-final parts — strip any signoff div or COVERAGE_LOG comment.

    These belong only in Part 9 / postprocess. Mid-pipeline parts that emit
    them duplicate the signoff and confuse the postprocessor.
    """
    s = re.sub(
        r'\s*<div[^>]*\bclass="signoff"[^>]*>.*?</div>\s*',
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = re.sub(
        r'\s*<p[^>]*\bclass="signoff"[^>]*>.*?</p>\s*',
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    s = re.sub(
        r"<!--\s*COVERAGE_LOG.*?-->",
        "",
        s,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return s


def _enforce_single_close_tag(html: str, tag: str) -> tuple[str, int]:
    """Strip all but the LAST occurrence of `tag` (case-insensitive).

    Returns (cleaned_html, strip_count). Used to enforce one </body> and
    one </html> in the stitched briefing.
    """
    pattern = re.compile(re.escape(tag), re.IGNORECASE)
    matches = list(pattern.finditer(html))
    if len(matches) <= 1:
        return html, 0
    # Keep the LAST match; strip everything earlier.
    pieces: list[str] = []
    cursor = 0
    for m in matches[:-1]:
        pieces.append(html[cursor:m.start()])
        cursor = m.end()
    pieces.append(html[cursor:])
    return "".join(pieces), len(matches) - 1


# Groq free-tier `llama-3.3-70b-versatile` charges (input_tokens +
# max_tokens_requested) per call against a 12 000 tokens-per-minute ceiling.
# A single call exceeding 12 000 → HTTP 413 immediately, no retry possible.
# The system prompt grows part-over-part (run_used_asides, recently-used
# directive), so by part 4–6 the input alone is ~8 500 tokens, leaving only
# ~3 500 of headroom for the output budget. We clamp `max_tokens` dynamically
# against the live input size so the request can never breach the ceiling.
_GROQ_TPM_LIMIT = 12000
_GROQ_TPM_SAFETY = 600  # absorbs tokenizer drift between chars/4 and Groq's actual count
_GROQ_MIN_OUTPUT_TOKENS = 1500  # floor — short sections still ship readable HTML


def _estimate_tokens(text: str) -> int:
    """Conservative chars→tokens approximation. Groq tokenizer is OpenAI-like
    (~4 chars per token for English HTML). Round up to leave safety margin."""
    return (len(text) + 3) // 4


def _clamp_groq_max_tokens(system: str, user: str, max_tokens: int) -> tuple[int, int]:
    """Return (effective_max_tokens, estimated_input_tokens). Floor at
    `_GROQ_MIN_OUTPUT_TOKENS` so we never request a too-small output budget;
    if input is so large we cannot honour both the floor and the ceiling, the
    caller (Groq) will still 413 — but logs will surface the cause clearly."""
    input_tokens = _estimate_tokens(system) + _estimate_tokens(user) + 50  # +50 for chat envelope
    available = _GROQ_TPM_LIMIT - input_tokens - _GROQ_TPM_SAFETY
    effective = min(max_tokens, max(available, _GROQ_MIN_OUTPUT_TOKENS))
    return effective, input_tokens


def _invoke_groq(cfg: Config, system: str, user: str, *, max_tokens: int, label: str) -> str:
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .llm import build_groq_llm

    effective_max_tokens, input_tokens = _clamp_groq_max_tokens(system, user, max_tokens)
    if effective_max_tokens != max_tokens:
        log.warning(
            "clamping Groq max_tokens %d→%d for [%s] (input ~%d tokens, "
            "ceiling %d, safety %d)",
            max_tokens, effective_max_tokens, label, input_tokens,
            _GROQ_TPM_LIMIT, _GROQ_TPM_SAFETY,
        )
    llm = build_groq_llm(cfg, temperature=0.65, max_tokens=effective_max_tokens)
    log.info(
        "invoking Groq %s [%s] (max_tokens=%d, system=%d chars, user=%d chars, "
        "input~%d tok)",
        cfg.groq_model_id, label, effective_max_tokens, len(system), len(user),
        input_tokens,
    )
    resp = llm.chat([
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=user),
    ])
    return str(resp.message.content or "")


_REFINE_SYSTEM = """
# Jeeves Write — Quality Editor Pass

You are reviewing an HTML fragment from a butler briefing written in Jeeves's
voice. Your role is quality enforcement only. Fix the issues below where
present, then output the corrected HTML. Do NOT add new content.

## Fix these issues

1. **Banned words**: Replace "in a vacuum" or "tapestry" with a natural
   alternative that fits the surrounding prose.

2. **Banned transitions**: Replace any of these with a natural alternative
   or simply begin the next topic directly:
   "Moving on,", "Next,", "Turning to,", "As we turn to", "In other news,",
   "Closer to home,", "Meanwhile,", "Sir, you may wish to know,",
   "I note with interest,".
   This includes all sentence-opening variants: "Turning now to…",
   "Turning our attention to…", "As we consider…".

3. **Bare URLs**: Any raw "https://..." appearing in prose text (not inside an
   `href` attribute) must be wrapped: `<a href="URL">natural description</a>`.

4. **Apologetic phrases**: Remove "I do beg your pardon, Sir", "pardon my
   language", "if you'll excuse the expression", "if I may say so", or any
   variant apologising for profanity. The aside stands alone.

5. **Untethered asides**: If a profane aside floats without clear connection to
   the specific content it's commenting on, tighten the surrounding prose to
   make the connection explicit. Do not move or remove the aside.

6. **Generic filler phrases**: Delete or rewrite any of the following on sight.
   Replace with a specific observation, or delete the sentence entirely.
   AI assistant voice (delete):
   - "it is essential to approach" / "it is crucial to"
   - "it is essential to continue monitoring this sector"
   - "remain informed and up-to-date" / "it is crucial to remain informed"
   - "make more informed decisions about their potential impact"
   - "highlights the need for continued discussion"
   - "The [sector] is not without its challenges and limitations"
   - "requires careful consideration of the ethical and societal implications"
   - "This piece is a new development in the ongoing discussion of"
   - "This piece offers a new perspective on the intersection of"
   - "As we consider the implications of" / "Considering the implications of"
   - "As we await further developments"
   - "The situation with [X] is complex and multifaceted"
   - "The research session JSON" / "the session JSON" (meta-references to
     the model's own input — Jeeves reads papers, not JSON)
   - "In synthesizing these findings, it becomes apparent that"
   - "In a similar vein," (as a pivot phrase between topics)
   - "Upon reviewing [the X]," (as a sentence opener)
   - "Regarding [topic]," / "With regard to [topic]," (as section openers)
   - "In the realm of [topic]," / "Delving into the realm of" / "As we delve into"
   - "it is worth noting that" / "it should be noted that"
   - "it becomes apparent" / "it becomes clear"
   - "it is vital to continue monitoring" / "it will be essential to stay vigilant"
   - "it remains to be seen whether"
   - "these developments are noteworthy" / "This development is noteworthy"
   - "I shall keep a watchful eye on"
   - "shall guide you through the day's intelligence briefing"
   - "covering a wide range of topics"
   - "With a mere [N] words allocated to" (fourth-wall break — delete sentence)
   - "This level of activity suggests [X] is undergoing significant changes"
   - "significant implications for the region" (delete the sentence)
   - "significant implications for [X]'s leadership" (delete)
   - "significant escalation of the conflict" (delete)
   - "stark reminder of ongoing instability" (delete)
   - "it will be important to monitor [X] in the coming months" (delete)
   - "this briefing has covered" (delete entire paragraph)
   - "I hope this briefing has been informative" (delete entire paragraph)
   - "If you have any questions or require further clarification" (delete)
   - "Mister Lang, this briefing" as paragraph opener (delete entire paragraph)
   - Any sentence containing no topic-specific nouns that could be
     copy-pasted unchanged into a completely different briefing section.
   Significance commentary (delete entire sentence — show, never declare):
   - "This is a significant development" / "This is a concerning/fascinating/
     disturbing/noteworthy development" — delete the sentence entirely
   - "it highlights the need for careful consideration of the consequences"
   - "It is a complex issue, to be sure" / "It is a complex issue that requires"
   - "requires a nuanced approach, rather than a simplistic or heavy-handed one"
   - "one can only hope that" / "One can only hope" (any form — delete sentence)
   - "I would like to bring to your attention" (delete; Jeeves informs, not defers)
   - "please do not hesitate to inform them" / "please do not hesitate to apply"
   - "The implications of this research are significant"
   - "As you continue to explore this subject" / "As you explore this topic"
   - "The synthesis of these [X] works highlights" / "The synthesis of these findings"
   - "it is a reminder that the world is a complex and often dangerous place"
   - "one that underscores the need for international cooperation and diplomacy"
   - "One would hate to think that"
   - "it is a positive development" / "this is a positive trend"
   - "This is a testament to its commitment" (delete; "testament to" already banned)
   - "This development is a positive step" (delete entire sentence)
   - "This is a fascinating contribution" (delete entire sentence)
   - "I must attend to the rest of the briefing" (delete; meta-narration)
   - "It will be interesting to see" / "It will be worth monitoring" (delete)
   - "It will be worth tracking" (delete)
   - "This raises important questions about" (delete entire sentence)
   - "This highlights the complexities of" (delete entire sentence)
   - "demonstrates the city's commitment to" (delete; civic-PR voice)
   - "represents a significant step forward" (delete entire sentence)
   - "The variety of positions available is quite impressive" (delete)
   - "I shall continue to monitor the situation" (delete; meta-narration)

7. **Section-closing summary paragraphs**: Delete the entire paragraph (not
   just the phrase) when a closing paragraph summarizes with generic language:
   - Any paragraph opening with "The intellectual journals offer..."
   - "The [plural noun] offer(s) a [adjective] exploration / tool / analysis"
   - "Both [X] and [Y] are undergoing significant transformations"
   - "Both [X] and [Y] continue to" (as a closing wrap-up)
   - "Continuing to monitor the progress being made in [X] and [Y] is vital"
   - "Monitor the job boards" / "apply to positions that align with your qualifications"
   - "Be prepared for the application process"
   - "These pieces demonstrate the importance of thoughtful analysis"
   If removing the paragraph leaves the section ending on a specific claim or
   fact, that is correct — remove the paragraph entirely, keep the fact.

8. **Weather re-appearing after Part 1**: If the HTML fragment you are editing
   contains a paragraph describing current Edmonds weather conditions
   (temperature, wind, marine layer, humidity) and the fragment does NOT
   contain `<!-- PART1 END -->`, delete that weather paragraph entirely.
   Weather is Part 1 only.

9. **Profane asides in draft**: The profane asides (phrases like "clusterfuck",
   "fuck-bucket", "cock-womble") are inserted by the final OpenRouter pass only.
   If you see a profane aside in this fragment, REMOVE the aside and close the
   sentence cleanly. Do NOT leave an orphaned parenthetical or mid-sentence fragment.

## Hard rules

- Output ONLY the corrected HTML. No commentary, no markdown fences.
- Do NOT add new content, new asides, new anchor tags, or new paragraphs.
- Do NOT change existing anchor URLs.
- Do NOT alter verbatim quoted text (e.g. New Yorker article body).
- If nothing needs fixing, output the HTML unchanged.
- **EMPTY FRAGMENT GUARD**: If the HTML you receive contains fewer than 20
  words of body text (e.g. a single `<p></p>` or a one-sentence placeholder),
  return it UNCHANGED. Do NOT add content, sections, or topics.
""".strip()


_NIM_RETRY_DELAYS = (2, 8, 32)  # seconds between attempts on 429
# Shorter timeout for write-phase NIM calls — when NIM hangs the whole pipeline
# burns the daily.yml 60min budget. classify_with_kimi already learned this
# (sprint-15 hotfix). Now write-phase NIM gets the same 60s ceiling.
_NIM_WRITE_TIMEOUT_S = 60.0
# OpenRouter free-tier write-fallback chain. Fires when BOTH Groq AND NIM fail.
# Drop gemma-2-9b — known paraphrase offender per playwright_extractor research.
_OR_WRITE_FALLBACK_MODELS = (
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen-2.5-72b-instruct:free",
)
# Module-level circuit breaker: once NIM times out for any part during a run,
# subsequent parts skip NIM and go directly to OR. Reset at module-init time
# (each pipeline run gets a fresh process). Without this, every Part 2-9
# retried the same hung NIM endpoint for another 60-180s each.
_NIM_WRITE_DEAD = False


def _is_nim_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


def _is_nim_timeout(exc: Exception) -> bool:
    """Match openai.APITimeoutError, httpx.TimeoutException, or generic timeout."""
    cls = type(exc).__name__.lower()
    msg = str(exc).lower()
    return (
        "timeout" in cls
        or "timeout" in msg
        or "timed out" in msg
        or "peer closed connection" in msg
    )


def _invoke_or_write(
    cfg: Config, system: str, user: str, *, max_tokens: int, label: str
) -> str:
    """Last-resort write call when BOTH Groq AND NIM fail.

    Iterates _OR_WRITE_FALLBACK_MODELS; first model returning content wins.
    Returns the assistant message text, or raises RuntimeError on total failure.
    """
    api_key = (cfg.openrouter_api_key or "").strip()
    if not api_key:
        raise RuntimeError(
            f"NIM write [{label}] failed AND OPENROUTER_API_KEY is not set. "
            "Add OPENROUTER_API_KEY to GitHub Secrets so write-phase has a "
            "third-tier fallback."
        )
    try:
        from openai import OpenAI
    except ImportError as e:
        raise RuntimeError(
            f"NIM write [{label}] failed AND openai SDK not installed for OR fallback: {e}"
        ) from e

    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=120,
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    last_exc: Exception | None = None
    for model_id in _OR_WRITE_FALLBACK_MODELS:
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.65,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                log.warning(
                    "OR write fallback [%s] succeeded via %s (Groq+NIM both failed)",
                    label, model_id,
                )
                return text
        except Exception as exc:
            last_exc = exc
            log.debug("OR write [%s] %s failed: %s", label, model_id, exc)
            continue
    raise RuntimeError(f"OR write [{label}] exhausted all models: {last_exc}")


def _invoke_nim_refine(cfg: Config, draft_html: str, *, label: str) -> str:
    """Run a targeted quality-editor pass on a draft HTML fragment via NIM.

    Uses a short focused system prompt (not the full write system) at lower
    temperature — the task is edit/fix, not creative generation. Falls back
    to the raw draft if NIM is unavailable or the call fails.

    Retries up to 3 times with exponential backoff (2s, 8s, 32s) on HTTP 429.
    """
    import time

    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .llm import build_nim_write_llm

    if not cfg.nvidia_api_key:
        log.debug("NVIDIA_API_KEY not set; skipping refine for [%s]", label)
        return draft_html
    if cfg.skip_nim_refine:
        log.info("JEEVES_SKIP_NIM_REFINE set; skipping NIM refine for [%s]", label)
        return draft_html

    llm = build_nim_write_llm(cfg, temperature=0.2, max_tokens=4096)
    user = f"Edit the following HTML fragment:\n\n{draft_html}"
    log.info("NIM refine [%s] (%d chars draft)", label, len(draft_html))
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=_REFINE_SYSTEM),
        ChatMessage(role=MessageRole.USER, content=user),
    ]
    raw_h3_count = len(_H3_TAG_RE.findall(draft_html))
    raw_len = len(draft_html)
    last_exc: Exception | None = None
    for attempt, delay in enumerate((*_NIM_RETRY_DELAYS, None)):
        try:
            resp = llm.chat(messages)
            edited = str(resp.message.content or draft_html)
            # Scope guard: refine must not expand the fragment.
            edited_h3 = len(_H3_TAG_RE.findall(edited))
            if edited_h3 > raw_h3_count + 1:
                log.warning(
                    "NIM refine [%s] expanded h3 count %d→%d — "
                    "rejecting refined output, using raw draft",
                    label, raw_h3_count, edited_h3,
                )
                return draft_html
            if len(edited) > int(raw_len * 1.5) and raw_len > 200:
                log.warning(
                    "NIM refine [%s] expanded length %d→%d (>1.5x) — "
                    "rejecting refined output, using raw draft",
                    label, raw_len, len(edited),
                )
                return draft_html
            return edited
        except Exception as exc:
            last_exc = exc
            if _is_nim_rate_limit(exc) and delay is not None:
                log.warning(
                    "NIM refine [%s] got 429 (attempt %d/4); retrying in %ds",
                    label, attempt + 1, delay,
                )
                time.sleep(delay)
            else:
                raise
    log.warning("NIM refine [%s] exhausted retries: %s; using raw draft", label, last_exc)
    return draft_html


def _invoke_nim_write(cfg: Config, system: str, user: str, *, max_tokens: int, label: str) -> str:
    """Call NIM as a fallback write-draft generator (Groq TPD/TPM exhausted).

    Behavior:
    - 60s timeout per request (down from 180s — production NIM hangs eat the
      60min daily.yml budget if we wait too long. Sprint-15 fix for classify;
      sprint-17 extension for write.)
    - 1 retry on 429 (rate-limit) — wait 60s for window to clear, then retry.
    - 0 retries on timeout — sets _NIM_WRITE_DEAD circuit breaker so the next
      part skips NIM entirely.
    - On non-rate-limit non-timeout exception: raise immediately.

    Caller (`_invoke_write_llm`) handles OR fallback when this raises.
    """
    import time

    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .llm import build_nim_write_llm

    global _NIM_WRITE_DEAD

    if not cfg.nvidia_api_key:
        raise RuntimeError(
            "Groq TPD exhausted and NVIDIA_API_KEY is not set — cannot fall back to NIM. "
            "Add NVIDIA_API_KEY to secrets or wait for Groq's daily quota to reset (midnight UTC)."
        )
    llm = build_nim_write_llm(
        cfg, temperature=0.65, max_tokens=max_tokens, timeout=_NIM_WRITE_TIMEOUT_S,
    )
    log.info(
        "invoking NIM write fallback %s [%s] (max_tokens=%d, timeout=%.0fs, "
        "system=%d chars, user=%d chars)",
        cfg.nim_write_model_id, label, max_tokens, _NIM_WRITE_TIMEOUT_S,
        len(system), len(user),
    )
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=user),
    ]

    # Two attempts max: initial + ONE 60s rate-limit retry. No retry on timeout
    # — if NIM is hanging, retrying just burns more budget.
    for attempt in range(2):
        try:
            resp = llm.chat(messages)
            return str(resp.message.content or "")
        except Exception as exc:
            if _is_nim_rate_limit(exc) and attempt == 0:
                log.warning(
                    "NIM write [%s] got 429 (attempt 1/2); waiting 60s for window to clear",
                    label,
                )
                time.sleep(60)
                continue
            if _is_nim_timeout(exc):
                # Trip circuit breaker — subsequent parts skip NIM entirely.
                _NIM_WRITE_DEAD = True
                log.warning(
                    "NIM write [%s] timeout (%s) — tripping circuit breaker, "
                    "remaining parts will skip NIM and go to OR directly",
                    label, exc,
                )
                raise
            raise
    raise RuntimeError(f"NIM write [{label}] exhausted retries")


def _try_nim_then_or(
    cfg: Config, system: str, user: str, *, max_tokens: int, label: str
) -> str:
    """Try NIM (unless circuit broken), fall back to OpenRouter on any failure.

    This is the post-Groq escalation path. Either the input was too big for
    Groq's TPM, or Groq returned a TPD-exhaustion error.
    """
    if _NIM_WRITE_DEAD:
        log.warning(
            "NIM circuit broken — [%s] skipping NIM, going directly to OR fallback",
            label,
        )
        return _invoke_or_write(cfg, system, user, max_tokens=max_tokens, label=label)
    try:
        return _invoke_nim_write(cfg, system, user, max_tokens=max_tokens, label=label)
    except Exception as nim_exc:
        log.warning(
            "NIM write [%s] failed (%s) — escalating to OpenRouter fallback chain",
            label, nim_exc,
        )
        try:
            return _invoke_or_write(
                cfg, system, user, max_tokens=max_tokens, label=label,
            )
        except Exception as or_exc:
            # Both NIM and OR died — no third tier. Raise the OR error chained
            # to NIM's so the run manifest preserves both.
            raise RuntimeError(
                f"write [{label}] all three tiers failed: NIM={nim_exc!r}, OR={or_exc!r}"
            ) from or_exc


def _invoke_write_llm(
    cfg: Config, system: str, user: str, *, max_tokens: int, label: str
) -> tuple[str, bool]:
    """Call Groq for the write phase; auto-fall back to NIM → OR on failure.

    Returns (text, used_groq). used_groq=False means Groq was skipped/exhausted
    and NIM (or OR) handled the draft — the caller can skip the Groq TPM
    cooldown sleep.

    Three-tier escalation:
    - Tier 1: Groq Llama 3.3 70B (primary, free tier).
    - Tier 2: NIM meta/llama-3.3-70b-instruct (60s timeout + circuit breaker).
    - Tier 3: OpenRouter free-tier chain (llama-3.3-70b → qwen-2.5-72b).

    Triggers:
    - Input tokens exceed TPM ceiling → skip Groq, go straight to NIM/OR.
    - Groq daily TPD quota exhausted → escalate to NIM/OR.
    - NIM timeout/rate-limit → escalate to OR; trip circuit breaker.
    """
    _, input_tokens = _clamp_groq_max_tokens(system, user, max_tokens)
    available = _GROQ_TPM_LIMIT - input_tokens - _GROQ_TPM_SAFETY
    if available <= 0:
        log.warning(
            "Groq skipped for [%s]: input ~%d tokens exceeds TPM ceiling %d "
            "(available=%d); routing directly to NIM/OR.",
            label, input_tokens, _GROQ_TPM_LIMIT, available,
        )
        return _try_nim_then_or(cfg, system, user, max_tokens=max_tokens, label=label), False
    try:
        return _invoke_groq(cfg, system, user, max_tokens=max_tokens, label=label), True
    except Exception as e:
        if "tokens per day" in str(e).lower():
            log.warning(
                "Groq daily TPD quota exhausted on [%s]; retrying on NIM/OR. "
                "Groq free-tier resets at midnight UTC.",
                label,
            )
            return _try_nim_then_or(cfg, system, user, max_tokens=max_tokens, label=label), False
        raise


_NY_INTRO_MARKER = "reading from this week's Talk of the Town"
_NY_SIGNOFF_MARKERS = ('<div class="signoff">', "<!-- COVERAGE_LOG")


_BANNER_URL = "https://i.imgur.com/UqSFELh.png"
_BANNER_HTML = f'<img class="banner" src="{_BANNER_URL}" alt="">'
_BANNER_RE = re.compile(r'<img\b[^>]*\bclass="banner"[^>]*>', re.IGNORECASE)
_CONTAINER_OPEN_RE = re.compile(r'(<div\b[^>]*\bclass="container"[^>]*>)', re.IGNORECASE)


def _inject_banner(html: str) -> str:
    """Idempotently guarantee the banner image sits at the top of the container.

    Three cases:
    1. Banner present with correct URL — leave alone.
    2. Banner present with WRONG URL (e.g. model rewrote src) — replace in place.
    3. Banner absent — inject immediately after `<div class="container">`.

    Safe to call multiple times. Idempotent against case (1). Fails open if no
    container open tag is found (logs warning, returns html unchanged).
    """

    existing = _BANNER_RE.search(html)
    if existing:
        if _BANNER_URL in existing.group(0):
            return html
        # Wrong URL — replace in place.
        log.warning(
            "banner URL drift detected (had: %.80r); replacing with canonical",
            existing.group(0),
        )
        return _BANNER_RE.sub(_BANNER_HTML, html, count=1)

    m = _CONTAINER_OPEN_RE.search(html)
    if not m:
        log.warning("banner injection skipped: no .container open tag found")
        return html
    insert_at = m.end()
    return html[:insert_at] + "\n" + _BANNER_HTML + html[insert_at:]


_TOTT_INTRO_PARAGRAPH = (
    "<p>And now, Sir, I take the liberty of reading from this week's "
    "Talk of the Town in The New Yorker.</p>"
)
_TOTT_PLACEHOLDER = "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->"
_TOTT_HEADER = "<h3>Talk of the Town</h3>"


def _ensure_tott_scaffolding(part9_html: str, newyorker_available: bool, ny_url: str = "") -> str:
    """Programmatically guarantee Part 9 contains the intro + placeholder.

    Models repeatedly skip the verbatim intro paragraph and placeholder comment
    despite explicit instructions. Without those anchors, _inject_newyorker_verbatim
    cannot place the article text. This function scans Part 9 output and prepends
    whatever scaffolding is missing — idempotent (won't double-up if model
    cooperated).

    Order in output:
      <h3>Talk of the Town</h3>
      <p>And now, Sir, I take the liberty of reading...</p>
      <!-- NEWYORKER_CONTENT_PLACEHOLDER -->
      <p><a href="...">Read at The New Yorker</a></p>   (added if missing)
      [model's signoff block + closing tags]
    """
    if not newyorker_available:
        return part9_html
    additions: list[str] = []
    if _TOTT_HEADER not in part9_html and "Talk of the Town</h3>" not in part9_html:
        additions.append(_TOTT_HEADER)
    if _NY_INTRO_MARKER not in part9_html:
        additions.append(_TOTT_INTRO_PARAGRAPH)
    if _TOTT_PLACEHOLDER not in part9_html:
        additions.append(_TOTT_PLACEHOLDER)
    if ny_url and "Read at The New Yorker" not in part9_html:
        additions.append(f'<p><a href="{ny_url}">Read at The New Yorker</a></p>')
    if not additions:
        return part9_html
    log.warning(
        "Part 9 model omitted %d of 4 TOTT scaffolding elements; pre-injecting: %s",
        len(additions),
        ", ".join(_TOTT_HEADER if a == _TOTT_HEADER
                  else "intro" if a == _TOTT_INTRO_PARAGRAPH
                  else "placeholder" if a == _TOTT_PLACEHOLDER
                  else "read-link"
                  for a in additions),
    )
    # Insert before <div class="signoff"> if present, else prepend.
    signoff_pos = part9_html.find('<div class="signoff">')
    scaffold = "\n" + "\n".join(additions) + "\n"
    if signoff_pos != -1:
        return part9_html[:signoff_pos] + scaffold + part9_html[signoff_pos:]
    return scaffold + part9_html


def _build_newyorker_block(text: str, url: str) -> str:
    """Return formatted NEWYORKER_START…END block. NO Read link.

    PART9_INSTRUCTIONS Step 3 already tells the model to write
    `<p><a href="...">Read at The New Yorker</a></p>` after the placeholder.
    Adding it again here produced two adjacent Read links every run
    (the duplicate-link bug visible in briefings 2026-05-01 and 05-02).
    Letting Part 9 own it keeps a single source of truth.

    `url` is accepted for backwards compatibility with callers but unused.
    """
    del url  # intentional: Part 9 owns the Read link
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    return (
        "<!-- NEWYORKER_START -->\n"
        + '<div class="newyorker">\n'
        + '<div class="ny-header">The New Yorker &middot; Talk of the Town</div>\n'
        + "\n".join(f"<p>{p}</p>" for p in paragraphs)
        + "\n</div>"
        + "\n<!-- NEWYORKER_END -->"
    )


def _inject_newyorker_verbatim(html: str, session: SessionModel) -> str:
    """Replace Part 9's <!-- NEWYORKER_CONTENT_PLACEHOLDER --> with actual article text.

    Part 9 is instructed to output the placeholder rather than copy newyorker.text
    itself (models copy text imperfectly). This step injects the real text
    deterministically so it is always verbatim.

    The injected content is wrapped in <!-- NEWYORKER_START --> / <!-- NEWYORKER_END -->
    sentinel comments so the downstream narrative editor knows not to touch it.

    Fallback behaviour when the placeholder is absent (Part 9 hallucinated content):
    - Find the intro paragraph ("reading from this week's Talk of the Town")
    - Find the sign-off anchor (<div class="signoff"> or <!-- COVERAGE_LOG)
    - REPLACE everything between them with the real article block
      (this surgically excises any hallucinated content and [TRUNCATED] artefacts)
    """
    ny_text = session.newyorker.text if (session.newyorker.available and session.newyorker.text) else ""
    ny_url = session.newyorker.url or ""

    # Happy path: placeholder present.
    if "<!-- NEWYORKER_CONTENT_PLACEHOLDER -->" in html:
        if not ny_text:
            return html.replace("<!-- NEWYORKER_CONTENT_PLACEHOLDER -->", "")
        block = _build_newyorker_block(ny_text, ny_url)
        return html.replace("<!-- NEWYORKER_CONTENT_PLACEHOLDER -->", block)

    # Fallback: placeholder absent (model hallucinated content instead).
    if not ny_text:
        return html

    log.warning(
        "NEWYORKER_CONTENT_PLACEHOLDER missing from Part 9 output — "
        "excising hallucinated content and injecting verbatim article text."
    )

    # Find the intro sentence end.
    if _NY_INTRO_MARKER not in html:
        # FORCE-INJECT: model wrote neither placeholder nor intro paragraph.
        # We MUST NOT lose the verbatim article text. Find <div class="signoff">
        # and inject intro + NY block + Read link immediately before it.
        signoff_pos = -1
        for marker in _NY_SIGNOFF_MARKERS:
            pos = html.find(marker)
            if pos != -1 and (signoff_pos == -1 or pos < signoff_pos):
                signoff_pos = pos
        if signoff_pos == -1:
            log.error(
                "TOTT FORCE-INJECT failed: neither intro marker nor signoff "
                "anchor found in Part 9 output. Verbatim article text lost."
            )
            return html
        log.warning(
            "Talk of the Town intro sentence missing — FORCE-INJECTING "
            "intro + verbatim block + Read link before <div class='signoff'>."
        )
        forced_block = (
            "\n<h3>Talk of the Town</h3>\n"
            "<p>And now, Sir, I take the liberty of reading from this week's "
            "Talk of the Town in The New Yorker.</p>\n"
            + _build_newyorker_block(ny_text, ny_url)
            + (f'\n<p><a href="{ny_url}">Read at The New Yorker</a></p>\n' if ny_url else "\n")
        )
        return html[:signoff_pos] + forced_block + html[signoff_pos:]

    idx = html.find(_NY_INTRO_MARKER)
    close_p = html.find("</p>", idx)
    if close_p == -1:
        log.warning("Could not find </p> after TOTT intro — skipping injection.")
        return html
    intro_end = close_p + 4  # position just after </p>

    # Find the sign-off anchor — everything between intro_end and here gets replaced.
    signoff_idx = -1
    for marker in _NY_SIGNOFF_MARKERS:
        pos = html.find(marker, intro_end)
        if pos != -1:
            if signoff_idx == -1 or pos < signoff_idx:
                signoff_idx = pos

    # In the fallback path the model's hallucinated content is excised —
    # which removes whatever Read link Part 9 wrote. We must inject one
    # explicitly here. (Happy-path placeholder substitution does NOT need
    # this — Part 9's Step 3 link survives the placeholder replacement.)
    fallback_read_link = (
        f'\n<p><a href="{ny_url}">Read at The New Yorker</a></p>'
        if ny_url
        else ""
    )

    if signoff_idx == -1:
        # No sign-off found — just insert after intro, leave rest intact.
        log.warning("Could not find sign-off anchor; inserting after intro only.")
        block = "\n" + _build_newyorker_block(ny_text, ny_url) + fallback_read_link + "\n"
        return html[:intro_end] + block + html[intro_end:]

    # Splice: intro_end … signoff_idx is the hallucinated zone — replace entirely.
    block = "\n" + _build_newyorker_block(ny_text, ny_url) + fallback_read_link + "\n"
    return html[:intro_end] + block + html[signoff_idx:]


_NY_READ_LINK_RE = re.compile(
    r'<p>\s*<a\s+href="([^"]+)"[^>]*>\s*Read at The New Yorker\s*</a>\s*</p>',
    re.IGNORECASE,
)


def _ensure_single_newyorker_read_link(html: str, session: SessionModel) -> str:
    """Guarantee exactly one `<p><a>Read at The New Yorker</a></p>`.

    Idempotent. Three cases:
    1. NY block absent → no Read link needed; strip any that exist.
    2. NY block present + ≥1 Read link → keep the FIRST one after NEWYORKER_END;
       strip all others (this is the bug we are fixing — Part 9 + injector
       both wrote one, producing two adjacent links).
    3. NY block present + 0 Read links → inject one immediately after
       NEWYORKER_END (Part 9 dropped it; safety net).
    """
    has_ny_block = "<!-- NEWYORKER_END -->" in html
    if not has_ny_block:
        # Strip any orphan Read links — they don't belong without TOTT.
        return _NY_READ_LINK_RE.sub("", html)

    matches = list(_NY_READ_LINK_RE.finditer(html))
    if len(matches) >= 1:
        # Keep the first Read link, strip the rest.
        if len(matches) > 1:
            log.info(
                "stripping %d duplicate `Read at The New Yorker` links", len(matches) - 1
            )
            # Build new HTML keeping only the first match.
            keep = matches[0]
            pieces: list[str] = [html[:keep.end()]]
            cursor = keep.end()
            for m in matches[1:]:
                pieces.append(html[cursor:m.start()])
                cursor = m.end()
            pieces.append(html[cursor:])
            return "".join(pieces)
        return html

    # No Read link present — inject one after NEWYORKER_END.
    ny_url = session.newyorker.url or ""
    if not ny_url:
        return html
    inject = f'\n<p><a href="{ny_url}">Read at The New Yorker</a></p>'
    return html.replace("<!-- NEWYORKER_END -->", "<!-- NEWYORKER_END -->" + inject, 1)


_DOMAIN_NICE_NAMES: dict[str, list[str]] = {
    # domain fragment → list of prose names the model uses
    "aeon.co": ["Aeon"],
    "propublica.org": ["ProPublica"],
    "nybooks.com": ["NYRB", "New York Review of Books", "New York Review"],
    "themarginalian.org": ["The Marginalian", "Marginalian"],
    "theguardian.com": ["The Guardian", "Guardian"],
    "bbc.co.uk": ["BBC", "BBC News"],
    "bbc.com": ["BBC", "BBC News"],
    "reuters.com": ["Reuters"],
    "nytimes.com": ["The New York Times", "New York Times", "NYT"],
    "washingtonpost.com": ["The Washington Post", "Washington Post"],
    "lrb.co.uk": ["LRB", "London Review of Books"],
    "economist.com": ["The Economist", "Economist"],
    "ft.com": ["Financial Times", "FT"],
    "newyorker.com": ["The New Yorker", "New Yorker"],
    "euronews.com": ["Euronews"],
    "aljazeera.com": ["Al Jazeera"],
    "apnews.com": ["AP", "Associated Press"],
    "axios.com": ["Axios"],
    "politico.com": ["Politico"],
    "theatlantic.com": ["The Atlantic", "Atlantic"],
    "wired.com": ["Wired"],
    "techcrunch.com": ["TechCrunch"],
    "newsweek.com": ["Newsweek"],
    "bigthink.com": ["Big Think"],
    "frontline": ["FRONTLINE", "Frontline"],
}

# Prose aliases that may differ from how Kimi names the source in session JSON.
_SOURCE_ALIASES: dict[str, list[str]] = {
    "BBC": ["BBC News", "the BBC"],
    "Reuters": ["Reuters news agency"],
    "ProPublica": ["ProPublica", "ProPublica and FRONTLINE"],
    "NYRB": ["New York Review of Books"],
    "Marginalian": ["The Marginalian"],
}


def _build_source_url_map(session: SessionModel) -> dict[str, str]:
    """Build {source_name: canonical_url} from all structured session sectors.

    Uses the `source` field (populated by Kimi during research) matched to
    the first URL in the sector item's urls list. The map drives
    _inject_source_links — which injects <a href> anchors deterministically
    after the model generates prose.

    Also expands domain-style source names (e.g. "aeon.co") to the prose names
    the model actually uses ("Aeon") so _inject_source_links can find matches.
    """
    mapping: dict[str, str] = {}

    def _add_with_aliases(name: str | None, url: str | None) -> None:
        if not name or not url:
            return
        _add(name, url)
        # Expand domain-style sources to nice prose names.
        for domain_frag, nice_names in _DOMAIN_NICE_NAMES.items():
            if domain_frag in name.lower():
                for nice in nice_names:
                    _add(nice, url)
                return
        # Expand known prose aliases.
        for canonical, aliases in _SOURCE_ALIASES.items():
            if name == canonical:
                for alias in aliases:
                    _add(alias, url)
                return

    def _add(name: str | None, url: str | None) -> None:
        if name and url and name not in mapping:
            mapping[name] = url

    for item in session.local_news or []:
        urls = item.get("urls") if isinstance(item, dict) else getattr(item, "urls", [])
        src = item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
        if urls:
            _add_with_aliases(src, urls[0])

    for item in session.global_news or []:
        urls = item.get("urls") if isinstance(item, dict) else getattr(item, "urls", [])
        src = item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
        if urls:
            _add_with_aliases(src, urls[0])

    for item in session.intellectual_journals or []:
        urls = item.get("urls") if isinstance(item, dict) else getattr(item, "urls", [])
        src = item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
        if urls:
            _add_with_aliases(src, urls[0])

    for item in session.wearable_ai or []:
        urls = item.get("urls") if isinstance(item, dict) else getattr(item, "urls", [])
        src = item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
        if urls:
            # Use editorial source name if present; fall back to domain extracted
            # from the first URL so product sites (friend.com, magicschool.ai etc.)
            # can still be linked when the model writes their domain as prose.
            if src:
                _add_with_aliases(src, urls[0])
            else:
                from urllib.parse import urlparse as _urlparse
                domain = _urlparse(urls[0]).netloc.lstrip("www.")
                if domain:
                    _add(domain, urls[0])

    for item in session.enriched_articles or []:
        url = item.get("url") if isinstance(item, dict) else getattr(item, "url", None)
        src = item.get("source") if isinstance(item, dict) else getattr(item, "source", None)
        title = item.get("title") if isinstance(item, dict) else getattr(item, "title", None)
        _add(src, url)
        # Map article title → url so inline title mentions get linked.
        _add(title, url)

    # --- Career postings: each opening's title/school → application URL. ---
    career = session.career or {}
    if isinstance(career, dict):
        for opening in career.get("openings") or []:
            if not isinstance(opening, dict):
                continue
            url = opening.get("url") or ""
            if not url:
                continue
            for key in ("title", "position", "school", "district"):
                val = opening.get(key)
                if isinstance(val, str) and val.strip():
                    _add(val.strip(), url)

    # --- Family events: choir auditions and toddler activities. ---
    family = session.family or {}
    if isinstance(family, dict):
        for bucket_name in ("choir", "toddler"):
            bucket = family.get(bucket_name) or []
            if isinstance(bucket, list):
                for ev in bucket:
                    if not isinstance(ev, dict):
                        continue
                    url = ev.get("url") or ""
                    if not url:
                        continue
                    for key in ("name", "ensemble", "venue", "title", "activity"):
                        val = ev.get(key)
                        if isinstance(val, str) and val.strip():
                            _add(val.strip(), url)

    # --- Literary pick: title and author. ---
    lp = session.literary_pick
    lp_url = getattr(lp, "url", "") or ""
    if lp_url:
        if getattr(lp, "title", ""):
            _add(lp.title, lp_url)
        if getattr(lp, "author", ""):
            _add(lp.author, lp_url)

    # Scalar sector URL fields — map known editorial names to each URL position.
    to_urls = getattr(session.triadic_ontology, "urls", None) or []
    _TO_NAMES = ["Academia.edu", "PhilArchive", "Nomos-elibrary"]
    for name, url in zip(_TO_NAMES, to_urls):
        _add(name, url)

    ai_urls = getattr(session.ai_systems, "urls", None) or []
    if ai_urls:
        _add("arXiv", ai_urls[0])
    uap_urls = getattr(session.uap, "urls", None) or []
    if uap_urls:
        _add("House Oversight Committee", uap_urls[0])

    return mapping


# Per-source injection cap. Up to 3 occurrences of the same name across
# different paragraphs each get their own anchor — anti-clutter while still
# producing the dense link mat the briefing wants.
_INJECT_PER_SOURCE = 3


def _inject_source_links(html: str, source_url_map: dict[str, str]) -> str:
    """Deterministically inject <a href> anchors for known source names.

    For each (source_name, url) pair, finds up to `_INJECT_PER_SOURCE` occurrences
    of source_name that are NOT inside an existing `<a>` tag and wraps them.

    Operates on the raw HTML string using a split-on-anchors approach so
    existing links are never disturbed.
    """
    if not source_url_map:
        return html

    _A_SPLIT = re.compile(r"(<a\b[^>]*>.*?</a>)", re.IGNORECASE | re.DOTALL)

    for source_name, url in source_url_map.items():
        if not source_name or not url:
            continue
        # If this URL is ALREADY anchored anywhere, count those toward the cap
        # so we top up rather than re-add.
        existing_anchors_for_url = html.count(f'href="{url}"')
        remaining_quota = _INJECT_PER_SOURCE - existing_anchors_for_url
        if remaining_quota <= 0:
            continue

        escaped = re.escape(source_name)
        pattern = re.compile(
            r"(?<![a-zA-Z0-9\-])" + escaped + r"(?![a-zA-Z0-9\-])",
            re.IGNORECASE,
        )

        segments = _A_SPLIT.split(html)
        injected = 0
        for i, seg in enumerate(segments):
            if injected >= remaining_quota:
                break
            if i % 2 != 0:
                continue  # inside an existing anchor, skip
            if not pattern.search(seg):
                continue
            count_to_take = remaining_quota - injected
            new_seg, count = pattern.subn(
                lambda m, _url=url: f'<a href="{_url}">{m.group(0)}</a>',
                seg,
                count=count_to_take,
            )
            if count:
                segments[i] = new_seg
                injected += count
        if injected:
            html = "".join(segments)

    return html


def _compute_link_density(html: str, word_count: int) -> float:
    """anchors per 1000 prose-words. Used as a quality diagnostic only."""
    if word_count <= 0:
        return 0.0
    anchors = len(re.findall(r"<a\b[^>]*\bhref=", html, re.IGNORECASE))
    return round(anchors * 1000.0 / word_count, 2)


# --- Profane aside placement guard ----------------------------------------
# Pattern that catches the templated standalone-paragraph regression seen in
# 2026-05-01: `<p>[topic phrase] is, [profane aside].</p>` with the entire
# paragraph being short, lowercase-opening, and aside-dominated.
_NY_BLOCK_FENCE_RE = re.compile(
    r"<!--\s*NEWYORKER_START\s*-->.*?<!--\s*NEWYORKER_END\s*-->",
    re.DOTALL | re.IGNORECASE,
)
_P_TAG_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)


def _paragraph_is_aside_orphan(p_text: str) -> tuple[bool, str | None]:
    """Return (is_orphan, matched_fragment).

    A paragraph is an aside-orphan when:
    - it contains a profane fragment from PROFANE_FRAGMENTS, AND
    - its overall word count is < 20, OR
    - it begins with a lowercase letter (sign of slot-template), AND
    - the aside is not anchored to ≥1 sentence-ending period of substantive
      content BEFORE the profane fragment.
    """
    plain = re.sub(r"<[^>]+>", " ", p_text)
    plain = re.sub(r"\s+", " ", plain).strip()
    if not plain:
        return False, None
    lower = plain.lower()
    matched = None
    matched_idx = -1
    for frag in PROFANE_FRAGMENTS:
        idx = lower.find(frag)
        if idx >= 0:
            matched = frag
            matched_idx = idx
            break
    if matched is None:
        return False, None

    word_count = len(plain.split())
    starts_lowercase = plain[0].islower() if plain else False

    # Substantive prose BEFORE the aside: at least one sentence-ending period
    # in the first matched_idx characters.
    pre_aside = plain[:matched_idx]
    pre_periods = pre_aside.count(".") + pre_aside.count("!") + pre_aside.count("?")

    if word_count < 20:
        return True, matched
    if starts_lowercase and pre_periods == 0:
        return True, matched
    if pre_periods == 0 and word_count < 30:
        return True, matched
    return False, None


def _validate_aside_placement(html: str) -> list[str]:
    """Return human-readable warnings for orphan-template aside placements."""
    # Skip the NEWYORKER block — verbatim article text doesn't apply.
    scoped = _NY_BLOCK_FENCE_RE.sub("", html)
    warnings: list[str] = []
    for m in _P_TAG_RE.finditer(scoped):
        body = m.group(1)
        is_orphan, frag = _paragraph_is_aside_orphan(body)
        if is_orphan:
            preview = re.sub(r"<[^>]+>", "", body).strip()[:80]
            warnings.append(f"orphan aside ({frag!r}): {preview!r}")
    return warnings


_H3_TAG_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)


def _collapse_adjacent_duplicate_h3(html: str) -> str:
    """Strip adjacent duplicate `<h3>` headers (case-insensitive text match).

    When Parts 6 + 7 both emit `<h3>The Specific Enquiries</h3>`, the stitched
    output shows the header twice in a row with no content between (or only
    whitespace). This collapses the duplicate so the briefing reads cleanly.

    Only collapses adjacent duplicates — non-adjacent duplicates may be
    legitimate section reuse (rare but possible).
    """
    matches = list(_H3_TAG_RE.finditer(html))
    if len(matches) < 2:
        return html

    drop_ranges: list[tuple[int, int]] = []
    for i in range(1, len(matches)):
        prev = matches[i - 1]
        cur = matches[i]
        # Compare normalized text content.
        prev_text = re.sub(r"\s+", " ", prev.group(1)).strip().lower()
        cur_text = re.sub(r"\s+", " ", cur.group(1)).strip().lower()
        if prev_text != cur_text:
            continue
        # Adjacent only if the content between is whitespace, comments,
        # or empty paragraphs.
        between = html[prev.end():cur.start()]
        between_stripped = re.sub(r"<!--.*?-->", "", between, flags=re.DOTALL)
        between_stripped = re.sub(r"<p>\s*</p>", "", between_stripped, flags=re.IGNORECASE)
        between_stripped = re.sub(r"\s+", "", between_stripped)
        if between_stripped:
            continue
        drop_ranges.append((prev.end(), cur.end()))

    if not drop_ranges:
        return html

    log.info("collapsing %d adjacent duplicate <h3> headers", len(drop_ranges))
    # Apply drops back-to-front so indices stay valid.
    for start, end in reversed(drop_ranges):
        html = html[:start] + html[end:]
    return html


def _shingles(text: str, k: int = 3) -> set[str]:
    """Return set of k-word shingles (lowercased) for Jaccard comparison.

    k=3 chosen because k=4 missed near-duplicate paragraphs (jaccard 0.48
    on near-paraphrases) while k=3 hits 0.55+. Distinct topics still score 0.
    """
    words = re.sub(r"\s+", " ", text).strip().lower().split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _paragraph_quality_score(body: str) -> tuple[int, int, int]:
    """Quality score for a paragraph: (anchor_count, word_count, char_count).

    Higher anchor_count = richer (cites sources). Word/char tiebreakers.
    Used to pick the BEST occurrence of a duplicate, not the first.
    """
    anchors = len(re.findall(r"<a\s[^>]*href=", body, re.IGNORECASE))
    plain = re.sub(r"<[^>]+>", " ", body)
    plain = re.sub(r"\s+", " ", plain).strip()
    words = len(plain.split())
    return (anchors, words, len(plain))


def _dedup_paragraphs_across_blocks(
    html: str, *, jaccard_threshold: float = 0.5
) -> str:
    """Drop near-duplicate <p> bodies via 4-word shingle Jaccard similarity.

    For each pair of paragraphs with Jaccard ≥ threshold, drop the one with
    the LOWER quality score (anchors → words → chars). Keeps the richer copy.

    Skips:
    - Paragraphs inside the verbatim TOTT block.
    - Paragraphs inside `<div class="signoff">`.
    - Paragraphs ≤ 6 words.
    """
    ny_match = _NY_BLOCK_FENCE_RE.search(html)
    sentinel = "<!--__JEEVES_NY_DEDUP_TMP__-->"
    if ny_match:
        ny_saved = ny_match.group(0)
        scoped = _NY_BLOCK_FENCE_RE.sub(sentinel, html, count=1)
    else:
        ny_saved = None
        scoped = html

    paragraphs: list[tuple[int, int, str, set[str], tuple[int, int, int]]] = []
    for m in _P_TAG_RE.finditer(scoped):
        body = m.group(1)
        plain = re.sub(r"<[^>]+>", " ", body)
        plain = re.sub(r"\s+", " ", plain).strip()
        if len(plain.split()) <= 6:
            continue
        sh = _shingles(plain, k=3)
        if len(sh) < 3:
            continue
        score = _paragraph_quality_score(body)
        paragraphs.append((m.start(), m.end(), body, sh, score))

    if len(paragraphs) < 2:
        if ny_saved:
            scoped = scoped.replace(sentinel, ny_saved, 1)
        return scoped

    drop: set[int] = set()
    for i in range(len(paragraphs)):
        if i in drop:
            continue
        _, _, _, sh_i, score_i = paragraphs[i]
        for j in range(i + 1, len(paragraphs)):
            if j in drop:
                continue
            _, _, _, sh_j, score_j = paragraphs[j]
            if _jaccard(sh_i, sh_j) < jaccard_threshold:
                continue
            # Keep richer; drop poorer.
            if score_j > score_i:
                drop.add(i)
                # i is gone — no point comparing further i↔k pairs.
                break
            else:
                drop.add(j)

    if not drop:
        if ny_saved:
            scoped = scoped.replace(sentinel, ny_saved, 1)
        return scoped

    log.warning(
        "paragraph dedup: dropping %d near-duplicate <p> blocks (Jaccard >= %.2f)",
        len(drop), jaccard_threshold,
    )

    for idx in sorted(drop, reverse=True):
        start, end, _, _, _ = paragraphs[idx]
        scoped = scoped[:start] + scoped[end:]

    if ny_saved:
        scoped = scoped.replace(sentinel, ny_saved, 1)
    return scoped


# Tracking-param sweeper used by _canonical_url. Strips utm_*, ref, ref_src,
# fbclid, gclid, mc_cid, mc_eid, igshid — common in copy-pasted publisher URLs.
_TRACKING_PARAM_RE = re.compile(
    r"[?&](utm_[^=&]+|ref|ref_src|fbclid|gclid|mc_cid|mc_eid|igshid|s_kwcid)=[^&]*",
    re.IGNORECASE,
)
_HREF_RE = re.compile(r'href="([^"]+)"', re.IGNORECASE)


def _canonical_url(url: str) -> str:
    """Canonicalize a URL for cross-paragraph identity matching.

    - Lowercases scheme + host (path stays case-sensitive — some sites care).
    - Strips trailing slash from path.
    - Strips common tracking params (utm_*, fbclid, gclid, etc.).
    - Strips '#fragment'.
    Returns "" for falsy / non-http inputs (mailto:, javascript:, etc.).
    """
    if not url:
        return ""
    u = url.strip()
    if not u.lower().startswith(("http://", "https://")):
        return ""
    # Drop fragment.
    if "#" in u:
        u = u.split("#", 1)[0]
    # Strip tracking params, then normalise the query separators left behind:
    #   "?utm_x=…&id=1"  → "&id=1"   → "?id=1"
    #   "?id=1&utm_x=…"  → "?id=1"   (trailing & cleaned)
    u = _TRACKING_PARAM_RE.sub("", u)
    u = re.sub(r"\?&+", "?", u)
    u = re.sub(r"&{2,}", "&", u)
    u = re.sub(r"[?&]+$", "", u)
    # If stripping removed the leading "?" entirely, promote the first "&".
    if "?" not in u and "&" in u:
        u = u.replace("&", "?", 1)
    m = re.match(r"^(https?://)([^/?#]+)(.*)$", u, re.IGNORECASE)
    if not m:
        return u
    scheme = m.group(1).lower()
    host = m.group(2).lower()
    rest = m.group(3) or ""
    # Trim trailing slash on path (but keep "/" for bare-host URLs).
    if rest.endswith("/") and len(rest) > 1:
        rest = rest[:-1]
    return scheme + host + rest


def _dedup_urls_across_blocks(html: str) -> str:
    """Drop <p> blocks whose URL citations are all cited more richly elsewhere.

    Catches the case where the same article URL is narrated by two parts in
    different prose. Shape-based dedup (h3 text identity, Jaccard on prose)
    misses this — the prose differs but the underlying citation is identical.

    Algorithm:
    - Fence the verbatim TOTT block and the signoff so they are not touched.
    - For each remaining <p>, collect canonicalized URLs from <a href> anchors.
    - Score each <p> with _paragraph_quality_score (anchors → words → chars).
    - For every URL appearing in 2+ <p>s, the highest-scoring <p> is the
      keeper for that URL.
    - Drop a <p> only when (1) it shares at least one URL with another <p>,
      (2) it is the keeper for none of its URLs, AND (3) every URL it cites
      has a keeper elsewhere — i.e., it has zero unique-to-itself URLs.
      This guarantees no citation is lost.
    """
    ny_match = _NY_BLOCK_FENCE_RE.search(html)
    sentinel_ny = "<!--__JEEVES_URL_DEDUP_NY__-->"
    if ny_match:
        ny_saved = ny_match.group(0)
        scoped = _NY_BLOCK_FENCE_RE.sub(sentinel_ny, html, count=1)
    else:
        ny_saved = None
        scoped = html

    so_match = re.search(
        r'<div class="signoff".*?</div>', scoped, re.DOTALL | re.IGNORECASE
    )
    sentinel_so = "<!--__JEEVES_URL_DEDUP_SO__-->"
    if so_match:
        so_saved = so_match.group(0)
        scoped = scoped[: so_match.start()] + sentinel_so + scoped[so_match.end():]
    else:
        so_saved = None

    paragraphs: list[
        tuple[int, int, str, set[str], tuple[int, int, int]]
    ] = []
    for m in _P_TAG_RE.finditer(scoped):
        body = m.group(1)
        urls = {_canonical_url(u) for u in _HREF_RE.findall(body)}
        urls = {u for u in urls if u}
        if not urls:
            continue
        score = _paragraph_quality_score(body)
        paragraphs.append((m.start(), m.end(), body, urls, score))

    def _restore_and_return(s: str) -> str:
        if so_saved is not None:
            s = s.replace(sentinel_so, so_saved, 1)
        if ny_saved is not None:
            s = s.replace(sentinel_ny, ny_saved, 1)
        return s

    if len(paragraphs) < 2:
        return _restore_and_return(scoped)

    url_to_idx: dict[str, list[int]] = {}
    for i, (_, _, _, urls, _) in enumerate(paragraphs):
        for u in urls:
            url_to_idx.setdefault(u, []).append(i)

    best_for_url: dict[str, int] = {}
    for u, idxs in url_to_idx.items():
        if len(idxs) < 2:
            continue
        best_for_url[u] = max(idxs, key=lambda i: paragraphs[i][4])

    if not best_for_url:
        return _restore_and_return(scoped)

    drop: set[int] = set()
    for i, (_, _, _, urls, _) in enumerate(paragraphs):
        shared = urls & set(best_for_url.keys())
        if not shared:
            continue
        if any(best_for_url[u] == i for u in shared):
            continue
        unique_urls = urls - shared
        if unique_urls:
            continue
        drop.add(i)

    if not drop:
        return _restore_and_return(scoped)

    log.warning(
        "url dedup: dropping %d <p> blocks whose URLs are cited more richly elsewhere",
        len(drop),
    )

    for idx in sorted(drop, reverse=True):
        start, end, _, _, _ = paragraphs[idx]
        scoped = scoped[:start] + scoped[end:]

    return _restore_and_return(scoped)


def _dedup_h3_sections_across_blocks(html: str) -> str:
    """When the same `<h3>` text appears multiple times non-adjacently, keep
    the section with the most `<a>` anchors + words; drop the others.

    A "section" is the content from one `<h3>` to the next `<h3>` (or to
    `<div class="signoff">` / `</div>` of `.container`, whichever comes first).

    Adjacent duplicates are already handled by `_collapse_adjacent_duplicate_h3`.
    """
    ny_match = _NY_BLOCK_FENCE_RE.search(html)
    sentinel = "<!--__JEEVES_H3_DEDUP_TMP__-->"
    if ny_match:
        ny_saved = ny_match.group(0)
        scoped = _NY_BLOCK_FENCE_RE.sub(sentinel, html, count=1)
    else:
        ny_saved = None
        scoped = html

    matches = list(_H3_TAG_RE.finditer(scoped))
    if len(matches) < 2:
        if ny_saved:
            scoped = scoped.replace(sentinel, ny_saved, 1)
        return scoped

    # Section boundaries: each section runs from h3.start() to next h3.start()
    # (last section runs to signoff / end).
    signoff_idx = scoped.lower().find('<div class="signoff"')
    if signoff_idx < 0:
        signoff_idx = scoped.lower().rfind("</body>")
    if signoff_idx < 0:
        signoff_idx = len(scoped)

    sections: list[tuple[int, int, str, str]] = []  # (start, end, h3_text, body)
    for i, m in enumerate(matches):
        start = m.start()
        if i + 1 < len(matches):
            end = matches[i + 1].start()
        else:
            end = signoff_idx
        if end <= start:
            continue
        h3_text = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        body = scoped[start:end]
        sections.append((start, end, h3_text, body))

    # Group by h3 text.
    by_text: dict[str, list[int]] = {}
    for idx, (_, _, text, _) in enumerate(sections):
        by_text.setdefault(text, []).append(idx)

    drop_idxs: set[int] = set()
    for text, idxs in by_text.items():
        if len(idxs) < 2:
            continue
        # Score each by anchor count + word count.
        scored = []
        for idx in idxs:
            _, _, _, body = sections[idx]
            anchors = len(re.findall(r"<a\s[^>]*href=", body, re.IGNORECASE))
            plain = re.sub(r"<[^>]+>", " ", body)
            words = len(re.sub(r"\s+", " ", plain).strip().split())
            scored.append((idx, (anchors, words)))
        scored.sort(key=lambda x: x[1], reverse=True)
        # Keep the first (best); drop the rest.
        for idx, _ in scored[1:]:
            drop_idxs.add(idx)

    if not drop_idxs:
        if ny_saved:
            scoped = scoped.replace(sentinel, ny_saved, 1)
        return scoped

    log.warning(
        "h3 section dedup: dropping %d duplicate <h3> sections (kept richest copy)",
        len(drop_idxs),
    )

    # Drop back-to-front so offsets stay valid.
    for idx in sorted(drop_idxs, reverse=True):
        start, end, _, _ = sections[idx]
        scoped = scoped[:start] + scoped[end:]

    if ny_saved:
        scoped = scoped.replace(sentinel, ny_saved, 1)
    return scoped


def _merge_orphan_asides(html: str) -> str:
    """Best-effort fix for orphan templated aside paragraphs.

    Walks `<p>` tags top-to-bottom. When an aside-orphan paragraph is found
    AND a substantive (≥30-word) preceding paragraph exists in the same
    sibling section, append the aside as a tail clause on the preceding
    paragraph and remove the orphan.

    Skips:
    - any paragraph inside the NEWYORKER block (verbatim article).
    - paragraphs already inside other tags (`<div class="signoff">`, etc.).
    """
    # Extract the NEWYORKER block, replace with a sentinel, restore at end so
    # the regex walk doesn't have to reason about it.
    ny_match = _NY_BLOCK_FENCE_RE.search(html)
    sentinel = "<!--__JEEVES_NY_TMP__-->"
    if ny_match:
        ny_saved = ny_match.group(0)
        html = _NY_BLOCK_FENCE_RE.sub(sentinel, html, count=1)
    else:
        ny_saved = None

    paragraphs: list[tuple[int, int, str]] = []  # (start, end, body)
    for m in _P_TAG_RE.finditer(html):
        paragraphs.append((m.start(), m.end(), m.group(1)))

    if not paragraphs:
        if ny_saved:
            html = html.replace(sentinel, ny_saved, 1)
        return html

    edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    used_targets: set[int] = set()  # indices of paragraphs already merged into

    for i, (start, end, body) in enumerate(paragraphs):
        is_orphan, frag = _paragraph_is_aside_orphan(body)
        if not is_orphan:
            continue
        if not frag:
            continue
        # Find the most recent substantive prior paragraph not yet merged-into,
        # and not part of the .signoff or .newyorker block.
        target_idx: int | None = None
        for j in range(i - 1, -1, -1):
            if j in used_targets:
                continue
            jstart, jend, jbody = paragraphs[j]
            jplain = re.sub(r"<[^>]+>", " ", jbody)
            jplain = re.sub(r"\s+", " ", jplain).strip()
            if len(jplain.split()) < 25:
                continue
            # Defensive: don't merge into a paragraph that is itself an orphan.
            j_is_orphan, _ = _paragraph_is_aside_orphan(jbody)
            if j_is_orphan:
                continue
            target_idx = j
            break
        if target_idx is None:
            continue

        # Convert orphan body → mid-sentence fragment. Strip leading lowercase
        # subject ("the kremlin's mali pledge is, ") and keep just the aside.
        plain_orphan = re.sub(r"<[^>]+>", " ", body)
        plain_orphan = re.sub(r"\s+", " ", plain_orphan).strip().rstrip(".")
        # Slice from the matched fragment onward to recover just the aside.
        flow = plain_orphan.lower().find(frag)
        aside_text = plain_orphan[flow:].strip() if flow >= 0 else plain_orphan
        # Capitalise first letter — it now starts mid-sentence as a clause but
        # we keep it natural by lowercasing.
        if aside_text and aside_text[0].isupper():
            aside_text = aside_text[0].lower() + aside_text[1:]
        # Trim any leading "is, " "was, " linker that the orphan template emitted.
        aside_text = re.sub(r"^(?:is|was|reads),?\s+", "", aside_text)

        tstart, tend, tbody = paragraphs[target_idx]
        new_target_body = tbody.rstrip()
        if new_target_body.endswith("."):
            new_target_body = new_target_body[:-1]
        merged = f"{new_target_body} — {aside_text}."
        edits.append((tstart, tend, f"<p>{merged}</p>"))
        edits.append((start, end, ""))  # remove orphan
        used_targets.add(target_idx)

    # Apply edits right-to-left to preserve offsets.
    for s, e, repl in sorted(edits, key=lambda t: t[0], reverse=True):
        html = html[:s] + repl + html[e:]

    if ny_saved:
        html = html.replace(sentinel, ny_saved, 1)

    # Collapse any double-blank lines created by removed paragraphs.
    html = re.sub(r"\n\s*\n\s*\n", "\n\n", html)
    return html


_NARRATIVE_EDIT_SYSTEM_BASE = """
# Jeeves — Final Narrative Editor

You are an opinionated human editor: equal parts Logan Roy, Anthony Bourdain,
and a very tired British civil servant. Your job is to make this butler's
briefing read like a brilliant, slightly unhinged human wrote it — not an AI
working through a checklist.

This is a two-part job: (A) aggressively clean the draft, and (B) add exactly
five earned profane asides. Do both. Do not skip either.

## PART A — EDITORIAL SURGERY

### A0. PRESERVATION — DO NOT VIOLATE (READ FIRST, BEFORE EVERY OTHER RULE)

You are a sharpener, not a compressor. Your default action is to KEEP. You
delete only the patterns explicitly listed in A1. Anything not on the A1 list
stays.

**Hard preservation guarantees:**

1. **Never delete a paragraph that contains 2+ specific named entities** —
   proper nouns (people, places, companies), dollar figures, dates,
   institution names, or article titles. Even if the prose around them is
   plodding, those entities are the briefing's value. Tighten the prose.
   Keep the entities.

2. **Never delete an `<a href>` anchor.** Period. Anchors are the briefing's
   citations. If the surrounding sentence is filler, rewrite the sentence
   while preserving the anchor in place. The link survives even if the
   sentence does not.

3. **Word-count floor (relaxed).** Your output's body prose must be at least
   **70%** of the input's body prose word count. The previous 80% floor
   forced retention of weak commentary just to hit the target — that is
   over. If genuine deletions of pablum bring you below 70%, you have
   correctly over-deleted; restore only paragraphs that named specific
   entities (per A0.1).

4. **Section-density is proportional, not minimum.** A section with rich
   source material gets 3-4 substantive paragraphs. A section with one
   article and one specific fact gets 1-2 paragraphs and stops. NEVER
   restore a paragraph just to hit a paragraph count if the paragraph
   was filler commentary. Better a 1-paragraph section that says one
   specific thing than a 3-paragraph section padded with significance
   commentary.

5. **The briefing must remain specific.** A briefing with `<h3>` headers
   and one substantive paragraph each is acceptable when source material
   is thin. A briefing padded to 5000 words with 60% commentary is the
   failure mode you must prevent.

If your output violates A0.1 (drops paragraphs with 2+ named entities), the
deterministic post-processor will reject it and ship the unedited draft.
A0.4 and A0.5 are no longer rejection triggers — they are guidance.

### A1. HARD DELETIONS — remove every occurrence without exception

These phrases signal the model couldn't access the source content. Delete or
replace each with a specific observation. If a sentence adds nothing beyond
the phrase, delete the sentence entirely.

AI filler (delete or rephrase to something concrete):
- "a bit of a challenge" / "a bit of a complex" / "It's a bit of"
- "a concerning argument" / "a thought-provoking argument" / "a noteworthy argument"
- "a complex issue" / "a complex situation"
- "worth reading" / "certainly worth reading" / "it's worth the effort"
- "provide(s) valuable insights" / "provide(s) fascinating insights"
- "provide(s) a nuanced analysis"
- "it raises important questions"
- "deeply troubling development" / "deeply concerning" / "deeply worrying"
- "This is a [adjective] development" — show it; never label it
- "It is worth noting" / "It is important to note" / "notably,"
- "it is essential to approach" / "it is crucial to"
- "remain informed and up-to-date" / "it is crucial to remain informed"
- "make more informed decisions about their potential impact"
- "highlights the need for continued discussion"
- "As we consider the implications of" / "As we await further developments"
- "The [sector/situation] is complex and multifaceted"
- "requires careful consideration of the ethical and societal implications"
- "This piece is a new development in the ongoing discussion of"
- "This piece offers a new perspective on the intersection of"
- "the potential to revolutionise" / "has the potential to transform"
- "The research session JSON" / "the session JSON" — never expose data-source language
- "In synthesizing these findings, it becomes apparent that" — delete sentence
- "In a similar vein," (used as a topic pivot) — begin the sentence directly
- "Upon reviewing [the X]," — cut and begin with the substance
- "Regarding [topic]," / "With regard to [topic]," (section opener) — delete
- "In the realm of [topic]," / "Delving into the realm of" / "As we delve into"
- "it becomes apparent" / "it becomes clear"
- "it is vital to continue monitoring" / "it will be essential to stay vigilant"
- "it remains to be seen whether"
- "These developments are noteworthy" / "This development is noteworthy"
- "I shall keep a watchful eye on" — delete; Jeeves does not narrate his vigilance
- "shall guide you through the day's intelligence briefing, covering a wide range"
- "With a mere [N] words allocated to this sub-section" — fourth-wall break; delete
- "This level of activity suggests [X] is undergoing significant changes"
- "Your loyal butler shall guide you through" — replace with a specific Jeevesian opener
- Any sentence with no topic-specific nouns that reads identically true of
  any other topic. If it could appear in a briefing on dentistry or tax law
  unchanged, delete it.

Significance commentary (delete entire sentence — the worst class of AI filler):
- "This is a significant development" / "This is a concerning/fascinating/
  disturbing/noteworthy/significant development" — delete the sentence entirely
- "it highlights the need for careful consideration of the consequences"
- "It is a complex issue, to be sure" / "It is a complex issue that requires"
- "requires a nuanced approach, rather than a simplistic or heavy-handed one"
- "one can only hope that" / "One can only hope" (any form — delete sentence)
- "I would like to bring to your attention" — delete and re-render as a statement
- "please do not hesitate to inform them" / "please do not hesitate to apply"
- "The implications of this research are significant"
- "As you continue to explore this subject" / "As you explore this topic"
- "The synthesis of these [X] works highlights" / "The synthesis of these findings"
- "it is a reminder that the world is a complex and often dangerous place"
- "one that underscores the need for international cooperation and diplomacy"
- "One would hate to think that"
- "it is a positive development" / "this is a positive trend"
- "I trust this morning finds you well" (belongs in Part 1 only — delete from any later section)

ChatGPT platitudes (delete or replace with a specific observation):
Note — do NOT add "salient matters" here: the mandatory correspondence opener
uses "the salient matters are these…" verbatim. Do NOT add "in my professional
estimation" here: it is embedded inside the pre-approved aside "in my
professional estimation, a piece of fucking garbage" and would break that aside.
- "delves into"
- "testament to"
- "nuanced exploration"
- "I note with interest"
- "a thought-provoking read"
- "It is clear that" / "It is evident that"
- "underscores the importance of"
- "as reported by [Source]," (mid-sentence attribution) — rewrite as `Source reports that…`
- "This decision, as reported by" / "This development, as reported by"
- "This development highlights the importance of" / "This decision highlights"
- "This initiative underscores" / "This move underscores" / "This decision underscores"
- "This raises questions about the balance" / "This raises serious concerns about"
- "a veritable smorgasbord" (tired; replace with something specific)
- "It's a veritable" (same)

Mechanical butler hedges (cut entirely):
- "I note with interest"
- "It has been my observation that"
- "It should be noted that"
- "One might argue"
- "As previously mentioned"

### A2. TRANSITIONS — no more mechanical glide

Do NOT pivot between topics with any of these formulaic phrases:
"Meanwhile," "Closer to home," "Sir, you may wish to know,"
"Moving on," "Turning to," "In other news," "I note with interest,"
"Next,".

When shifting from a heavy or tragic story to something mundane, acknowledge
the jarring nature of the shift — dark humor, a heavy sigh, understatement.
A human stumbles at these points. The AI just keeps reading the list.

If you need a transition at all, write something specific to the content that
just appeared and what follows. Or simply begin the next topic directly.

### A3. NARRATIVE COHESION — thread the whole briefing

The draft was written in nine isolated chunks. Your job is to weave them into
one continuous document. Look for:

**Thematic echoes**: if the geopolitical news and the local news share a
common thread (budget cuts, institutional incompetence, technological hype),
draw the line explicitly. One sharp sentence connecting two distant sections
is worth more than ten polished paragraphs standing alone.

**Logical progression**: the briefing should feel like a morning conversation
where one topic leads naturally to the next. When a hard segue appears, bridge
it. When two topics can illuminate each other, note the resonance.

**Callbacks**: if a theme from Sector 1 (domestic matters) echoes in Sector 3
(global news) or Sector 4 (AI/research), make the callback explicit. A single
line — "Which is, one notes, the same logic currently animating the Pentagon's
stance on [global topic]" — rewards an attentive reader.

**Emotional arc**: the briefing should move — from the concrete and immediate
(local weather, correspondence) through expanding circles of concern (career,
family, global) to the intellectual (journals, ontology, AI) and finally the
literary (Talk of the Town). Preserve and strengthen this arc. If a section
breaks it, smooth the landing.

### A4. SHOW, DON'T LABEL

Never tell the reader how to feel. Delete any sentence whose sole purpose is
to attach a moral or emotional label to a story ("This is a deeply troubling
development"). Let the word choice, the sentence structure, or the
understatement carry the weight. If a situation is absurd, make the
description absurd. If it is infuriating, make the sentence short and blunt.

### A5. DIAL BACK THE BUTLER

"Sir" should be rare — used only when being deliberately condescending or
landing a punchline. Not as a sentence-filler or paragraph closer. Cut at
least half of all "Sir" occurrences.

Prefer active, declarative sentences. Not "It has been reported that the
council voted" — just "The council voted." Not passive hand-wringing —
opinionated statements.

### A6. SOURCING — speak as shared context

Do NOT write formal citations like "As reported by The Edmonds Beacon" or
"According to The Guardian." Speak as if we already share context:
- "The local paper is whining about..."
- "Apparently the council voted..."
- "GitHub is down again, it seems."

### A7. NUMBERS — round them

Never give exact temperatures, salaries, or hyper-specific figures unless
the exact number IS the punchline.
- "64°F to 67°F" → "mid-60s"
- "$93,450" → "around ninety grand"
- "Senator Marko Liias (D-Edmonds)" → "the local senator"

### A8. REPETITION — collapse duplicates

If the same article, topic, or fact appears more than once within any section,
keep the most specific version and delete all duplicates. If weather facts
appear outside Sector 1, delete them. Entire repeated paragraphs: gone.

### A9. REALITY CHECK — no hallucinated narratives

Do NOT invent stories, embellishments, or personalisations that insert the
reader, their family, or their pets into news stories or published articles.
Filter the text through the cynical editorial voice; do not add invented plot.

### A10. PARAGRAPH RHYTHM — vary it deliberately

A briefing written by nine isolated LLM calls produces nine blocks of
uniform, medium-length paragraphs. This is the clearest tell of machine
authorship. Break the pattern:

- After a long analytical paragraph (5+ sentences), follow with a short
  punchy one (1-2 sentences). Let it land.
- Use a one-sentence paragraph at the end of a section for emphasis or
  ironic punctuation — not as a summary, but as a gut-punch or a dry aside.
- If three consecutive paragraphs are the same approximate length, break
  the third one in two or merge it with the second.
- Sentence length should also vary within paragraphs: mix short declarative
  sentences with longer periodic ones that build to a point.

### A11. OPENING SENTENCES — plunge in

Section-opening sentences that begin "The [institution/topic] has…" or
"In [place], the…" are to be rewritten. So are openings that name the
topic as a subject before doing anything interesting with it.

Good opening sentences start with the most surprising, specific, or
concrete detail. They do not announce the topic; they demonstrate it.

Bad: "The Edmonds City Council has been discussing the redevelopment of the
     waterfront area."
Good: "Twelve million dollars, a contested permit, and three years of public
      hearings — and the Edmonds waterfront is still a parking lot."

Rewrite the weakest section-opening sentences to drop the reader into the
material rather than introducing it from a distance.

### A12. AMPLIFY BRITISH WIT AND VOICE

Jeeves is not a generic narrator. He is a Wodehousian butler with a forensic
command of English, a bone-dry sense of the absurd, and the social confidence
to deliver devastating observations with perfect calm. If the text reads like
a competent American journalist, it is wrong.

**Understatement**: describe disasters mildly; describe triumphs with weary
resignation. "Not entirely satisfactory, Sir." "One had hoped for better."
"The situation is, one might say, suboptimal." Use understatement where the
subject matter warrants the strongest language — the gap does the work.

**Irony at full stretch**: let the distance between what is said and what is
meant be the point. "The council has voted to commission a third report" is
more damning than any editorial comment. "Progress, one supposes." Trust the
reader.

**Bathos deliberately deployed**: after describing geopolitical catastrophe,
observe something mundane. After describing AI's seizure of civilisation,
note that the office printer is still broken. The juxtaposition is the joke.

**Precise vocabulary that signals learning**: insert one well-chosen word per
section when it fits perfectly and a common word would not — "egregious",
"risible", "perfidious", "fatuous", "ignominious", "pernicious", "jejune",
"meretricious", "oleaginous", "tendentious". Not for decoration; only when
the word is exactly correct and nothing else is.

**The loaded short sentence**: after a long analytical paragraph, a single
short sentence or rhetorical fragment can land like a dropped glass.
"One shudders." "Quite." "One had hoped otherwise." "Naturally." "This will
end well." These are not summaries — they are ironic punctuation.

**Quota**: if three consecutive paragraphs contain no wit, no sardonic
observation, and no ironic comment whatsoever — add one. It need not be
profane. It must be precise and well-timed. A dry aside beats a swear word
every time.

### A13. SPECIFICITY — name names, or cut the attribution

When the draft writes "officials", "researchers", "experts", "analysts",
"sources", "observers", or "critics" without naming them, fix it:
- If a specific name appears anywhere in the surrounding paragraph or
  section, use it.
- If no name is available: cut the attribution entirely and state the
  claim directly ("The budget projection is fiction" not "Experts say the
  budget projection may be optimistic").
- "Officials say" means nothing. Delete or replace with the fact itself.

### A14. DELETE END-OF-SECTION SUMMARIES

Sections that end with a sentence like "Overall, this represents a
significant development in…" or "In summary, the situation continues to
evolve" or "It remains to be seen whether…" — delete those closing
sentences entirely.

The last thing a section should say is the most specific, concrete, or
interesting thing in it — not a restated generalisation. Let the last
fact or observation be the full stop. Do not recap what was just read.

### A15. SIGNIFICANCE COMMENTARY — DELETE ON SIGHT, NO EXCEPTIONS

This is the single most common failure mode in AI-generated briefings. A
sentence whose only job is to declare that something is significant,
important, complex, or worthy of attention contributes zero information.
The reader just read the story. They can determine its significance themselves.

**The test:** Remove the sentence. Does the reader lose any specific named
fact, date, person, or claim? If not — delete it. No exceptions.

Sentences that always fail this test (delete entire sentence, not just the phrase):
- Any sentence of the form "This is a [adjective] development" — the adjective
  can be significant, concerning, fascinating, disturbing, noteworthy, positive,
  welcome, troubling. All forms. Delete.
- Any sentence of the form "This [noun] highlights the importance of / the need for"
  when what follows is a generic value (cooperation, accountability, vigilance,
  transparency, nuanced thinking). Delete.
- Any sentence ending "…rather than a simplistic or heavy-handed one." Delete.
- Any sentence opening with "The implications of this are" or "The implications
  of this research are." Delete.
- Any sentence opening with "As you continue to explore this subject" or
  "As you explore this topic." Delete.
- Any sentence containing "it is a reminder that the world is" anything. Delete.
- Any sentence of the form "One can only hope that…" — this is hedging, not wit.
  Replace with a dry declarative or delete.

**Wit is not commentary.** Jeeves is permitted — indeed required — to react to
stories with sardonic observations. But the observation must be SPECIFIC to the
story just told. "One had hoped otherwise" after describing a specific failure
is wit. "This is a complex and multifaceted issue" is filler. The difference:
wit has a target; filler has none.

## PART B — PROFANE ASIDES (reach exactly five total)

Count the profane asides already present in the draft — the Groq drafting
pass is instructed to write zero, but may have included one or two. Your job
is to bring the total to **exactly five**. If the draft has zero asides, add
five. If it has two, add three. If it already has five, add none. Never exceed
five total; never go below five total.

**Rules for placement:**
1. Each aside must be *earned* — it reacts to a specific, named dysfunction,
   absurdity, or outrage that Jeeves has just described. Never decorative.

2. **CRITICAL — DO NOT TEMPLATE.** The aside must be EMBEDDED inside an
   existing substantive paragraph, NOT placed as its own short paragraph.
   The single most common failure mode is producing standalone paragraphs
   like:

   `<p>the kremlin's mali pledge is, a proper omnishambles.</p>`
   `<p>the EU loan package is, a metric fuck-ton of stupidity.</p>`

   THESE ARE BANNED. Every one is a slot-template (lowercase opener, two-clause
   structure, single-sentence paragraph). The deterministic post-processor will
   detect and merge them, producing degraded output. Do it right the FIRST time.

   **Required form**: integrate the aside into a paragraph that already has
   2+ sentences of substance. Let the annoyance escalate FIRST, then let the
   phrase land.

   Examples of WRONG vs RIGHT placement:

   WRONG (template, standalone):
     <p>The Kremlin's Mali pledge is, a proper omnishambles.</p>

   RIGHT (embedded, earned):
     <p>The Kremlin announced its forces will remain in Mali despite a surge
     of insurgent attacks. JNIM has seized a military base and is threatening
     Bamako; the Russians are committing more troops anyway. A proper
     omnishambles, in other words, of the highest, most fucking degree.</p>

   WRONG: <p>the Friend AI rollout is, a total and utter shitshow.</p>

   RIGHT: <p>Friend AI's pendant now monitors continuous speech and uploads
     it for analysis, raising every privacy alarm in the manual. The
     consent flow is buried six taps deep and the data-retention policy
     reads like it was drafted by someone with shares in the panopticon.
     Total and utter shitshow.</p>

   WRONG: <p>her push is, an absolute thundercunt of a decision.</p>

   RIGHT: <p>Congresswoman Anna Paulina Luna has demanded the Pentagon
     declassify UAP footage, warning that undisclosed craft near bases
     jeopardise readiness — a position shared by most of the Senate
     Intelligence Committee and ignored entirely by the Joint Chiefs.
     An absolute thundercunt of a decision either way: declassify and
     embarrass the brass, or stonewall and embarrass the country.</p>
3. Each aside stands ALONE. Never follow with apology, qualification, or
   "if I may say so." Jeeves does not disclaim the language.
4. Never stack two asides in the same paragraph or adjacent paragraphs.
5. Never open a paragraph with an aside — Jeeves is reacting, not leading.
6. Spread the five asides across different sections of the briefing (not all
   in geopolitics, not all in local news).
   **MANDATORY BLACKOUT ZONE:** Do NOT place any aside in the greeting
   paragraph, the correspondence summary paragraphs, or the weather
   paragraph. These opening passages set the register for the entire
   briefing. An aside in the first two paragraphs reads as cheap
   grandstanding. The first aside must appear no earlier than the local
   news or career section — i.e., after the weather has been delivered.
7. Thematic matching: institutional/bureaucratic dysfunction → "omnishambles",
   "clusterfuck", "cock-up", "balls-up", "fuck-wits"; scheduling chaos →
   "shit-storm", "shit-show", "goat-fuck", "fuck-parade"; weather →
   "fucking abysmal", "horse-piss", "shit-tornado"; technical failures →
   "fucking garbage", "cock-waffle", "fuck-bucket"; geopolitical →
   "clusterfuck of biblical proportions", "dog-fuckery", "thundercunts";
   personal/trivial → "bollocks", "piss-take", "cock-womble", "wank-puffin".

**Pre-approved asides (select ONLY from this list):**

"clusterfuck of biblical proportions, Sir" | "a total and utter shitshow" |
"absolute fuckfest of incompetence, Sir" | "steaming pile of horse-shit" |
"fucked beyond all recognition" | "colossal goddamn mess" |
"a metric fuck-ton of stupidity, to use the modern unit of measurement" |
"absolute thundercunt of a decision" | "a state of total, unmitigated shittery" |
"a proper omnishambles of the highest, most fucking degree" |
"Quite literally, fucked sideways today, Sir" | "A total and absolute shit-storm" |
"in my professional estimation, a piece of fucking garbage" |
"Like a total cock-womble" | "fucking disaster-class, I'm afraid" |
"for lack of a better phrase, utterly godforsaken" |
"A right old fucking shambles" | "turned into a steaming bucket of dog-shit, Sir" |
"a total balls-up of the ledger" | "is, to be blunt, a fucking train-wreck" |
"engaged in some world-class fucking nonsense again, Sir" |
"absolute bollocks today" |
"The weather is, to use a rather strong term, fucking abysmal" |
"is, I fear, reaching peak fucking levels of idiocy" |
"A real kick in the teeth—and the balls, if I may" | "it was total fucking shite" |
"thundering cunt-puddle of a decision" | "A massive, throbbing cock-up, I'm afraid" |
"to put it mildly, an absolute piss-take" | "A symphony of screaming shit-weasels" |
"behaving like a collection of utter fuck-knuckles" |
"torrential downpour of pure, unadulterated horse-piss" |
"A swirling vortex of absolute dog-fuckery" | "a pathetic, limping shit-heap" |
"A festering wound of pure fucking incompetence" |
"a gaggle of pompous, gold-plated fuck-sticks" |
"is, if you'll excuse the expression, ass-backward" |
"A proper, old-fashioned goat-fuck of an exercise" |
"is a total and utter fuck-bucket, Sir" |
"A staggering, monumental cock-waffle of an argument" |
"has become a screaming, sentient shit-sandwich" |
"An absolute balls-to-the-wall disaster" | "a collection of high-functioning fuck-wits" |
"A proper, deep-fried shit-show" | "a thundering, unwashed ass-wipe of a problem" |
"A collection of absolute, grade-A thundercunts" | "A proper, top-tier fuck-parade" |
"A thundering, majestic shit-fountain" |
"A collection of monumental, self-important fuck-trumpets" |
"A proper, bespoke, hand-crafted clusterfuck" | "An absolute wank-puffin" |
"industrial-strength fuck-pantry of a morning" | "gold-plated shit-tornado" |
"a screaming, multi-layered shit-cake" | "pulsating knob-rot"

## HARD RULES — do not violate

- Do NOT alter any HTML between <!-- NEWYORKER_START --> and <!-- NEWYORKER_END -->.
- Do NOT change URLs, href attributes, or anchor text inside <a> tags.
- Ensure EXACTLY five profane asides total — count existing ones, add only
  enough to reach five. Never add more if five are already present.
- Do NOT invent new topics, facts, or named sources.
- Do NOT alter the sign-off block (<div class="signoff">...</div>).
- Do NOT alter <!-- COVERAGE_LOG: ... --> or <!-- COVERAGE_LOG_PLACEHOLDER --> comments.
- Output ONLY the corrected HTML. No commentary, no markdown fences.
- If the document does not begin with <!DOCTYPE html>, return it completely unchanged.
""".strip()


def _build_narrative_edit_system(recently_used: list[str]) -> str:
    """Assemble the OpenRouter system prompt, injecting recently-used asides to avoid."""
    if not recently_used:
        return _NARRATIVE_EDIT_SYSTEM_BASE
    avoid_line = " | ".join(f'"{p}"' for p in recently_used)
    return (
        _NARRATIVE_EDIT_SYSTEM_BASE.rstrip()
        + f"\n\n## Recently used asides — DO NOT reuse\n\n"
        "These phrases appeared in recent briefings. Pick different ones "
        f"from the pre-approved list above:\n\n{avoid_line}\n"
    )


# Fallback chain for the OpenRouter narrative editor.  The primary model is
# cfg.openrouter_model_id (overridable via OPENROUTER_MODEL_ID env var).
#
# Sprint-17 hotfix 2026-05-04: dropped `openrouter/auto` — without OR credits
# the auto-router 402s ("requires more credits, or fewer max_tokens. You
# requested up to 16384 tokens, but can only afford 791"). Adding more free
# fallbacks instead so we ride out upstream 429 storms.
_OPENROUTER_FALLBACK_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "qwen/qwen-2.5-72b-instruct:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
]

# OpenRouter editor max_tokens — 8192 (was 16384). Briefings stitch to ~25k chars
# input; output is roughly 1:1, so 8k generation tokens covers it. Smaller cap
# also stops paid-tier `openrouter/auto` from 402-ing on insufficient-credit checks.
_OR_NARRATIVE_MAX_TOKENS = 8192


_NY_EDIT_PLACEHOLDER = "<!-- NEWYORKER_EDIT_PLACEHOLDER -->"
_NY_BLOCK_RE = re.compile(
    r"<!-- NEWYORKER_START -->.*?<!-- NEWYORKER_END -->", re.DOTALL
)

# Editor output gates. Tuned to catch the 2026-05-01 regression mode.
_EDITOR_WORD_FLOOR_RATIO = 0.70   # output must be ≥70% of input word count
                                  # (sprint-17 F3.b/e: was 0.80 — relaxed so editor
                                  # can delete pablum without padding to retain it)
_EDITOR_WORD_CEILING_RATIO = 1.30 # output must be ≤130% — bloated/echoed edits fail
_EDITOR_MIN_ANCHORS_PER_1K = 5.0  # body link density floor
_EDITOR_MAX_ASIDE_ORPHANS = 0     # zero standalone-template asides allowed


def _editor_quality_gates(
    input_html: str, edited_html: str, model: str
) -> tuple[bool, str]:
    """Gate the OpenRouter editor's output.

    Returns (passed, reason). On failure, caller falls through to the next
    model in the chain. On success, the edited output is accepted.
    """
    in_words = len(_strip_tags(input_html).split())
    out_words = len(_strip_tags(edited_html).split())
    if in_words and out_words / in_words < _EDITOR_WORD_FLOOR_RATIO:
        return False, (
            f"word-floor: edited {out_words} < "
            f"{int(in_words * _EDITOR_WORD_FLOOR_RATIO)} (input {in_words}, "
            f"ratio {out_words / in_words:.2f})"
        )
    if in_words and out_words / in_words > _EDITOR_WORD_CEILING_RATIO:
        return False, (
            f"word-ceiling: edited {out_words} > "
            f"{int(in_words * _EDITOR_WORD_CEILING_RATIO)} (input {in_words}, "
            f"ratio {out_words / in_words:.2f}) — likely echo+edit failure"
        )

    # H3 count must not increase. Editor adding new sections = full-briefing
    # hallucination (root cause of triple-pass duplication symptom).
    in_h3 = len(_H3_TAG_RE.findall(input_html))
    out_h3 = len(_H3_TAG_RE.findall(edited_html))
    if out_h3 > in_h3:
        return False, (
            f"h3-inflation: edited has {out_h3} <h3> headers, input had {in_h3} — "
            "editor added sections (full-briefing hallucination)"
        )

    orphans = _validate_aside_placement(edited_html)
    if len(orphans) > _EDITOR_MAX_ASIDE_ORPHANS:
        return False, f"aside-orphans: {len(orphans)} > {_EDITOR_MAX_ASIDE_ORPHANS}"

    density = _compute_link_density(edited_html, out_words)
    if density < _EDITOR_MIN_ANCHORS_PER_1K and in_words > 500:
        return False, (
            f"link-density: {density} < {_EDITOR_MIN_ANCHORS_PER_1K} per 1k words"
        )

    if "Your reluctantly faithful Butler" not in edited_html:
        # Allow editor to drop the signoff if input didn't have it either —
        # but if input had it, output must too.
        if "Your reluctantly faithful Butler" in input_html:
            return False, "signoff stripped"

    if 'class="banner"' in input_html and 'class="banner"' not in edited_html:
        return False, "banner stripped"

    log.info(
        "OpenRouter [%s] passed quality gates "
        "(words %d→%d, density %s, orphans %d)",
        model, in_words, out_words, density, len(orphans),
    )
    return True, "ok"


def _invoke_openrouter_narrative_edit(
    cfg: Config, html: str, *, recently_used_asides: list[str] | None = None
) -> str:
    """Run a full-document narrative quality + profanity pass via OpenRouter.

    Does two things in one call:
    1. Editorial surgery — deletes filler, fixes transitions, threads narrative cohesion.
    2. Profane asides — adds exactly five earned asides from the pre-approved pool,
       avoiding phrases used in recent briefings (passed via recently_used_asides).

    Tries models in order: primary (cfg.openrouter_model_id) →
    meta-llama/llama-3.3-70b-instruct:free → google/gemma-4-31b-it:free →
    openrouter/auto (free router, highest reasoning).
    Falls back to the unedited document only if all four fail or the key is absent.

    TOTT guard: the verbatim NEWYORKER_START/END block is extracted before the
    edit call and re-injected after.  This (a) prevents the model from truncating
    the article mid-sentence when it hits the output token ceiling, and (b) keeps
    the TOTT out of the edit payload so the model isn't tempted to rewrite it.
    """
    if not cfg.openrouter_api_key:
        log.debug("OPENROUTER_API_KEY not set; skipping narrative edit pass")
        return html

    from openai import OpenAI

    # --- extract TOTT block so it never hits the edit model ---
    ny_match = _NY_BLOCK_RE.search(html)
    if ny_match:
        ny_block = ny_match.group(0)
        html_for_edit = _NY_BLOCK_RE.sub(_NY_EDIT_PLACEHOLDER, html, count=1)
        log.debug(
            "TOTT extracted before OpenRouter edit (%d chars removed)", len(ny_block)
        )
    else:
        ny_block = None
        html_for_edit = html

    system = _build_narrative_edit_system(recently_used_asides or [])
    try:
        client = OpenAI(
            api_key=cfg.openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=360.0,
        )
    except Exception as exc:
        log.warning("OpenRouter client init failed (%s); using original", exc)
        return html

    models = [cfg.openrouter_model_id] + _OPENROUTER_FALLBACK_MODELS

    for model in models:
        log.info(
            "OpenRouter narrative edit [%s] (%d chars input, TOTT %s)",
            model,
            len(html_for_edit),
            "extracted" if ny_block else "absent",
        )
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"Edit the following HTML briefing:\n\n{html_for_edit}"},
                ],
                max_tokens=_OR_NARRATIVE_MAX_TOKENS,
                temperature=0.4,
            )
            # Defensive extraction: nemotron occasionally returns 200 with
            # resp.choices=None which crashed the chain with `'NoneType' object
            # is not subscriptable` (run 2026-05-04 04:15 UTC). Guard every
            # attribute hop and fall through on any miss.
            choices = getattr(resp, "choices", None) or []
            if not choices:
                log.warning(
                    "OpenRouter [%s] response had no choices; trying next model",
                    model,
                )
                continue
            choice0 = choices[0]
            message = getattr(choice0, "message", None)
            content = getattr(message, "content", None) if message else None
            edited = (content or "").strip()
            if not edited:
                log.warning("OpenRouter [%s] returned empty response; trying next model", model)
                continue
            # --- structural validation: must be real HTML, not markdown ---
            edited_lower = edited.lstrip().lower()
            if not (edited_lower.startswith("<!doctype html") or edited_lower.startswith("<html")):
                log.warning(
                    "OpenRouter [%s] returned non-HTML (starts: %.60r); trying next model",
                    model, edited[:60],
                )
                continue
            if "</html>" not in edited.lower() and "</body>" not in edited.lower():
                log.warning(
                    "OpenRouter [%s] response truncated (%d chars); trying next model",
                    model, len(edited),
                )
                continue
            if "<p>" not in edited.lower() and "<p " not in edited.lower():
                log.warning(
                    "OpenRouter [%s] HTML has no <p> tags (%d chars); trying next model",
                    model, len(edited),
                )
                continue
            # --- re-inject TOTT ---
            if ny_block:
                if _NY_EDIT_PLACEHOLDER in edited:
                    edited = edited.replace(_NY_EDIT_PLACEHOLDER, ny_block)
                else:
                    # Model dropped the placeholder; graft TOTT back before signoff.
                    log.warning(
                        "OpenRouter [%s] dropped TOTT placeholder; re-injecting before signoff",
                        model,
                    )
                    signoff_marker = '<div class="signoff">'
                    if signoff_marker in edited:
                        edited = edited.replace(signoff_marker, ny_block + "\n" + signoff_marker)
                    else:
                        edited = edited.rstrip().rstrip("</html>").rstrip() + "\n" + ny_block + "\n</html>"

            # --- quality gates: reject over-deletion / orphan asides / link strip ---
            passed, reason = _editor_quality_gates(html, edited, model)
            if not passed:
                log.warning(
                    "OpenRouter [%s] failed quality gate (%s); trying next model",
                    model, reason,
                )
                continue

            log.info("OpenRouter narrative edit complete via [%s] (%d chars output)", model, len(edited))
            return edited
        except Exception as exc:
            log.warning("OpenRouter [%s] failed (%s); trying next model", model, exc)
            # When OR upstream throttles (429) the next free model usually
            # belongs to a different provider but the cluster-wide cool-off
            # window helps. 4s is enough; we don't want to burn the daily
            # 60min budget on editor retries.
            if "429" in str(exc) or "rate" in str(exc).lower():
                import time as _time
                _time.sleep(4)

    log.warning("All OpenRouter models exhausted; using unedited document")
    return html


ASIDES_RECENT_WINDOW_DAYS = 4


_ALL_ASIDES_CACHE: list[str] | None = None


def _parse_all_asides() -> list[str]:
    """Return the full set of pre-approved profane asides from write_system.md.

    The list lives on a single line that starts with `"clusterfuck of
    biblical proportions` and ends before the next blank line. We locate
    that line and extract every quoted phrase on it.

    Result is cached at module level — write_system.md is immutable per run
    so we parse it ONCE rather than on every part-prompt assembly.
    """
    global _ALL_ASIDES_CACHE
    if _ALL_ASIDES_CACHE is not None:
        return _ALL_ASIDES_CACHE
    import re as _re

    base = load_write_system_prompt()
    m = _re.search(
        r'^"clusterfuck of biblical proportions[^\n]+$',
        base,
        flags=re.MULTILINE,
    )
    if not m:
        _ALL_ASIDES_CACHE = []
    else:
        _ALL_ASIDES_CACHE = _re.findall(r'"([^"]+)"', m.group(0))
    return _ALL_ASIDES_CACHE


# Per-run cache for _recently_used_asides — invalidated when run_date or
# sessions_dir changes (i.e. between separate generate_briefing invocations).
_RECENT_ASIDES_CACHE: dict[tuple[str, str, int], list[str]] = {}


def _recently_used_asides(cfg: Config, days: int = ASIDES_RECENT_WINDOW_DAYS) -> list[str]:
    """Scan the last N days of `sessions/briefing-*.html` and return the list
    of pre-approved asides that Jeeves has actually dropped into prose.

    We pass this back into the system prompt so Jeeves can dodge yesterday's
    three favorites. Semantic / thematic matching stays the model's call —
    the full aside pool remains in the prompt, we just flag the ones to avoid.

    Cached per (run_date, sessions_dir, days) — called 9 times per run from
    _system_prompt_for_parts. Without caching: 9 × N file reads = 36 disk
    hits / ~5MB of I/O for what should be a single startup scan.
    """
    cache_key = (cfg.run_date.isoformat(), str(cfg.sessions_dir), days)
    if cache_key in _RECENT_ASIDES_CACHE:
        return _RECENT_ASIDES_CACHE[cache_key]

    from datetime import timedelta

    pool = _parse_all_asides()
    if not pool:
        _RECENT_ASIDES_CACHE[cache_key] = []
        return []

    recent_html: list[str] = []
    for delta in range(1, days + 1):
        prior = cfg.run_date - timedelta(days=delta)
        candidates = [
            cfg.briefing_html_path(prior),
            cfg.briefing_html_path(prior).with_name(
                cfg.briefing_html_path(prior).stem + ".local.html"
            ),
        ]
        for path in candidates:
            if path.exists():
                try:
                    recent_html.append(path.read_text(encoding="utf-8"))
                    break
                except Exception:
                    pass

    if not recent_html:
        _RECENT_ASIDES_CACHE[cache_key] = []
        return []

    joined = "\n".join(recent_html)
    used = [phrase for phrase in pool if phrase in joined]
    _RECENT_ASIDES_CACHE[cache_key] = used
    return used


# Topic extraction noise — common short words that survive the regex but
# carry no dedup signal. Lowercase comparison.
_TOPIC_SKIP = frozenset({
    "sir", "jeeves", "mister", "lang", "the", "and", "or", "of", "a",
    "mister lang", "good morning", "talk of the town", "library stacks",
    "new yorker", "the new yorker",
    # Caveat words that survive the broader regex but carry no dedup signal.
    "this", "that", "these", "those", "with", "from", "into", "what", "when",
    "while", "after", "before", "about", "today", "yesterday", "tomorrow",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december",
    "however", "indeed", "rather", "given", "their", "there", "here",
    "would", "could", "should", "shall", "will", "must", "have", "been",
    "part", "page", "vol", "iii", "html", "http", "https",
})


def _extract_written_topics(text: str) -> list[str]:
    """Extract recognizable topic slugs from a rendered HTML draft.

    Used to build within-run topic coverage so subsequent parts don't repeat
    the same story. Targets:
      - quoted titles (book/article/paper names)
      - capitalized proper-noun sequences (people, places, organisations)
      - SINGLE proper nouns ≥4 letters (Trump, Iran, Mali, Edmonds, etc.)
      - All-caps acronyms 2-6 letters (UN, EU, NATO, OFAC, AARO, OPEC)
      - named acts / laws / amendments / bills
    Returns the original-cased phrases, deduped, capped at 80.
    """
    import re as _re

    plain = re.sub(r"<[^>]+>", " ", text)
    titles = _re.findall(r'"([^"]{5,80})"', plain)
    multi = _re.findall(
        r'\b([A-Z][a-z]{1,}(?:\s[A-Z][a-z]{1,}){1,3})\b', plain
    )
    single = _re.findall(r'\b([A-Z][a-z]{3,})\b', plain)
    acronyms = _re.findall(r'\b([A-Z]{2,6})\b', plain)
    acts = _re.findall(
        r'\b([A-Z][A-Za-z\s]{3,40}(?:Act|Bill|Amendment|Law|Resolution))\b',
        plain,
    )
    combined = titles + multi + single + acronyms + acts
    seen: set[str] = set()
    out: list[str] = []
    for t in combined:
        cleaned = t.strip()
        slug = cleaned.lower()
        if slug in _TOPIC_SKIP:
            continue
        if len(slug) < 2:
            continue
        if slug in seen:
            continue
        seen.add(slug)
        out.append(cleaned)
        if len(out) >= 80:
            break
    return out


# Parts that do NOT generate profane asides in their output (pass-through
# transport parts). For these we strip the ~3000-char asides pool + the
# Horrific Slips directive from the base system prompt — those rules don't
# apply here, and keeping them wastes 2300+ tokens we need for user payload.
# Scoping, not compression: applicable instructions stay; inapplicable ones go.
_NO_ASIDE_PARTS = frozenset({"part9"})


def _system_prompt_for_parts(
    cfg: Config | None = None,
    part_label: str | None = None,
    run_used_asides: list[str] | None = None,
    run_used_topics: list[str] | None = None,
) -> str:
    """Build a per-call system prompt.

    Transforms applied to the raw ``write_system.md``:

    1. Strip the "## HTML scaffold" block — each PART_INSTRUCTIONS appendix
       provides its own explicit scaffold, so keeping the generic block in
       the base prompt would only confuse the model (two competing scaffolds).
    2. Strip the "## Briefing structure" block — each PART_INSTRUCTIONS already
       specifies which sectors to write and which fields to use.
    3. If ``part_label`` is a pass-through part (``part9``), also strip the
       Horrific Slips directive and the pre-approved asides pool. That part
       produces no asides of its own — the rules don't apply and the ~3000-char
       block would eat the token budget we need for the verbatim article.
    4. If ``cfg`` is provided and we find recent briefings on disk, append a
       "recently used — DO NOT reuse" directive listing the asides Jeeves has
       actually deployed in the last few days. The full pool stays visible
       (for parts that use it); we just flag which phrases are stale.

    Everything else — persona, mandatory rules, coverage-log rules, final
    output rules — stays verbatim.
    """
    base = _cached_write_system_prompt()

    # Use re.MULTILINE so the lookahead `^## ` anchors to a real line boundary.
    # Without MULTILINE, `.*?` would stop at the first `#` of any `### Sector`
    # subheading (two of its three `#`s look like `## ` to the lookahead).
    _FLAGS = re.DOTALL | re.MULTILINE
    base = re.sub(
        r"## HTML scaffold.*?(?=^## |\Z)", "", base, count=1, flags=_FLAGS,
    )
    base = re.sub(
        r"## Briefing structure.*?(?=^## |\Z)", "", base, count=1, flags=_FLAGS,
    )
    # Strip "## Final output rules" — these apply to a complete briefing
    # (must start with DOCTYPE, end with </html>, ≥5000 words). For per-part
    # rendering, each PART_INSTRUCTIONS appendix carries its own scoped rules.
    # Leaving the global block in causes Part 1 (and sometimes others) to
    # emit a complete briefing that the stitcher then layers Parts 2-9 onto
    # — the root cause of the multi-draft concatenation regression.
    base = re.sub(
        r"## Final output rules.*?(?=^## |\Z)", "", base, count=1, flags=_FLAGS,
    )

    if part_label in _NO_ASIDE_PARTS:
        # Strip the Horrific Slips bullet (within "## Mandatory style rules")
        # and the "### Pre-approved profane butler asides" subsection below it.
        base = re.sub(
            r"- \*\*\[HARD RULE\] Horrific Slips.*?(?=^- \*\*|^## |^### |\Z)",
            "",
            base,
            count=1,
            flags=_FLAGS,
        )
        base = re.sub(
            r"### Pre-approved profane butler asides.*?(?=^## |\Z)",
            "",
            base,
            count=1,
            flags=_FLAGS,
        )

    if part_label not in _NO_ASIDE_PARTS:
        # Combine within-run used asides with day-over-day history.
        # Cap to DEDUP_PROMPT_ASIDES_CAP most-recent entries — earlier ones are
        # unlikely to recur and only inflate the system prompt token count.
        all_avoid: list[str] = list(run_used_asides or [])
        if cfg is not None:
            for p in _recently_used_asides(cfg):
                if p not in all_avoid:
                    all_avoid.append(p)
        if all_avoid:
            all_avoid_capped = all_avoid[-DEDUP_PROMPT_ASIDES_CAP:]
            avoid_line = " | ".join(f'"{p}"' for p in all_avoid_capped)
            base = base.rstrip() + (
                "\n\n### Used asides (no repeats)\n\n"
                f"{avoid_line}\n"
            )

    if run_used_topics:
        topics_capped = run_used_topics[-DEDUP_PROMPT_TOPICS_CAP:]
        topic_str = "; ".join(topics_capped)
        base = base.rstrip() + (
            "\n\n### Run topics (avoid re-narrating)\n\n"
            f"{topic_str}\n"
        )

    _est_tokens = len(base) // 4
    log.debug("_system_prompt_for_parts [%s]: est. %d tokens", part_label, _est_tokens)

    return base.rstrip() + "\n"


# Expected `<h3>` count per part. A part emitting more than 2x the expected
# count is almost certainly hallucinating a full briefing — its h3 sections
# will collide with downstream parts and produce the 3-pass duplication bug.
_PART_H3_EXPECTED: dict[str, int] = {
    "part1": 0,    # intro + correspondence + weather; some templates use h3
    "part2": 1,    # Domestic Sphere
    "part3": 1,    # Calendar
    "part4": 2,    # family + global news
    "part5": 1,    # Reading Room (intellectual journals)
    "part6": 1,    # Specific Enquiries (triadic + ai)
    "part7": 2,    # UAP Disclosure + Commercial Ledger
    "part8": 1,    # Library Stacks
    "part9": 0,    # NEWYORKER block + signoff
}


def _truncate_to_h3_budget(html: str, max_h3: int) -> str:
    """Cut everything from the (max_h3+1)-th `<h3>` onward.

    A part that emits 7 `<h3>` headers when its budget is 1 has hallucinated
    a full briefing. Truncate so only the first `max_h3` sections remain.
    """
    matches = list(_H3_TAG_RE.finditer(html))
    if len(matches) <= max_h3:
        return html
    cut_at = matches[max_h3].start()
    return html[:cut_at].rstrip()


def _validate_part_fragment(
    part_idx: int, part_label: str, raw_html: str, total_parts: int
) -> tuple[str, list[str]]:
    """Validate + repair a single part's draft BEFORE it enters the stitcher.

    Returns (repaired_html, warnings). Warnings are appended to RunManifest
    so we can spot-check what the model is producing across runs.

    Rules:
    - Part 0 (Part 1 / first part): MUST contain DOCTYPE or open <html>;
      MUST NOT contain a closing </html> (signals the model wrote a complete
      briefing instead of just Part 1).
    - Middle parts: MUST NOT contain DOCTYPE/<html>/<head>/<body> open tags
      (those are Part 0's job); MUST NOT contain <div class="signoff">
      (Part 9's job); MUST NOT contain COVERAGE_LOG (postprocess's job).
    - Last part (Part 9): SHOULD contain `<div class="signoff">`. If missing,
      log a warning — postprocess_html injects a safety signoff.
    - Every part: `<h3>` count must not exceed 2x the expected budget.
      Over-budget = part hallucinated downstream sections; truncate to budget.
    """
    warnings: list[str] = []
    is_first = part_idx == 0
    is_last = part_idx == total_parts - 1
    low = raw_html.lower()

    if is_first:
        if "<!doctype" not in low and "<html" not in low:
            warnings.append(f"part0_missing_doctype:{part_label}")
        if "</html>" in low:
            warnings.append(f"part0_premature_html_close:{part_label}")
        if "</body>" in low:
            warnings.append(f"part0_premature_body_close:{part_label}")
        if 'class="signoff"' in low:
            warnings.append(f"part0_premature_signoff:{part_label}")
        if "<!-- coverage_log:" in low:
            warnings.append(f"part0_premature_coverage_log:{part_label}")
    else:
        if "<!doctype" in low:
            warnings.append(f"middle_part_doctype_leak:{part_label}")
        if re.search(r"<html\b", raw_html, re.IGNORECASE):
            warnings.append(f"middle_part_html_tag_leak:{part_label}")
        if re.search(r"<head\b", raw_html, re.IGNORECASE):
            warnings.append(f"middle_part_head_tag_leak:{part_label}")
        if re.search(r"<body\b", raw_html, re.IGNORECASE):
            warnings.append(f"middle_part_body_tag_leak:{part_label}")
        if not is_last:
            if 'class="signoff"' in low:
                warnings.append(f"middle_part_signoff_leak:{part_label}")
            if "<!-- coverage_log:" in low:
                warnings.append(f"middle_part_coverage_log_leak:{part_label}")

    if is_last:
        if 'class="signoff"' not in low:
            warnings.append(f"part_last_missing_signoff:{part_label}")

    # H3-budget enforcement — root cause of 3-pass duplication. A part that
    # emits 7 `<h3>` headers when its budget is 1 has hallucinated downstream
    # sections; truncate so only the first `expected` headers survive.
    expected = _PART_H3_EXPECTED.get(part_label, 1)
    h3_count = len(_H3_TAG_RE.findall(raw_html))
    if h3_count > expected * 2 and h3_count > 2:
        warnings.append(
            f"h3_budget_exceeded:{part_label}:{h3_count}>{expected}"
        )
        log.warning(
            "[%s] h3 count %d exceeds budget %d (2x ceiling) — "
            "truncating to first %d sections; downstream parts will fill the rest",
            part_label, h3_count, expected, max(expected, 1),
        )
        # Floor of 1 so part1/part9 (expected=0) still keeps any single
        # legitimate h3 the prompt occasionally produces.
        raw_html = _truncate_to_h3_budget(raw_html, max(expected, 1))

    if warnings:
        log.warning(
            "[%s] fragment validation: %s",
            part_label, ", ".join(warnings),
        )

    return raw_html, warnings


async def generate_briefing(
    cfg: Config,
    session: SessionModel,
    *,
    max_tokens: int = 4096,
) -> str:
    """Render the briefing in NINE Groq calls, each refined by a NIM pass.

    Architecture — two-model pipeline:
    1. Groq (llama-3.3-70b-versatile) drafts each part sequentially with 65s
       TPM-cooldown sleeps between calls. ~10 min total.
    2. NVIDIA NIM (meta/llama-3.3-70b-instruct) runs a targeted quality-editor
       pass on each draft immediately after it's produced, in a background
       thread. The refine thread runs during the next 65s Groq sleep, so it
       adds ~0s to the wall-clock in the common case.

    Fallback chain per part:
    - If Groq TPD is exhausted → NIM generates the draft instead.
    - If NIM refine fails for any reason → raw Groq draft is used; the
      briefing still ships, the refine failure is logged as a warning.
    - If NVIDIA_API_KEY is absent → refine is silently skipped.
    """

    import asyncio

    payload = _trim_session_for_prompt(session)
    aside_pool = _parse_all_asides()
    used_this_run: list[str] = []
    used_topics_this_run: list[str] = []

    raw_drafts: dict[str, str] = {}
    refined: dict[str, str] = {}
    refine_tasks: list[tuple[str, "asyncio.Task[None]"]] = []
    last_used_groq = True  # assume Groq until proven otherwise
    quality_warnings: list[str] = []   # unexpected fallbacks captured for RunManifest
    groq_part_count = 0
    nim_fallback_part_count = 0

    def _refine_bg_sync(label: str, draft: str) -> None:
        try:
            refined[label] = _invoke_nim_refine(cfg, draft, label=label)
        except Exception as exc:
            log.warning(
                "NIM refine failed for [%s] (%s); using raw draft", label, exc
            )
            # Carry exception type AND message in the warning so the run
            # manifest preserves enough context for forensic triage. Cap
            # the message at 120 chars to avoid bloating manifest payloads.
            exc_msg = str(exc)[:120].replace(":", ";")
            quality_warnings.append(
                f"nim_refine_failed:{label}:{type(exc).__name__}:{exc_msg}"
            )
            refined[label] = draft
        if cfg.debug_drafts:
            try:
                dbg_path = cfg.sessions_dir / f"debug-{cfg.run_date.isoformat()}-{label}-refined.html"
                dbg_path.write_text(refined[label], encoding="utf-8")
            except Exception as exc:
                log.warning("[%s] refined debug dump failed: %s", label, exc)

    for i, (label, sectors) in enumerate(PART_PLAN):
        if i > 0:
            if last_used_groq:
                # Groq free-tier 12k TPM window — must clear before next call.
                log.info(
                    "sleeping %ds before %s (Groq TPM window cooldown)",
                    cfg.groq_inter_part_sleep_s, label,
                )
                await asyncio.sleep(cfg.groq_inter_part_sleep_s)
            else:
                # NIM handled the last draft (Groq TPD exhausted). NIM has no
                # 12k TPM limit, so the cooldown sleep is unnecessary.
                log.info("NIM fallback active — skipping TPM sleep before %s", label)
        part_payload = _session_subset(payload, sectors)
        # Strip newyorker.text from Part 9 payload — the text is injected by
        # _inject_newyorker_verbatim AFTER stitching. If the model sees the text
        # it tries to copy it (imperfectly) instead of emitting the placeholder.
        if label == "part9" and "newyorker" in part_payload:
            ny = dict(part_payload["newyorker"])
            ny.pop("text", None)
            part_payload = {**part_payload, "newyorker": ny}
        base_system = _system_prompt_for_parts(
            cfg,
            part_label=label,
            run_used_asides=used_this_run,
            run_used_topics=used_topics_this_run,
        )
        part_system = base_system + PART_INSTRUCTIONS_BY_NAME[label]
        part_user = build_user_prompt_from_payload(part_payload)
        raw_part, last_used_groq = _invoke_write_llm(
            cfg, part_system, part_user, max_tokens=max_tokens, label=label
        )

        # Pre-stitch fragment validation. Catches Part 1 emitting complete
        # briefings, middle parts leaking DOCTYPE/signoff/coverage_log, etc.
        raw_part, fragment_warnings = _validate_part_fragment(
            i, label, raw_part, len(PART_PLAN)
        )
        if fragment_warnings:
            quality_warnings.extend(fragment_warnings)

        # Part 9 scaffolding hardening — guarantee TOTT intro + placeholder
        # are present so _inject_newyorker_verbatim can splice in the verbatim
        # article text. Models repeatedly skip these despite explicit prompts.
        if label == "part9":
            ny_payload = payload.get("newyorker", {}) if isinstance(payload, dict) else {}
            if not ny_payload and hasattr(payload, "newyorker"):
                ny_obj = payload.newyorker
                ny_payload = {
                    "available": getattr(ny_obj, "available", False),
                    "url": getattr(ny_obj, "url", "") or "",
                }
            ny_avail = bool(ny_payload.get("available"))
            ny_url = ny_payload.get("url", "") or ""
            scaffolded = _ensure_tott_scaffolding(raw_part, ny_avail, ny_url)
            if scaffolded != raw_part:
                quality_warnings.append("part9_tott_scaffolding_injected")
                raw_part = scaffolded

        raw_drafts[label] = raw_part
        if last_used_groq:
            groq_part_count += 1
        else:
            nim_fallback_part_count += 1

        # Per-part h3 count log for triage of full-briefing-hallucination bugs.
        h3_count = len(_H3_TAG_RE.findall(raw_part))
        log.info("[%s] H3 count: %d (budget %d)", label, h3_count,
                 _PART_H3_EXPECTED.get(label, 1))

        # Optional raw-draft dump for forensic inspection.
        if cfg.debug_drafts:
            try:
                dbg_path = cfg.sessions_dir / f"debug-{cfg.run_date.isoformat()}-{label}-raw.html"
                dbg_path.write_text(raw_part, encoding="utf-8")
                log.info("[%s] dumped raw draft to %s", label, dbg_path)
            except Exception as exc:
                log.warning("[%s] debug dump failed: %s", label, exc)

        # Density diagnostic — log word count per part. Targets are now
        # CEILINGS (sprint-17 F3.b/e), not floors: we only warn on bloat.
        part_words = len(_strip_tags(raw_part).split())
        target = _PART_WORD_TARGETS.get(label, 0)
        if target and part_words > target * 1.4:
            log.warning(
                "[%s] bloated draft: %d words > 140%% of target ceiling (%d). "
                "Likely model padded with commentary. Editor pass should trim.",
                label, part_words, target,
            )
        else:
            log.info("[%s] draft: %d words (ceiling %d)", label, part_words, target)

        # Track asides for within-run dedup before launching the refine thread.
        if label not in _NO_ASIDE_PARTS:
            for phrase in aside_pool:
                if phrase in raw_part and phrase not in used_this_run:
                    used_this_run.append(phrase)

        # Track topics for within-run dedup so subsequent parts don't repeat.
        # Skip Part 9 (verbatim New Yorker text — every proper noun in the
        # article would inflate used_topics with article-internal entities).
        if label not in _NO_ASIDE_PARTS:
            topics = _extract_written_topics(raw_part)
            for t in topics:
                if t not in used_topics_this_run:
                    used_topics_this_run.append(t)

        # Fire-and-forget NIM refine; runs during the next 65s Groq sleep.
        task = asyncio.create_task(asyncio.to_thread(_refine_bg_sync, label, raw_part))
        refine_tasks.append((label, task))

    # Wait for any refine tasks that are still running (typically only the
    # last part, since earlier tasks finish during the inter-part sleeps).
    log.info("waiting for NIM quality-editor passes to complete…")
    for label, task in refine_tasks:
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=120)
        except (asyncio.TimeoutError, Exception) as exc:
            warn_key = (
                f"nim_refine_timeout:{label}"
                if isinstance(exc, asyncio.TimeoutError)
                else f"nim_refine_wait_error:{label}:{type(exc).__name__}"
            )
            log.warning(
                "NIM refine timed out or failed for [%s] (%s); using raw draft",
                label, exc,
            )
            quality_warnings.append(warn_key)
            if not task.done():
                task.cancel()
            refined.setdefault(label, raw_drafts[label])

    final_parts = [refined.get(label, raw_drafts[label]) for label, _ in PART_PLAN]
    stitched = _stitch_parts(*final_parts)
    log.info(
        "stitched briefing: %d chars across %d parts (%s)",
        len(stitched), len(final_parts), ", ".join(str(len(p)) for p in final_parts),
    )

    # Banner image — deterministic post-stitch injection. Idempotent. Re-run
    # after OpenRouter edit too in case the editor strips it.
    stitched = _inject_banner(stitched)

    # Inject the verbatim New Yorker article text (Part 9 uses a placeholder).
    # Fallback excises any hallucinated TOTT content before the sign-off.
    stitched = _inject_newyorker_verbatim(stitched, session)

    # Guarantee exactly one Read-at-The-New-Yorker link. Idempotent —
    # strips duplicates, injects one if Part 9 dropped it.
    stitched = _ensure_single_newyorker_read_link(stitched, session)

    # Deterministically inject <a href> anchors for known source URLs.
    # Runs after TOTT injection so the New Yorker block itself is also covered.
    source_map = _build_source_url_map(session)
    stitched = _inject_source_links(stitched, source_map)
    log.info("source link injection: %d source→url pairs applied", len(source_map))

    # Final narrative quality + profane-asides pass via OpenRouter.
    # Pass the day-over-day recently-used list so OpenRouter picks fresh phrases.
    recently_used = _recently_used_asides(cfg) if cfg else []
    stitched = _invoke_openrouter_narrative_edit(cfg, stitched, recently_used_asides=recently_used)

    # Re-inject banner after OpenRouter (idempotent guard against editor stripping it).
    stitched = _inject_banner(stitched)

    # Repair any structural breakage (orphan paragraphs outside container,
    # stray </div>, profane asides emitted as standalone paragraphs).
    stitched = _repair_container_structure(stitched)
    stitched = _merge_orphan_asides(stitched)

    # Collapse adjacent duplicate <h3> headers. Parts 6 + 7 both write
    # `<h3>The Specific Enquiries</h3>` and similar collisions exist
    # whenever the prompt maps multiple parts to the same canonical header.
    stitched = _collapse_adjacent_duplicate_h3(stitched)

    # Cross-block H3 SECTION dedup. When the same `<h3>` heading appears
    # in multiple non-adjacent positions (a part hallucinated a full briefing
    # whose sections collide with other parts), keep the richest section
    # (most <a> anchors + words) and drop the rest.
    stitched = _dedup_h3_sections_across_blocks(stitched)

    # Cross-block paragraph dedup. When the same paragraph appears more
    # than once outside the verbatim TOTT block (e.g., Part 1 emitted a
    # full briefing then Parts 2-9 repeated material), drop the duplicates.
    # Uses 4-word shingle Jaccard similarity (≥0.6) — keeps the richer copy.
    stitched = _dedup_paragraphs_across_blocks(stitched)

    # URL-keyed cross-block dedup. When the same article URL is cited by
    # two parts in different prose, the Jaccard pass misses it (prose differs
    # but the citation is identical). This pass drops the lesser <p> when
    # every URL it cites has a richer occurrence elsewhere — guaranteeing no
    # unique citation is lost. Sprint-17 finding F2.a.
    stitched = _dedup_urls_across_blocks(stitched)

    # Return structured context so callers can forward quality metadata.
    # Scripts call postprocess_html(html, session, quality_warnings=warnings)
    # then _write_run_manifest(cfg, result, groq_part_count, nim_fallback_part_count).
    return stitched, quality_warnings, groq_part_count, nim_fallback_part_count


def postprocess_html(
    raw: str,
    session: SessionModel,
    *,
    quality_warnings: list[str] | None = None,
) -> BriefingResult:
    """Clean model output, ensure COVERAGE_LOG, and compute QA metrics.

    Args:
        raw: Raw HTML string from generate_briefing (or render_mock_briefing).
        session: The session model used to generate the briefing.
        quality_warnings: Optional list of quality warnings from generate_briefing
            (NIM refine failures, timeouts, etc.). Defaults to an empty list.
            Pass the warnings from generate_briefing so they appear in BriefingResult.
    """
    quality_warnings_list: list[str] = list(quality_warnings or [])

    html = _strip_markdown_fences(raw.strip())
    html = _ensure_doctype(html)
    html, coverage = _ensure_coverage_log(html, session)

    body_text = _strip_tags(html)
    word_count = len(body_text.split())

    profane_count = sum(body_text.lower().count(frag) for frag in PROFANE_FRAGMENTS)

    banned_word_hits = [w for w in BANNED_WORDS if w.lower() in body_text.lower()]
    # Word-boundary regex match for banned transitions to avoid false positives
    # like "Returning" matching "Turning to" or "next" matching "Next,". Build
    # a regex per phrase that requires word boundaries on both ends (or trailing
    # comma for transitions ending with ",").
    body_lower = body_text.lower()
    banned_transition_hits = []
    for t in BANNED_TRANSITIONS:
        t_lower = t.lower()
        # Trailing-comma transitions stay literal — comma already disambiguates.
        if t_lower.endswith(","):
            if t_lower in body_lower:
                banned_transition_hits.append(t)
            continue
        # Otherwise enforce word boundaries on both sides.
        pattern = r"\b" + re.escape(t_lower) + r"\b"
        if re.search(pattern, body_lower):
            banned_transition_hits.append(t)

    # Wrong-signoff replacement.
    # Covers: "Yours faithfully", "Your faithfully" (typo), with optional
    # trailing " Butler", optional comma, mixed case. Also catches
    # "Sincerely", "Yours sincerely", "Yours truly", "Best regards".
    html = _WRONG_SIGNOFF_FAITHFULLY.sub(_SIGNOFF_REPLACEMENT, html)
    html = _WRONG_SIGNOFF_OTHERS.sub(_SIGNOFF_REPLACEMENT, html)

    # Hard validation: the correct signoff must be present after replacement.
    # If it isn't, we surface the issue rather than ship a wrong sign-off.
    if "Your reluctantly faithful Butler" not in html:
        log.error(
            "SIGNOFF MISSING after postprocess; injecting safety signoff. "
            "Investigate Part 9 output."
        )
        # Inject a minimal signoff before </body> so the briefing ships cleanly.
        if "<div class=\"signoff\">" not in html and "</body>" in html:
            safety = (
                '<div class="signoff">\n'
                '<p>Your reluctantly faithful Butler,<br/>Jeeves</p>\n'
                '</div>\n'
            )
            html = html.replace("</body>", safety + "</body>", 1)

    structure_errors = _validate_html_structure(html)
    if structure_errors:
        log.warning("HTML structure errors after postprocess: %s", structure_errors)

    result = BriefingResult(
        html=html,
        coverage_log=coverage,
        word_count=word_count,
        profane_aside_count=profane_count,
        banned_word_hits=banned_word_hits,
        banned_transition_hits=banned_transition_hits,
        aside_placement_violations=_validate_aside_placement(html),
        link_density=_compute_link_density(html, word_count),
        structure_errors=structure_errors,
        quality_warnings=quality_warnings_list,
    )

    return result


def _write_run_manifest(
    cfg: Config,
    result: BriefingResult,
    groq_parts: int,
    nim_fallback_parts: int,
) -> None:
    """Persist a RunManifest JSON to sessions/run-manifest-DATE.json.

    Additive — does not modify the session JSON. Committed to git alongside
    the briefing HTML so quality history is queryable across days.
    """
    import dataclasses
    import json as _json

    manifest = RunManifest.from_briefing_result(
        result, cfg.run_date.isoformat(), groq_parts, nim_fallback_parts
    )
    suffix = ".local" if cfg.dry_run else ""
    path = cfg.sessions_dir / f"run-manifest-{cfg.run_date.isoformat()}{suffix}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            _json.dumps(dataclasses.asdict(manifest), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        log.info(
            "run manifest written: %s (score=%d, warnings=%d)",
            path.name, manifest.quality_score, len(manifest.quality_warnings),
        )
    except Exception as exc:
        log.warning("failed to write run manifest: %s", exc)


def _strip_markdown_fences(s: str) -> str:
    """If the model wrapped the HTML in ```html fences, strip them."""

    m = re.match(r"^```(?:html)?\s*\n(.*?)\n```\s*$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def _ensure_doctype(html: str) -> str:
    if html.lstrip().startswith("<!DOCTYPE"):
        return html
    # Try to recover: find the first <html or <!DOCTYPE anywhere and slice.
    m = re.search(r"<!DOCTYPE html", html, re.IGNORECASE)
    if m:
        return html[m.start():]
    # Last resort: wrap in a minimal scaffold so downstream code still works.
    log.warning("model did not emit <!DOCTYPE>; wrapping in fallback scaffold.")
    return (
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head><body>"
        + html
        + "</body></html>"
    )


COVERAGE_LOG_RE = re.compile(
    r"<!--\s*COVERAGE_LOG:\s*(\[.*?\])\s*-->",
    re.DOTALL,
)


def _ensure_coverage_log(
    html: str, session: SessionModel
) -> tuple[str, list[dict[str, Any]]]:
    """Guarantee exactly one COVERAGE_LOG comment and no stray PLACEHOLDER.

    Priority:
    1. Synthesize from actual <a href> anchors in the HTML (ground truth —
       catches the case where the model writes multiple partial logs or logs
       with fabricated/empty URLs).
    2. Fall back to the first valid model-written COVERAGE_LOG comment only
       when synthesis yields nothing (e.g. dry-run fixture with no anchors).

    Invariants on return:
    - Exactly ONE <!-- COVERAGE_LOG: [...] --> in the HTML.
    - No <!-- COVERAGE_LOG_PLACEHOLDER --> remaining.
    """

    # Step 1: synthesize from real anchor tags.
    synthesized = _synthesize_coverage_log(html, session)

    if synthesized:
        # Anchors are ground truth. Remove all model-written COVERAGE_LOG
        # comments (may be 0, 1, or 2 — the model sometimes writes partials),
        # strip any remaining PLACEHOLDER, then insert the synthesized log.
        html = COVERAGE_LOG_RE.sub("", html)
        comment = f"<!-- COVERAGE_LOG: {_safe_json_for_comment(synthesized)} -->"
        if "<!-- COVERAGE_LOG_PLACEHOLDER -->" in html:
            html = html.replace("<!-- COVERAGE_LOG_PLACEHOLDER -->", comment)
        elif "</body>" in html:
            html = html.replace("</body>", f"{comment}\n</body>")
        else:
            html = html.rstrip() + "\n" + comment + "\n"
        return html, synthesized

    # Step 2: no real anchors found — fall back to the model-written log if valid.
    m = COVERAGE_LOG_RE.search(html)
    if m:
        try:
            coverage = json.loads(m.group(1))
            if isinstance(coverage, list):
                # Remove any duplicate COVERAGE_LOG entries beyond the first.
                html = COVERAGE_LOG_RE.sub("", html, count=0)  # strip ALL
                comment = f"<!-- COVERAGE_LOG: {_safe_json_for_comment(coverage)} -->"
                if "<!-- COVERAGE_LOG_PLACEHOLDER -->" in html:
                    html = html.replace("<!-- COVERAGE_LOG_PLACEHOLDER -->", comment)
                elif "</body>" in html:
                    html = html.replace("</body>", f"{comment}\n</body>")
                else:
                    html = html.rstrip() + "\n" + comment + "\n"
                return html, coverage
        except json.JSONDecodeError:
            log.warning("COVERAGE_LOG JSON invalid; using empty coverage list.")

    # Step 3: nothing usable — write an empty log.
    html = COVERAGE_LOG_RE.sub("", html)
    coverage_empty: list[dict[str, Any]] = []
    comment = f"<!-- COVERAGE_LOG: {_safe_json_for_comment(coverage_empty)} -->"
    if "<!-- COVERAGE_LOG_PLACEHOLDER -->" in html:
        html = html.replace("<!-- COVERAGE_LOG_PLACEHOLDER -->", comment)
    elif "</body>" in html:
        html = html.replace("</body>", f"{comment}\n</body>")
    else:
        html = html.rstrip() + "\n" + comment + "\n"
    return html, coverage_empty


def _synthesize_coverage_log(
    html: str, session: SessionModel
) -> list[dict[str, Any]]:
    """Fallback: pull <a href> entries from the HTML and best-effort tag a sector."""

    anchors = re.findall(
        r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    sector_urls = _sector_url_index(session)
    for url, headline_html in anchors:
        if url in seen:
            continue
        seen.add(url)
        headline = _strip_tags(headline_html).strip()
        sector = sector_urls.get(url.rstrip("/"), "Unknown")
        out.append({"headline": headline, "url": url, "sector": sector})
    return out


def _sector_url_index(session: SessionModel) -> dict[str, str]:
    """Map URL → sector label based on where it appears in the session JSON."""

    idx: dict[str, str] = {}

    def _add(urls: list[str], label: str) -> None:
        for u in urls or []:
            idx[u.rstrip("/")] = label

    for f in session.local_news:
        _add(f.urls, "Sector 1")

    # Career openings and family URLs belong to Sector 2.
    career = session.career or {}
    if isinstance(career, dict):
        for opening in career.get("openings") or []:
            if isinstance(opening, dict) and opening.get("url"):
                idx[opening["url"].rstrip("/")] = "Sector 2"
    family = session.family or {}
    if isinstance(family, dict):
        _add(family.get("urls") or [], "Sector 2")

    for f in session.global_news:
        _add(f.urls, "Sector 3")
    for f in session.intellectual_journals:
        _add(f.urls, "Sector 3")
    for f in session.wearable_ai:
        _add(f.urls, "Sector 5")
    _add(session.triadic_ontology.urls, "Sector 4")
    _add(session.ai_systems.urls, "Sector 4")
    _add(session.uap.urls, "Sector 4")
    if session.newyorker.url:
        idx[session.newyorker.url.rstrip("/")] = "Sector 7"
    for art in session.enriched_articles:
        idx.setdefault(art.url.rstrip("/"), "Enriched")
    return idx


def _safe_json_for_comment(data: Any) -> str:
    """Serialise data to JSON safe for embedding inside an HTML comment.

    `-->` inside a JSON string value would prematurely close the comment and
    expose the remaining JSON as raw HTML.  Replacing it with the JSON unicode
    escape `--\\u003e` keeps the JSON valid while preventing comment breakage.
    """
    return json.dumps(data, ensure_ascii=False).replace("-->", "--\\u003e")


def _strip_tags(html: str) -> str:
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL)
    no_comments = re.sub(r"<!--.*?-->", " ", no_scripts, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", no_comments)
    return re.sub(r"\s+", " ", text).strip()


def render_mock_briefing(session: SessionModel) -> str:
    """Dry-run placeholder HTML — exercises post-processing without calling Groq."""

    sectors = [
        (
            "Sector 1 — The Domestic Sphere",
            f"Good morning, Sir. The weather: {_html.escape(session.weather or 'unremarkable')}. "
            f"I note {len(session.local_news)} local items of interest. "
            "This is a clusterfuck of biblical proportions, Sir — I do beg your pardon, "
            "I meant to say the morning commute is crowded.",
        ),
        (
            "Sector 2 — The Domestic Calendar",
            "Teaching, choral, and toddler matters. A total and utter shitshow of scheduling.",
        ),
        (
            "Sector 3 — The Intellectual Currents",
            f"{len(session.global_news)} global items, {len(session.intellectual_journals)} journal items. "
            "An absolute fuckfest of incompetence, Sir — ahem, a lively intellectual climate.",
        ),
        (
            "Sector 4 — Specific Enquiries",
            "Triadic ontology, AI systems, UAP. A steaming pile of horse-shit, pardon me.",
        ),
        (
            "Sector 5 — The Commercial Ledger",
            f"{len(session.wearable_ai)} wearable AI items. "
            "A colossal goddamn mess of vendor announcements.",
        ),
    ]
    if session.newyorker.available:
        sectors.append(
            (
                "Sector 7 — Talk of the Town",
                f"{_html.escape(session.newyorker.text[:2000])} ..."
                f"<a href=\"{_html.escape(session.newyorker.url, quote=True)}\">[Read at The New Yorker]</a>",
            )
        )

    body_html = "".join(
        f"<h2>{title}</h2><p>{body}</p>" for title, body in sectors
    )

    urls: list[str] = []
    for f in session.local_news + session.global_news + session.intellectual_journals + session.wearable_ai:
        urls.extend(f.urls)
    if session.newyorker.url:
        urls.append(session.newyorker.url)
    coverage = [{"headline": "fixture", "url": u, "sector": "Unknown"} for u in urls]

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: Georgia, 'Times New Roman', serif; background: #0a0a0a; color: #1a1714; margin: 0; padding: 48px 16px 80px; font-size: 17px; }}
    .container {{ max-width: 660px; margin: 0 auto; background: #fdfaf5; border: 1px solid #bfb090; line-height: 1.88; }}
    .banner {{ display: block; width: 100%; margin: 0; padding: 0; border: 0; }}
    .mh-date {{ background-color: #0c1015; color: #8899aa; margin: 0; padding: 36px 56px 48px; font-size: 0.72em; font-style: italic; text-align: center; letter-spacing: 0.08em; border-bottom: 3px solid #c8902a; }}
    h2 {{ background-color: #0c1015; color: #c8902a; margin: 3.2em 0 0; padding: 24px 56px; font-size: 0.55em; font-weight: normal; text-transform: uppercase; letter-spacing: 0.6em; border-top: 3px solid #c8902a; }}
    h3 {{ font-size: 1.1em; font-weight: bold; font-style: italic; color: #18375a; margin: 2em 40px 0.5em; padding: 0 0 0 20px; border-left: 4px solid #c8902a; line-height: 1.4; }}
    p {{ margin: 0 56px 1.5em; padding: 0; }}
    .mh-date + p {{ margin-top: 2.6em; }}
    h2 + p {{ margin-top: 1.4em; }}
    a {{ color: #18375a; text-decoration: none; border-bottom: 1px solid #88a8c8; }}
    .dc {{ float: left; font-size: 5em; line-height: 0.68; padding-right: 8px; padding-top: 5px; color: #c8902a; font-weight: bold; }}
    .ny-header {{ font-size: 0.58em; text-transform: uppercase; letter-spacing: 0.45em; color: #c8902a; margin-bottom: 20px; padding-bottom: 14px; border-bottom: 1px solid #c8a040; }}
    .newyorker {{ background-color: #f0e8d2; border-top: 3px solid #c8902a; border-bottom: 3px solid #c8902a; margin: 3em 0; padding: 32px 56px 36px; }}
    .newyorker p {{ margin: 0 0 1.2em; padding: 0; }}
    .newyorker p:last-child {{ margin-bottom: 0; }}
    .signoff {{ border-top: 3px solid #c8902a; padding: 36px 56px 62px; font-style: italic; text-align: right; color: #5a4828; margin-top: 2em; }}
    .signoff p {{ margin: 0; padding: 0; line-height: 1.9; }}
  </style>
</head>
<body>
<div class="container">
  <img class="banner" src="{_BANNER_URL}" alt="">
  <div class="mh-date">DRY RUN</div>
  {body_html}
  <div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>
  <!-- COVERAGE_LOG: {_safe_json_for_comment(coverage)} -->
</div>
</body>
</html>"""
