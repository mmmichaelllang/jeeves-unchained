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


# -- Three-call render (free-tier 12k TPM ceiling) --
#
# llama-3.3-70b on Groq's free `on_demand` tier caps at 12k TPM, which a full
# session blows through in one shot. We split the render into THREE sequential
# Groq calls grouped by the write_system.md sector layout, then stitch the
# HTML. Each call stays comfortably under 11k tokens.

PART1_SECTORS = [
    "correspondence", "weather", "local_news", "career", "family",
]
PART2_SECTORS = [
    "global_news", "intellectual_journals", "enriched_articles",
]
PART3_SECTORS = [
    "triadic_ontology", "ai_systems", "uap", "wearable_ai",
    "vault_insight", "newyorker",
]

PART1_INSTRUCTIONS = """

---

## PART 1 of 3 — render instructions

You are writing PART 1 of a three-part briefing. Output the full HTML opening
(`<!DOCTYPE html>` through `<body>` and the `<div class="container">` wrapper), the
`<h1>` with today's full weekday date, and then ONLY these sectors in order:

- Sector 1 — The Domestic Sphere (correspondence, weather, municipal local_news, public safety local_news)
- Sector 2 — The Domestic Calendar (career, family)

Aim for ~1,700 words across these two sectors, 2 profane asides (remaining parts add 3 more).

When Sector 2 is complete, emit the literal comment `<!-- PART1 END -->` and STOP.
Do NOT write any subsequent sectors. Do NOT write the sign-off. Do NOT write the coverage log.
Do NOT close `</div>`, `</body>`, or `</html>`. Parts 2 and 3 will continue the document.
"""

PART2_INSTRUCTIONS = """

---

## PART 2 of 3 — render instructions

You are CONTINUING a briefing. Part 1 rendered the opening, `<h1>`, and Sectors 1-2.
You do NOT rewrite any of that.

Output ONLY Sector 3 — The Intellectual Currents (global_news, intellectual_journals,
enriched_articles). Raw HTML paragraphs only. NO `<!DOCTYPE html>`, NO `<head>`, NO
`<body>`, NO new `<h1>`.

Aim for ~1,700 words, 1-2 profane asides.

When Sector 3 is complete, emit the literal comment `<!-- PART2 END -->` and STOP.
Do NOT write Sectors 4-7. Do NOT write the sign-off. Do NOT close any outer tags. Part 3 will continue.
"""

PART3_INSTRUCTIONS = """

---

## PART 3 of 3 — render instructions

You are CONTINUING a briefing. Parts 1 and 2 rendered Sectors 1-3. You do NOT rewrite
any of that.

Output ONLY the following, as raw HTML paragraphs (NO `<!DOCTYPE html>`, NO `<head>`,
NO `<body>`, NO new `<h1>`):

- Sector 4 — Specific Enquiries (triadic_ontology, ai_systems, uap)
- Sector 5 — Wearable Intelligence (wearable_ai)
- Sector 6 — From the Library Stacks (vault_insight, only if `vault_insight.available === true`)
- Sector 7 — Talk of the Town (newyorker, only if `newyorker.available === true`; must be LAST)

Aim for ~1,700 words, 1-2 profane asides.

After Sector 7 (or Sector 5 if 6 and 7 are unavailable), emit the closing signoff block:

```html
<div class="closing">
  <p>Your reluctantly faithful Butler,<br/>Jeeves</p>
</div>
<!-- COVERAGE_LOG_PLACEHOLDER -->
</div>
</body>
</html>
```

The `<!-- COVERAGE_LOG_PLACEHOLDER -->` is intentional — the post-processor fills it
by scanning anchor tags across the full stitched document.
"""


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


def _system_prompt_for_parts() -> str:
    """Base system prompt with the "## HTML scaffold" section stripped.

    Each part has its own scaffold instructions (in PART1_INSTRUCTIONS /
    PART2_INSTRUCTIONS) so the base scaffold block is dead weight in the
    two-call path — and trimming it gives us token headroom on each call.
    """
    import re as _re

    base = load_write_system_prompt()
    return _re.sub(
        r"## HTML scaffold.*?(?=## |\Z)",
        "",
        base,
        count=1,
        flags=_re.DOTALL,
    ).rstrip() + "\n"


def generate_briefing(
    cfg: Config,
    session: SessionModel,
    *,
    max_tokens: int = 8192,
) -> str:
    """Render the briefing in three Groq calls and stitch the HTML.

    Free-tier Groq `on_demand` is 12k TPM on llama-3.3-70b. A rich session
    exceeds that in one call, so we split into three sequential calls grouped
    by the write_system.md sector layout. We sleep 65 seconds between calls
    so the rolling 60-second TPM window clears.
    """

    import time

    base_system = _system_prompt_for_parts()
    payload = _trim_session_for_prompt(session)

    parts: list[str] = []
    for i, (sectors, instructions, label) in enumerate(
        [
            (PART1_SECTORS, PART1_INSTRUCTIONS, "part1"),
            (PART2_SECTORS, PART2_INSTRUCTIONS, "part2"),
            (PART3_SECTORS, PART3_INSTRUCTIONS, "part3"),
        ]
    ):
        if i > 0:
            log.info("sleeping 65s before %s (TPM window cooldown)", label)
            time.sleep(65)
        part_payload = _session_subset(payload, sectors)
        part_system = base_system + instructions
        part_user = build_user_prompt_from_payload(part_payload)
        parts.append(
            _invoke_groq(cfg, part_system, part_user, max_tokens=max_tokens, label=label)
        )

    stitched = _stitch_parts(*parts)
    log.info(
        "stitched briefing: %d chars (parts: %s)",
        len(stitched), ", ".join(str(len(p)) for p in parts),
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
