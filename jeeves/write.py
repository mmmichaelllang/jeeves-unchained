"""Phase 3 — Groq Llama 3.3 70B renders a session JSON into Jeeves-voice HTML."""

from __future__ import annotations

import html as _html
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .schema import SessionModel

log = logging.getLogger(__name__)

WRITE_PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "write_system.md"


@dataclass
class BriefingResult:
    html: str
    coverage_log: list[dict[str, Any]]
    word_count: int
    profane_aside_count: int
    banned_word_hits: list[str]
    banned_transition_hits: list[str]


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


DEDUP_PROMPT_HEADLINES_CAP = 80


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
            dedup["covered_headlines"] = dedup["covered_headlines"][:DEDUP_PROMPT_HEADLINES_CAP]
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

Aim for ~600-800 words. No profane asides — the final editor adds them.
When Sector 1 opening is complete,
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
11. WIT QUOTA. At least one sardonic, wry, or darkly humorous observation per
    part. This may be a short parenthetical, a loaded short sentence after a
    long paragraph, an ironic understatement, or a dry aside about human folly.
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
    - Any sentence that could be copy-pasted unchanged into a briefing about
      a completely different topic. Zero topic-specific nouns = delete it.
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

Aim for ~500-700 words when items exist. No profane asides in draft.
Missing persons or fatal incidents must be treated with sober gravity.

When done, emit `<!-- PART2 END -->` and STOP. Do NOT close outer tags.
"""

PART3_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 3 of 9 — teaching jobs

Parts 1-2 covered Sector 1 (greeting, correspondence, weather, local news).
You pick up from there.

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

**SYNTHESIS CLOSE (REQUIRED):**
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

Aim for ~500-700 words. No profane asides in draft — the final editor adds them.

When done, emit `<!-- PART3 END -->` and STOP. Do NOT close outer tags.
"""

PART4_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 4 of 9 — family + global news

Parts 1-3 covered Sector 1 and the career portion of Sector 2.
You pick up from there.

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

Aim for ~700-900 words when items exist. No profane asides in draft.

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

**SYNTHESIS CLOSE (REQUIRED):**
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

Aim for ~600-800 words. No profane asides in draft — the final editor adds them.

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

**SYNTHESIS CLOSE (REQUIRED):**
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

Aim for ~600-800 words total for this part. No profane asides in draft.

When done, emit `<!-- PART6 END -->` and STOP. Do NOT close outer tags.
"""

PART7_INSTRUCTIONS = CONTINUATION_RULES + """

---

## PART 7 of 9 — UAP + wearables

Parts 1-6 covered Sectors 1-3 plus the triadic/AI portion of Sector 4.
You pick up from there.

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

Aim for ~600-800 words total for this part. No profane asides in draft.

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

Do NOT write "I had hoped to find some solace in the library stacks."
Do NOT write "the vault insight is entirely empty" — "vault insight" and
"vault_insight" are internal field names; Jeeves does not know them.
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
    s = _re.sub(r"^```(?:html)?\s*", "", s)
    s = _re.sub(r"\s*```\s*$", "", s)
    return s


