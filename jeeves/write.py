"""Phase 3 — Groq Llama 3.3 70B renders a session JSON into Jeeves-voice HTML."""

from __future__ import annotations

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


BANNED_WORDS = ["in a vacuum", "tapestry"]
BANNED_TRANSITIONS = ["Moving on,", "Next,", "Turning to,", "In other news,"]
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


def build_user_prompt(session: SessionModel) -> str:
    """Serialize the session JSON into the LLM user message."""

    payload = _trim_session_for_prompt(session)
    return build_user_prompt_from_payload(payload)


def build_user_prompt_from_payload(payload: dict[str, Any]) -> str:
    return (
        "Here is the research session JSON. Render the briefing now in Jeeves's "
        "voice, following every rule in the system prompt. Output HTML only, "
        "starting with <!DOCTYPE html>.\n\n"
        "```json\n"
        + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        + "\n```"
    )


# -- Eight-call render (free-tier 12k TPM ceiling) --
#
# llama-3.3-70b on Groq's free `on_demand` tier caps at 12k TPM. The full
# system prompt (persona + rules + all seven sector descriptions + full
# profane-aside pool + coverage-log + output rules) is ~11.7k chars
# (~7.8k tokens) on its own — there's no headroom for a rich session JSON
# in one call. We keep the system prompt intact and split the user payload
# across EIGHT sequential Groq calls with a 65s sleep between each so the
# rolling 60s TPM window clears. ~9 min total wall-clock.
#
# Each PART handles one slice of the briefing. The first opens the document;
# the last closes it with the sign-off and the coverage-log placeholder.

# (name, session_field_list). The 8-slot plan balances per-part user-payload
# chars so no call creeps above the free-tier 12k TPM ceiling. Heaviest fields
# (local_news, career, ai_systems) each get their own slot; smaller fields are
# paired.
PART_PLAN: list[tuple[str, list[str]]] = [
    ("part1", ["correspondence", "weather"]),
    ("part2", ["local_news"]),
    ("part3", ["career"]),
    ("part4", ["family", "global_news"]),
    ("part5", ["intellectual_journals", "enriched_articles"]),
    ("part6", ["triadic_ontology", "ai_systems"]),
    ("part7", ["uap", "wearable_ai"]),
    ("part8", ["vault_insight", "newyorker"]),
]

# Back-compat aliases — a few tests import PART1_SECTORS et al.
PART1_SECTORS = PART_PLAN[0][1]
PART2_SECTORS = PART_PLAN[1][1]
PART3_SECTORS = PART_PLAN[2][1]


PART1_INSTRUCTIONS = """

---

## PART 1 of 8 — render instructions

You are writing PART 1 of an eight-part briefing. Output the full HTML opening
(`<!DOCTYPE html>` through `<body>` and the `<div class="container">` wrapper),
the `<h1>` with today's full weekday date, then:

- Sector 1 opening material: the formal butler greeting to Mister Lang, the
  correspondence summary (if `correspondence.found=true`), and the weather
  forecast from `weather`.

**Correspondence summary rule:** If `correspondence.found=true` and
`correspondence.fallback_used=false`, open with: *"The morning's correspondence
has already been laid out in full, Sir, but the salient matters are these…"*
and condense `correspondence.text` to roughly 400 words in Jeeves's voice.
If `fallback_used=true`, summarize naturally without that opener.

Aim for ~600-800 words. 1 profane aside. When Sector 1 opening is complete,
emit the literal comment `<!-- PART1 END -->` and STOP.

Do NOT write local_news yet — Part 2 handles it. Do NOT write the sign-off.
Do NOT close `</div>`, `</body>`, or `</html>`. Later parts continue the document.
"""

PART2_INSTRUCTIONS = """

---

## PART 2 of 8 — render instructions

You are CONTINUING a briefing. Part 1 opened the HTML and covered the greeting,
correspondence summary, and weather. You do NOT rewrite any of that.

Output ONLY Sector 1's local-news material: municipal / Edmonds items from
`local_news` whose category is municipal/civic/development, then public-safety
items from `local_news` that satisfy the 3-mile geofence (3 miles from
47.810652, -122.377355; serious incidents only). Raw HTML paragraphs. NO
`<!DOCTYPE html>`, NO `<head>`, NO `<body>`, NO new `<h1>`.

Aim for ~500-700 words. When done, emit `<!-- PART2 END -->` and STOP.
Do NOT close outer tags. Later parts continue.
"""

PART3_INSTRUCTIONS = """

---

## PART 3 of 8 — render instructions

You are CONTINUING a briefing. Parts 1-2 already covered Sector 1.

Output ONLY the teaching-jobs portion of Sector 2 — The Domestic Calendar,
drawn from `career`. This is the job-board sweep for HS English / History
openings within ~30 miles of Edmonds. Raw HTML paragraphs only. No DOCTYPE/
head/body/h1.

Aim for ~500-700 words. 1 profane aside (match the tone of any bureaucratic
dysfunction you encounter in the listings). When done, emit `<!-- PART3 END -->`
and STOP. Do NOT close outer tags.
"""

PART4_INSTRUCTIONS = """

---

## PART 4 of 8 — render instructions

You are CONTINUING a briefing. Parts 1-3 covered Sector 1 plus the career
portion of Sector 2.

Output the rest of Sector 2 (family: choral auditions from `family.choir`,
toddler activities from `family.toddler`) followed by the global-news
portion of Sector 3, drawn from `global_news`. Raw HTML paragraphs only.
No DOCTYPE/head/body/h1.

Aim for ~700-900 words. 1-2 profane asides. When done, emit `<!-- PART4 END -->`
and STOP. Do NOT close outer tags.
"""

PART5_INSTRUCTIONS = """

---

## PART 5 of 8 — render instructions

You are CONTINUING a briefing. Parts 1-4 covered Sectors 1-2 and the global-
news portion of Sector 3.

Output ONLY the intellectual-journals / long-form portion of Sector 3, drawn
from `intellectual_journals` and deepened where possible with `enriched_articles`.
Raw HTML paragraphs only. No DOCTYPE/head/body/h1.

Aim for ~600-800 words. When done, emit `<!-- PART5 END -->` and STOP.
Do NOT close outer tags.
"""

PART6_INSTRUCTIONS = """

---

## PART 6 of 8 — render instructions

You are CONTINUING a briefing. Parts 1-5 covered Sectors 1-3.

Output the triadic-ontology and AI-systems portion of Sector 4 — Specific
Enquiries. Use `triadic_ontology` and `ai_systems`. Raw HTML paragraphs only.
No DOCTYPE/head/body/h1.

Aim for ~600-800 words. 1 profane aside. When done, emit `<!-- PART6 END -->`
and STOP. Do NOT close outer tags.
"""

PART7_INSTRUCTIONS = """

---

## PART 7 of 8 — render instructions

You are CONTINUING a briefing. Parts 1-6 covered Sectors 1-3 plus the triadic
and AI-systems portion of Sector 4.

Output the UAP portion of Sector 4 (from `uap`), then Sector 5 — Wearable
Intelligence (from `wearable_ai`, all three subcategories: AI voice hardware,
teacher AI tools, wearable devices). Raw HTML paragraphs only. No DOCTYPE/
head/body/h1.

Aim for ~700-900 words. 1 profane aside. When done, emit `<!-- PART7 END -->`
and STOP. Do NOT close outer tags. Part 8 delivers Library Stacks, Talk of
the Town, and the sign-off.
"""