def _strip_continuation_wrapper(s: str) -> str:
    """Remove DOCTYPE/head/body/h1/masthead divs that a continuation part leaked."""
    import re as _re
    s = _re.sub(r"^<!DOCTYPE[^>]*>", "", s, flags=_re.IGNORECASE).strip()
    s = _re.sub(r"<html[^>]*>", "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"<head>.*?</head>", "", s, flags=_re.IGNORECASE | _re.DOTALL)
    s = _re.sub(r"<body[^>]*>", "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"<h1[^>]*>.*?</h1>", "", s, flags=_re.IGNORECASE | _re.DOTALL)
    # Strip masthead divs (mh-label, mh-date) if a continuation part leaks them.
    s = _re.sub(r'<div[^>]*class="mh-(?:label|date)"[^>]*>.*?</div>', "", s, flags=_re.IGNORECASE | _re.DOTALL)
    return s.strip()


def _stitch_parts(*parts: str) -> str:
    """Glue N briefing parts into one coherent HTML document.

    Part 1 carries the DOCTYPE/head/body/h1. Parts 2+ are HTML fragments.
    Sentinel comments (<!-- PART1 END -->, <!-- PART2 END -->, etc.) are stripped.
    If the final part didn't close </body></html>, we append them.
    """

    cleaned: list[str] = []
    for i, raw in enumerate(parts):
        s = _strip_fences((raw or "").strip())
        # Remove sentinel comments of any part number.
        import re as _re
        s = _re.sub(r"<!--\s*PART\d+\s*END\s*-->", "", s).rstrip()
        if i > 0:
            s = _strip_continuation_wrapper(s)
        cleaned.append(s)

    combined = "\n".join(p for p in cleaned if p)
    low = combined.lower()
    if "</body>" not in low:
        combined += "\n</body>"
    if "</html>" not in low:
        combined += "\n</html>"
    return combined


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
""".strip()


_NIM_RETRY_DELAYS = (2, 8, 32)  # seconds between attempts on 429


def _is_nim_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "too many requests" in msg


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

    llm = build_nim_write_llm(cfg, temperature=0.2, max_tokens=4096)
    user = f"Edit the following HTML fragment:\n\n{draft_html}"
    log.info("NIM refine [%s] (%d chars draft)", label, len(draft_html))
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=_REFINE_SYSTEM),
        ChatMessage(role=MessageRole.USER, content=user),
    ]
    last_exc: Exception | None = None
    for attempt, delay in enumerate((*_NIM_RETRY_DELAYS, None)):
        try:
            resp = llm.chat(messages)
            return str(resp.message.content or draft_html)
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
    """Call NIM as a fallback write-draft generator (Groq TPD exhausted).

    Retries up to 3 times with exponential backoff (2s, 8s, 32s) on HTTP 429.
    """
    import time

    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .llm import build_nim_write_llm

    if not cfg.nvidia_api_key:
        raise RuntimeError(
            "Groq TPD exhausted and NVIDIA_API_KEY is not set — cannot fall back to NIM. "
            "Add NVIDIA_API_KEY to secrets or wait for Groq's daily quota to reset (midnight UTC)."
        )
    llm = build_nim_write_llm(cfg, temperature=0.65, max_tokens=max_tokens)
    log.info(
        "invoking NIM write fallback %s [%s] (max_tokens=%d, system=%d chars, user=%d chars)",
        cfg.nim_write_model_id, label, max_tokens, len(system), len(user),
    )
    messages = [
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=user),
    ]
    last_exc: Exception | None = None
    for attempt, delay in enumerate((*_NIM_RETRY_DELAYS, None)):
        try:
            resp = llm.chat(messages)
            return str(resp.message.content or "")
        except Exception as exc:
            last_exc = exc
            if _is_nim_rate_limit(exc) and delay is not None:
                log.warning(
                    "NIM write [%s] got 429 (attempt %d/4); retrying in %ds",
                    label, attempt + 1, delay,
                )
                time.sleep(delay)
            else:
                raise
    raise RuntimeError(f"NIM write [%s] exhausted retries: {last_exc}")


def _invoke_write_llm(
    cfg: Config, system: str, user: str, *, max_tokens: int, label: str
) -> tuple[str, bool]:
    """Call Groq for the write phase; auto-fall back to NIM on daily-quota exhaustion.

    Returns (text, used_groq). used_groq=False means Groq TPD was exhausted and
    NIM handled the draft — the caller can skip the Groq TPM cooldown sleep.

    Groq's free-tier TPD (tokens-per-day) limit charges input_tokens +
    max_tokens_requested per call. At 100k tokens/day the 9-part pipeline
    (~63k tokens at max_tokens=3000) fits, but test runs earlier in the day
    can exhaust the budget. When the specific TPD error fires, we transparently
    retry on NVIDIA NIM (meta/llama-3.3-70b-instruct — same model family).
    """
    try:
        return _invoke_groq(cfg, system, user, max_tokens=max_tokens, label=label), True
    except Exception as e:
        if "tokens per day" in str(e).lower():
            log.warning(
                "Groq daily TPD quota exhausted on [%s]; retrying on NIM (%s). "
                "Groq free-tier resets at midnight UTC.",
                label, cfg.nim_write_model_id,
            )
            return _invoke_nim_write(cfg, system, user, max_tokens=max_tokens, label=label), False
        raise


_NY_INTRO_MARKER = "reading from this week's Talk of the Town"
_NY_SIGNOFF_MARKERS = ('<div class="signoff">', "<!-- COVERAGE_LOG")


def _build_newyorker_block(text: str, url: str) -> str:
    """Return formatted NEWYORKER_START…END block plus the Read link paragraph."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    read_link = f'\n<p><a href="{url}">Read at The New Yorker</a></p>' if url else ""
    return (
        "<!-- NEWYORKER_START -->\n"
        + '<div class="newyorker">\n'
        + '<div class="ny-header">The New Yorker &middot; Talk of the Town</div>\n'
        + "\n".join(f"<p>{p}</p>" for p in paragraphs)
        + "\n</div>"
        + "\n<!-- NEWYORKER_END -->"
        + read_link
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
        log.warning(
            "Talk of the Town intro sentence also missing — "
            "verbatim article text will not appear in this briefing."
        )
        return html

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

    if signoff_idx == -1:
        # No sign-off found — just insert after intro, leave rest intact.
        log.warning("Could not find sign-off anchor; inserting after intro only.")
        block = "\n" + _build_newyorker_block(ny_text, ny_url) + "\n"
        return html[:intro_end] + block + html[intro_end:]

    # Splice: intro_end … signoff_idx is the hallucinated zone — replace entirely.
    block = "\n" + _build_newyorker_block(ny_text, ny_url) + "\n"
    return html[:intro_end] + block + html[signoff_idx:]


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


def _inject_source_links(html: str, source_url_map: dict[str, str]) -> str:
    """Deterministically inject <a href> anchors for known source names.

    For each (source_name, url) pair, finds the FIRST occurrence of source_name
    in the HTML that is NOT already inside an <a> tag, and wraps it in an anchor.
    Operates on the raw HTML string using a split-on-anchors approach so existing
    links are never disturbed.

    Only the first occurrence per source is linked (enough to satisfy rule 8
    without cluttering prose with repeated anchors to the same URL).
    """
    if not source_url_map:
        return html

    # Split HTML into alternating [outside_anchor, inside_anchor, ...] segments.
    # We only modify segments that are NOT inside <a>…</a> tags.
    # Pattern: everything up to an <a …>, the anchor content, the </a>, repeat.
    _A_SPLIT = re.compile(r"(<a\b[^>]*>.*?</a>)", re.IGNORECASE | re.DOTALL)

    for source_name, url in source_url_map.items():
        if not source_name or not url:
            continue
        # Skip if already linked anywhere in the document.
        if url in html:
            continue
        # Escape for use in a word-boundary regex.
        # IGNORECASE so domain-extracted names (magicschool.ai) match prose
        # capitalisation (MagicSchool.ai). m.group(0) preserves original case.
        escaped = re.escape(source_name)
        pattern = re.compile(
            r"(?<![a-zA-Z0-9\-])" + escaped + r"(?![a-zA-Z0-9\-])",
            re.IGNORECASE,
        )

        segments = _A_SPLIT.split(html)
        replaced = False
        for i, seg in enumerate(segments):
            if replaced:
                break
            # Even-indexed segments are outside anchors.
            if i % 2 == 0 and pattern.search(seg):
                new_seg, count = pattern.subn(
                    lambda m, _url=url: f'<a href="{_url}">{m.group(0)}</a>',
                    seg,
                    count=1,
                )
                if count:
                    segments[i] = new_seg
                    replaced = True
        if replaced:
            html = "".join(segments)

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

## PART B — PROFANE ASIDES (reach exactly five total)

Count the profane asides already present in the draft — the Groq drafting
pass is instructed to write zero, but may have included one or two. Your job
is to bring the total to **exactly five**. If the draft has zero asides, add
five. If it has two, add three. If it already has five, add none. Never exceed
five total; never go below five total.

**Rules for placement:**
1. Each aside must be *earned* — it reacts to a specific, named dysfunction,
   absurdity, or outrage that Jeeves has just described. Never decorative.
2. Alter sentence structure to make it feel like the aside erupted naturally.
   Let the annoyance escalate FIRST, then let the phrase land.
   Wrong: "The transit merger is an absolute shit-show, Sir."
   Right: "Everett transit is merging with Sound Transit. The financing is
           unclear, the timeline is fictional, and the public consultation was
           a nine-person Zoom call at 2pm on a Tuesday. What a shit-show."
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
_OPENROUTER_FALLBACK_MODELS = [
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-4-31b-it:free",
    "openrouter/auto",
]


_NY_EDIT_PLACEHOLDER = "<!-- NEWYORKER_EDIT_PLACEHOLDER -->"
_NY_BLOCK_RE = re.compile(
    r"<!-- NEWYORKER_START -->.*?<!-- NEWYORKER_END -->", re.DOTALL
)


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
    from openai import OpenAI

    if not cfg.openrouter_api_key:
        log.debug("OPENROUTER_API_KEY not set; skipping narrative edit pass")
        return html

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
                max_tokens=16384,
                temperature=0.4,
            )
            edited = (resp.choices[0].message.content or "").strip()
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
            log.info("OpenRouter narrative edit complete via [%s] (%d chars output)", model, len(edited))
            return edited
        except Exception as exc:
            log.warning("OpenRouter [%s] failed (%s); trying next model", model, exc)

    log.warning("All OpenRouter models exhausted; using unedited document")
    return html


ASIDES_RECENT_WINDOW_DAYS = 4


def _parse_all_asides() -> list[str]:
    """Return the full set of pre-approved profane asides from write_system.md.

    The list lives on a single line that starts with `"clusterfuck of
    biblical proportions` and ends before the next blank line. We locate
    that line and extract every quoted phrase on it.
    """
    import re as _re

    base = load_write_system_prompt()
    m = _re.search(
        r'^"clusterfuck of biblical proportions[^\n]+$',
        base,
        flags=_re.MULTILINE,
    )
    if not m:
        return []
    return _re.findall(r'"([^"]+)"', m.group(0))


def _recently_used_asides(cfg: Config, days: int = ASIDES_RECENT_WINDOW_DAYS) -> list[str]:
    """Scan the last N days of `sessions/briefing-*.html` and return the list
    of pre-approved asides that Jeeves has actually dropped into prose.

    We pass this back into the system prompt so Jeeves can dodge yesterday's
    three favorites. Semantic / thematic matching stays the model's call —
    the full aside pool remains in the prompt, we just flag the ones to avoid.
    """
    from datetime import timedelta

    pool = _parse_all_asides()
    if not pool:
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
        return []

    joined = "\n".join(recent_html)
    used = [phrase for phrase in pool if phrase in joined]
    return used


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
    import re as _re

    base = load_write_system_prompt()

    # Use re.MULTILINE so the lookahead `^## ` anchors to a real line boundary.
    # Without MULTILINE, `.*?` would stop at the first `#` of any `### Sector`
    # subheading (two of its three `#`s look like `## ` to the lookahead).
    _FLAGS = _re.DOTALL | _re.MULTILINE
    base = _re.sub(
        r"## HTML scaffold.*?(?=^## |\Z)", "", base, count=1, flags=_FLAGS,
    )
    base = _re.sub(
        r"## Briefing structure.*?(?=^## |\Z)", "", base, count=1, flags=_FLAGS,
    )

    if part_label in _NO_ASIDE_PARTS:
        # Strip the Horrific Slips bullet (within "## Mandatory style rules")
        # and the "### Pre-approved profane butler asides" subsection below it.
        base = _re.sub(
            r"- \*\*\[HARD RULE\] Horrific Slips.*?(?=^- \*\*|^## |^### |\Z)",
            "",
            base,
            count=1,
            flags=_FLAGS,
        )
        base = _re.sub(
            r"### Pre-approved profane butler asides.*?(?=^## |\Z)",
            "",
            base,
            count=1,
            flags=_FLAGS,
        )

    if part_label not in _NO_ASIDE_PARTS:
        # Combine within-run used asides with day-over-day history.
        all_avoid: list[str] = list(run_used_asides or [])
        if cfg is not None:
            for p in _recently_used_asides(cfg):
                if p not in all_avoid:
                    all_avoid.append(p)
        if all_avoid:
            avoid_line = " | ".join(f'"{p}"' for p in all_avoid)
            base = base.rstrip() + (
                "\n\n### Recently used asides — DO NOT reuse in today's briefing\n\n"
                "The following asides appeared in earlier parts of today's briefing "
                f"or in Jeeves's briefings over the last {ASIDES_RECENT_WINDOW_DAYS} "
                "days. Pick a fresh phrase from the full pool above — same thematic "
                "matching rules apply, just a different word choice:\n\n"
                f"{avoid_line}\n"
            )

    return base.rstrip() + "\n"


def generate_briefing(
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

    import threading
    import time

    payload = _trim_session_for_prompt(session)
    aside_pool = _parse_all_asides()
    used_this_run: list[str] = []

    raw_drafts: dict[str, str] = {}
    refined: dict[str, str] = {}
    refine_threads: list[tuple[str, threading.Thread]] = []
    last_used_groq = True  # assume Groq until proven otherwise

    def _refine_bg(label: str, draft: str) -> None:
        try:
            refined[label] = _invoke_nim_refine(cfg, draft, label=label)
        except Exception as exc:
            log.warning(
                "NIM refine failed for [%s] (%s); using raw draft", label, exc
            )
            refined[label] = draft

    for i, (label, sectors) in enumerate(PART_PLAN):
        if i > 0:
            if last_used_groq:
                # Groq free-tier 12k TPM window — must clear before next call.
                log.info("sleeping 65s before %s (Groq TPM window cooldown)", label)
                time.sleep(65)
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
            cfg, part_label=label, run_used_asides=used_this_run
        )
        part_system = base_system + PART_INSTRUCTIONS_BY_NAME[label]
        part_user = build_user_prompt_from_payload(part_payload)
        raw_part, last_used_groq = _invoke_write_llm(
            cfg, part_system, part_user, max_tokens=max_tokens, label=label
        )
        raw_drafts[label] = raw_part

        # Track asides for within-run dedup before launching the refine thread.
        if label not in _NO_ASIDE_PARTS:
            for phrase in aside_pool:
                if phrase in raw_part and phrase not in used_this_run:
                    used_this_run.append(phrase)

        # Fire-and-forget NIM refine; runs during the next 65s Groq sleep.
        t = threading.Thread(target=_refine_bg, args=(label, raw_part), daemon=True)
        t.start()
        refine_threads.append((label, t))

    # Wait for any refine threads that are still running (typically only the
    # last part, since earlier threads finish during the inter-part sleeps).
    log.info("waiting for NIM quality-editor passes to complete…")
    for label, t in refine_threads:
        t.join(timeout=120)
        if t.is_alive():
            log.warning("NIM refine timed out for [%s]; using raw draft", label)
            refined.setdefault(label, raw_drafts[label])

    final_parts = [refined.get(label, raw_drafts[label]) for label, _ in PART_PLAN]
    stitched = _stitch_parts(*final_parts)
    log.info(
        "stitched briefing: %d chars across %d parts (%s)",
        len(stitched), len(final_parts), ", ".join(str(len(p)) for p in final_parts),
    )

    # Inject the verbatim New Yorker article text (Part 9 uses a placeholder).
    # Fallback excises any hallucinated TOTT content before the sign-off.
    stitched = _inject_newyorker_verbatim(stitched, session)

    # Deterministically inject <a href> anchors for known source URLs.
    # Runs after TOTT injection so the New Yorker block itself is also covered.
    source_map = _build_source_url_map(session)
    stitched = _inject_source_links(stitched, source_map)
    log.info("source link injection: %d source→url pairs applied", len(source_map))

    # Final narrative quality + profane-asides pass via OpenRouter.
    # Pass the day-over-day recently-used list so OpenRouter picks fresh phrases.
    recently_used = _recently_used_asides(cfg) if cfg else []
    stitched = _invoke_openrouter_narrative_edit(cfg, stitched, recently_used_asides=recently_used)

    return stitched


def postprocess_html(raw: str, session: SessionModel) -> BriefingResult:
    """Clean model output, ensure COVERAGE_LOG, and compute QA metrics."""

    html = _strip_markdown_fences(raw.strip())
    html = _ensure_doctype(html)
    html, coverage = _ensure_coverage_log(html, session)

    body_text = _strip_tags(html)
    word_count = len(body_text.split())

    profane_count = sum(body_text.lower().count(frag) for frag in PROFANE_FRAGMENTS)

    banned_word_hits = [w for w in BANNED_WORDS if w.lower() in body_text.lower()]
    banned_transition_hits = [t for t in BANNED_TRANSITIONS if t.lower() in body_text.lower()]

    if "yours faithfully" in body_text.lower():
        log.warning("WRONG SIGNOFF: 'Yours faithfully' found — replacing with correct sign-off")
        html = re.sub(
            r"[Yy]ours faithfully,?",
            "Your reluctantly faithful Butler,",
            html,
        )

    return BriefingResult(
        html=html,
        coverage_log=coverage,
        word_count=word_count,
        profane_aside_count=profane_count,
        banned_word_hits=banned_word_hits,
        banned_transition_hits=banned_transition_hits,
    )


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
  <img class="banner" src="https://i.imgur.com/UqSFELh.png" alt="">
  <div class="mh-date">DRY RUN</div>
  {body_html}
  <div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>
  <!-- COVERAGE_LOG: {_safe_json_for_comment(coverage)} -->
</div>
</body>
</html>"""