PART8_INSTRUCTIONS = """

---

## PART 8 of 8 — render instructions

You are CONTINUING a briefing. Parts 1-7 covered Sectors 1-5.

Output, in this exact order:

1. Sector 6 — From the Library Stacks, ONLY if `vault_insight.available === true`.
   Introduction: *"I have been, as is my habit, browsing the library stacks in
   the small hours, Sir, and came across something rather arresting…"* Present
   `vault_insight.insight` in Jeeves's voice at roughly 200 words. Reference
   with *"Drawn from your notes on [topic]…"* — never expose `note_path`.
   Close with one wry (non-profane) Jeeves aside.

2. Sector 7 — Talk of the Town, ONLY if `newyorker.available === true` (MUST
   be last). Introduction: *"And now, Sir, I take the liberty of reading from
   this week's Talk of the Town in The New Yorker."* Output `newyorker.text`
   verbatim and in full — every word, every paragraph, as HTML `<p>` tags.
   One brief weary closing Jeeves remark. End with the URL as
   `<a href="[newyorker.url]">[Read at The New Yorker]</a>`.

If both Sector 6 and Sector 7 are unavailable, write a single brief sentence
acknowledging the slim morning from the library and the press.

After the content, emit the closing signoff block AND the coverage-log
placeholder AND the outer closing tags:

```html
<div class="signoff">
  <p>Your reluctantly faithful Butler,<br/>Jeeves</p>
</div>
<!-- COVERAGE_LOG_PLACEHOLDER -->
</div>
</body>
</html>
```

The `<!-- COVERAGE_LOG_PLACEHOLDER -->` is intentional — the post-processor
fills it by scanning anchor tags across the full stitched document.
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
}


def _session_subset(payload: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    """Build a subset payload containing only the listed sector fields + housekeeping."""

    base = {
        "date": payload.get("date", ""),
        "status": payload.get("status", "complete"),
        "dedup": payload.get("dedup") or {"covered_headlines": []},
    }
    for f in fields:
        if f in payload:
            base[f] = payload[f]
    return base


def _strip_fences(s: str) -> str:
    import re as _re
    s = _re.sub(r"^```(?:html)?\s*", "", s)
    s = _re.sub(r"\s*```\s*$", "", s)
    return s


def _strip_continuation_wrapper(s: str) -> str:
    """Remove DOCTYPE/head/body/h1 that a continuation part leaked despite instructions."""
    import re as _re
    s = _re.sub(r"^<!DOCTYPE[^>]*>", "", s, flags=_re.IGNORECASE).strip()
    s = _re.sub(r"<html[^>]*>", "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"<head>.*?</head>", "", s, flags=_re.IGNORECASE | _re.DOTALL)
    s = _re.sub(r"<body[^>]*>", "", s, flags=_re.IGNORECASE)
    s = _re.sub(r"<h1[^>]*>.*?</h1>", "", s, flags=_re.IGNORECASE | _re.DOTALL)
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


def _invoke_groq(cfg: Config, system: str, user: str, *, max_tokens: int, label: str) -> str:
    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .llm import build_groq_llm

    llm = build_groq_llm(cfg, temperature=0.65, max_tokens=max_tokens)
    log.info(
        "invoking Groq %s [%s] (max_tokens=%d, system=%d chars, user=%d chars)",
        cfg.groq_model_id, label, max_tokens, len(system), len(user),
    )
    resp = llm.chat([
        ChatMessage(role=MessageRole.SYSTEM, content=system),
        ChatMessage(role=MessageRole.USER, content=user),
    ])
    return str(resp.message.content or "")


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


def _system_prompt_for_parts(cfg: Config | None = None) -> str:
    """Build a per-call system prompt.

    Two transforms:

    1. Strip the "## HTML scaffold" block — each PART_INSTRUCTIONS appendix
       provides its own explicit scaffold, so keeping the generic block in
       the base prompt would only confuse the model (two competing scaffolds).
    2. If `cfg` is provided and we can find recent briefings on disk, append
       a "recently used — DO NOT reuse" directive listing the asides Jeeves
       has actually deployed in the last few days. The full pool stays in
       the prompt; we just flag which phrases are stale. This is the anti-
       repetition lever while preserving semantic/thematic matching.

    Everything else — persona, mandatory rules, all seven sector descriptions,
    coverage-log rules, final output rules — stays verbatim.
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
    # Strip the sector-descriptions block — each PART_INSTRUCTIONS already
    # specifies which sectors to write and which data fields to use.  The full
    # seven-sector narrative costs ~2800 chars (~2155 tokens) on every call and
    # is redundant given the per-part instructions.
    base = _re.sub(
        r"## Briefing structure.*?(?=^## |\Z)", "", base, count=1, flags=_FLAGS,
    )

    if cfg is not None:
        used = _recently_used_asides(cfg)
        if used:
            avoid_line = " | ".join(f'"{p}"' for p in used)
            base = base.rstrip() + (
                "\n\n### Recently used asides — DO NOT reuse in today's briefing\n\n"
                "The following asides already appeared in Jeeves's briefings over "
                f"the last {ASIDES_RECENT_WINDOW_DAYS} days. Pick different phrases "
                "from the full pool above — same thematic matching rules apply, "
                "just a different word choice:\n\n"
                f"{avoid_line}\n"
            )

    return base.rstrip() + "\n"


def generate_briefing(
    cfg: Config,
    session: SessionModel,
    *,
    max_tokens: int = 8192,
) -> str:
    """Render the briefing in EIGHT Groq calls and stitch the HTML.

    Free-tier Groq `on_demand` is 12k TPM on llama-3.3-70b. The full system
    prompt (persona + rules + all seven sector descriptions + full profane-
    aside pool + coverage-log + output rules) is ~11.7k chars on its own,
    leaving no headroom for a rich session in one call. Splitting into 8
    narrow calls keeps each request's system + user payload comfortably
    under the limit.

    Each call gets a *different* random sample of the profane-aside pool so
    Jeeves doesn't default to the same 3 phrases every day.

    Sleep 65s between calls to let Groq's rolling 60s TPM window clear.
    Total wall-clock: ~9 minutes.
    """

    import time

    payload = _trim_session_for_prompt(session)
    # Build the per-call base system prompt once; all 8 calls see the same
    # "recently used — DO NOT reuse" appendix so Jeeves avoids yesterday's
    # favorites consistently across parts.
    base_system = _system_prompt_for_parts(cfg)

    parts: list[str] = []
    for i, (label, sectors) in enumerate(PART_PLAN):
        if i > 0:
            log.info("sleeping 65s before %s (TPM window cooldown)", label)
            time.sleep(65)
        part_payload = _session_subset(payload, sectors)
        part_system = base_system + PART_INSTRUCTIONS_BY_NAME[label]
        part_user = build_user_prompt_from_payload(part_payload)
        parts.append(
            _invoke_groq(cfg, part_system, part_user, max_tokens=max_tokens, label=label)
        )

    stitched = _stitch_parts(*parts)
    log.info(
        "stitched briefing: %d chars across %d parts (%s)",
        len(stitched), len(parts), ", ".join(str(len(p)) for p in parts),
    )
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
    """Find an existing COVERAGE_LOG comment or synthesize one from the HTML anchors."""

    m = COVERAGE_LOG_RE.search(html)
    if m:
        try:
            coverage = json.loads(m.group(1))
            if isinstance(coverage, list):
                return html, coverage
        except json.JSONDecodeError:
            log.warning("COVERAGE_LOG JSON invalid; rebuilding.")

    # No valid log — synthesize from anchor tags present in the body.
    synthesized = _synthesize_coverage_log(html, session)
    comment = f"<!-- COVERAGE_LOG: {json.dumps(synthesized, ensure_ascii=False)} -->"
    if "<!-- COVERAGE_LOG_PLACEHOLDER -->" in html:
        html = html.replace("<!-- COVERAGE_LOG_PLACEHOLDER -->", comment)
    elif "</body>" in html:
        html = html.replace("</body>", f"{comment}\n</body>")
    else:
        html = html.rstrip() + "\n" + comment + "\n"
    return html, synthesized


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
            f"Good morning, Sir. The weather: {session.weather or 'unremarkable'}. "
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
                f"{session.newyorker.text[:2000]} ...<a href=\"{session.newyorker.url}\">[Read at The New Yorker]</a>",
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
    body {{ font-family: Georgia, serif; background: #faf9f6; color: #1a1a1a; margin: 0; padding: 20px; }}
    .container {{ max-width: 720px; margin: 0 auto; line-height: 1.7; }}
  </style>
</head>
<body>
<div class="container">
  <h1>📜 Daily Intelligence from Jeeves — DRY RUN</h1>
  {body_html}
  <div class="signoff"><p>Your reluctantly faithful Butler,<br/>Jeeves</p></div>
  <!-- COVERAGE_LOG: {json.dumps(coverage, ensure_ascii=False)} -->
</div>
</body>
</html>"""
