"""Phase 2 — per-sector research runner.

Each sector gets its own FunctionAgent with a fresh 131k Kimi context window.
The driver loops sectors sequentially, collects per-sector JSON output, and
assembles the final SessionModel. This replaces the single-agent design that
couldn't cover all sectors before the shared context overflowed.

Design notes:
- No `emit_session` terminator. Each sector's agent just returns a JSON string
  as its final message; FunctionAgent stops when the LLM stops calling tools.
- `enriched_articles` runs last and is seeded with URLs surfaced by prior
  sectors, so the extraction pass targets what actually appeared in coverage.
- Dedup accumulates as sectors complete so later-run sectors don't re-fetch
  the same URLs in the same session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Config

log = logging.getLogger(__name__)


def _classify_api_error(exc: Exception) -> str:
    """Return 'rate_limit', 'auth', 'timeout', 'server', or 'unknown'."""
    msg = str(exc).lower()
    if any(k in msg for k in ("429", "rate limit", "rate_limit", "too many")):
        return "rate_limit"
    if any(k in msg for k in ("401", "403", "unauthorized", "forbidden", "api key")):
        return "auth"
    if any(k in msg for k in ("timeout", "timed out", "connection", "peer closed")):
        return "timeout"
    if any(k in msg for k in ("500", "502", "503", "504", "internal server")):
        return "server"
    return "unknown"


@dataclass
class SectorSpec:
    name: str
    shape: str  # one of: "string", "list", "dict", "deep", "newyorker", "enriched"
    instruction: str
    default: Any


SECTOR_SPECS: list[SectorSpec] = [
    SectorSpec(
        name="weather",
        shape="string",
        instruction=(
            "Today's weather for Edmonds, Washington (47.81, -122.38). "
            "Dispatch ALL THREE of these in parallel — do not wait for one before starting the others:\n"
            "1. serper_search(query='Edmonds WA weather today forecast', tbs='qdr:d')\n"
            "2. tavily_search(query='weather forecast Edmonds Washington 98020 today')\n"
            "3. gemini_grounded_synthesize(question='What is the current weather forecast for "
            "Edmonds, Washington 98020 today? Include conditions, temperature, precipitation "
            "chance, wind, and evening outlook.')\n"
            "Synthesize the best data from whichever searches succeed. "
            "Return a single plain-text string (no JSON wrapper), ~300-600 chars, covering "
            "conditions, precipitation, temps, comfort.\n"
            "\nFALLBACK CHAIN — work through these in order if the primary trio returns "
            "empty or insufficient data:\n"
            "  4. exa_search(query='Edmonds WA weather forecast today', "
            "search_type='fast', num_results=3) — exa often indexes NWS/Weather.gov pages.\n"
            "  5. tavily_search(query='weather forecast Edmonds Washington "
            "site:weather.gov OR site:forecast.weather.gov OR site:wunderground.com')\n"
            "  6. serper_search(query='Seattle area weather forecast today site:weather.gov',"
            " tbs='qdr:d') — Seattle metro forecast is always close enough.\n"
            "CRITICAL: Do NOT return an empty string under any circumstances. "
            "If all six steps fail, return a narrative estimate: "
            "'Forecast unavailable; typical for this date in Edmonds: partly cloudy, "
            "mid-50s to low 60s, chance of afternoon drizzle, westerly winds 5-10 mph.' "
            "Note it clearly as an estimate."
        ),
        default="",
    ),
    SectorSpec(
        name="local_news",
        shape="list",
        instruction=(
            "Local news for Edmonds, Snohomish County, and Seattle. Cover two subcategories: "
            "'municipal' (council, schools, transit, local policy) and 'public_safety'. "
            "Public safety GEOFENCE: 3 miles from (47.810652, -122.377355). Only homicides, "
            "major assaults, armed incidents, missing persons. Reject petty crime. "
            "\n\nPRIMARY SEARCHES — dispatch ALL of these in parallel:\n"
            "1. serper_search(query='Edmonds WA news', tbs='qdr:d') — last 24h.\n"
            "2. serper_search(query='Edmonds Washington city council OR permit OR "
            "development', tbs='qdr:w') — last 7 days for municipal.\n"
            "3. tavily_search(query='Edmonds WA local news today', max_results=8).\n"
            "4. gemini_grounded_synthesize(question='What is the latest local news in "
            "Edmonds, Washington today? Include any city council, public safety, development, "
            "or school district news.') — synthesises across sources.\n"
            "5. exa_search(query='Edmonds Washington news', search_type='fast', "
            "num_results=5) — run in parallel with the others; exa indexes local "
            "outlets (myedmondsnews.com, heraldnet.com) well.\n"
            "For each story you plan to include, call tavily_extract on the article URL "
            "to read the actual content — do not write findings from a headline alone. "
            "Return a JSON array of objects: [{category, source, findings, urls}, ...]. "
            "\nNEVER RETURN AN EMPTY ARRAY. If Edmonds-specific results are thin after "
            "all five searches above, work through this fallback chain in order:\n"
            "  a) tavily_search(query='Snohomish County Washington local news this week', "
            "max_results=5) — broader county scope via tavily.\n"
            "  b) serper_search(query='Shoreline OR Lynnwood OR Mountlake Terrace news "
            "today') — immediate neighbours.\n"
            "  c) exa_search(query='Snohomish County news myedmondsnews heraldnet', "
            "search_type='fast', num_results=5) — exa fallback on named outlets.\n"
            "  d) serper_search(query='Snohomish County news site:heraldnet.com', "
            "tbs='qdr:w') — site-restricted serper.\n"
            "  e) Return the most recent minor municipal item you found — even a city "
            "     commission meeting notice — as category='municipal'. Quiet news days "
            "     deserve one honest line, not an empty array that breaks the briefing."
        ),
        default=[],
    ),
    SectorSpec(
        name="career",
        shape="dict",
        instruction=(
            "High-school English or History teacher jobs within ~30 miles of Edmonds, WA. "
            "Districts to scan: Edmonds, Shoreline, Mukilteo, Everett, Northshore, Lake "
            "Washington, Bellevue, Snohomish, Marysville, Monroe, Lake Stevens, Renton, "
            "Highline, Mercer Island, Issaquah, Riverview, Tukwila, Seattle Public Schools. "
            "Use tavily_search or serper_search for district HR pages and job boards. "
            "For each opening, read the posting page via tavily_extract to pull the "
            "application deadline and salary range if listed. "
            "Return a JSON object: "
            "{openings: [{district, role, url, summary, deadline, salary_range}, ...], "
            "notes: '...'}. "
            "Use null for deadline or salary_range if not found in the posting."
        ),
        default={},
    ),
    SectorSpec(
        name="family",
        shape="dict",
        instruction=(
            "Two subkeys. 'choir': Seattle/Puget Sound choral auditions. 'toddler': "
            "Edmonds/Lynnwood activities for a 2-year-old.\n\n"
            "MANDATORY FIRST STEP — dispatch ALL THREE in parallel right now:\n"
            "1. serper_search(query='Seattle choral ensemble choir auditions open 2026', "
            "tbs='qdr:m')\n"
            "2. serper_search(query='Edmonds Lynnwood library storytime toddler activities "
            "May 2026')\n"
            "3. exa_search(query='Seattle Pro Musica Northwest Chorale choral auditions 2026', "
            "search_type='fast', num_results=3)\n"
            "Organisations to check for choir: Seattle Choral Company, Seattle Pro Musica, "
            "Northwest Chorale, Choral Arts NW, Pacific Lutheran Univ Choirs. "
            "Toddler venues: Edmonds Library (Main St), Lynnwood Library, Sno-Isle system, "
            "Imagine Children's Museum. "
            "Return {choir: 'findings string', toddler: 'findings string', urls: [...]}."
        ),
        default={},
    ),
    SectorSpec(
        name="global_news",
        shape="list",
        instruction=(
            "Global news, today. Sources: BBC, CNN, Al Jazeera, The Guardian, NPR, "
            "Memeorandum, NYT, Reuters, AP. "
            "\n\nPRIMARY SEARCHES — dispatch ALL of these in parallel:\n"
            "1. serper_search(query='world news today', tbs='qdr:d', num=10).\n"
            "2. tavily_search(query='top global news stories today', max_results=8).\n"
            "3. exa_search(query='BBC Guardian Reuters breaking news today', "
            "search_type='fast', num_results=5) — run alongside 1 and 2; exa returns "
            "full article text, reducing the need for a separate tavily_extract pass.\n"
            "4. gemini_grounded_synthesize(question='What are the 5 most significant "
            "global news stories right now? Include geopolitics, economics, and major "
            "international events. Cite specific sources.') — critical for comprehensive "
            "coverage.\n"
            "5. vertex_grounded_search(question='Latest breaking world news today — top "
            "stories from BBC, Guardian, Reuters, Al Jazeera.') — grounded search "
            "fallback if gemini returns thin results.\n"
            "\nIMPORTANT — ongoing stories: if prior_urls already contains URLs for a "
            "major ongoing story (e.g. a war, a trade dispute), do NOT skip the story. "
            "Instead, search specifically for TODAY'S NEW DEVELOPMENT: "
            "serper_search(query='[story name] latest update today', tbs='qdr:d'). "
            "A new URL about the same story is NOT a duplicate — cover the new development. "
            "After ranking your top 4-8 stories, call tavily_extract on those article URLs "
            "that exa did NOT already return full text for (batch up to 5 per call) to read "
            "actual content before writing findings. Never summarise from a headline alone. "
            "\nSOURCE DIVERSITY — your final output MUST include at least one item whose "
            "source is BBC, The Guardian, Al Jazeera, NPR, or AP. If the initial searches "
            "return only Reuters or only Gemini-synthesised results, run a follow-up: "
            "exa_search(query='BBC Guardian Al Jazeera news today', search_type='fast', "
            "num_results=4, text_max_chars=3000) and incorporate the best result.\n"
            "\nGEMINI REDIRECT URLS — gemini_grounded_synthesize returns "
            "vertexaisearch.cloud.google.com citation URLs that expire within hours. "
            "Do NOT put these in the urls array. For each Gemini-sourced finding, "
            "find the real article URL with: serper_search(query='[headline] site:bbc.com "
            "OR site:theguardian.com OR site:reuters.com OR site:apnews.com', tbs='qdr:d') "
            "and use that canonical URL instead. If no real URL is found, omit the item "
            "from your output rather than citing an ephemeral redirect.\n"
            "Return a JSON array of {category, source, findings, urls}. "
            "\nNEVER RETURN AN EMPTY ARRAY. If the primary searches all return thin or "
            "empty results, work through this fallback chain:\n"
            "  a) exa_search(query='world news today', search_type='fast', "
            "num_results=8) — wider exa sweep.\n"
            "  b) tavily_search(query='breaking international news today', "
            "max_results=6) — different tavily query.\n"
            "  c) serper_search(query='international news today BBC Reuters "
            "Guardian', tbs='qdr:d') — direct source targeting.\n"
            "  d) exa_search(query='New York Times Reuters AP news today', "
            "search_type='fast', num_results=5) — named-outlet exa pass.\n"
            "If all fallbacks fail, return the single best headline you encountered "
            "in any earlier search — include the URL and what was visible in the snippet. "
            "An empty global_news array breaks the briefing; a thin single-item array "
            "does not."
        ),
        default=[],
    ),
    SectorSpec(
        name="intellectual_journals",
        shape="list",
        instruction=(
            "Long-form intellectual journals: NYRB, New Yorker (NOT Talk of the Town), "
            "Aeon, Marginalian, Kottke, ProPublica, The Intercept, Scientific American, "
            "LRB, Arts & Letters Daily, Big Think, Jacobin, OpenSecrets.\n\n"
            "MANDATORY FIRST STEP — dispatch ALL THREE in parallel right now:\n"
            "1. exa_search(query='LRB London Review of Books Aeon essay 2026', "
            "search_type='auto', num_results=3, text_max_chars=4000)\n"
            "2. exa_search(query='NYRB New York Review of Books ProPublica Intercept "
            "long read 2026', search_type='auto', num_results=3, text_max_chars=4000)\n"
            "3. exa_search(query='Marginalian Big Think Scientific American Jacobin "
            "essay 2026', search_type='auto', num_results=2, text_max_chars=4000)\n\n"
            "From the results, select 4-5 articles. DIVERSITY RULE: at least 3 different "
            "source publications must appear in your final output — do not return all items "
            "from the same journal. Prioritise articles not in prior_urls.\n"
            "Read the full text returned by exa for each chosen article — do not summarise "
            "from the title or dek alone. Write findings from the body.\n"
            "DOMAIN-ANCHOR FALLBACK (use if the three parallel searches above return fewer "
            "than 3 distinct publications — these are known-good anchor domains):\n"
            "  4. exa_search(query='long-form essay 2025 2026', search_type='fast', "
            "num_results=4, text_max_chars=4000, "
            "include_domains=['aeon.co', 'lrb.co.uk', 'nybooks.com', 'themarginalia.com'])\n"
            "  5. serper_search(query='Jacobin ProPublica Intercept OpenSecrets new article "
            "this week', tbs='qdr:w') — then tavily_extract on top 2 results.\n"
            "  6. exa_search(query='Artforum N+1 Jewish Currents Dissent essay 2026', "
            "search_type='fast', num_results=3, text_max_chars=4000)\n"
            "Return a JSON array of {source, findings, urls}."
        ),
        default=[],
    ),
    SectorSpec(
        name="wearable_ai",
        shape="list",
        instruction=(
            "Three subsections. 'ai_voice_hardware': voice-first AI hardware (Friend, Tab, "
            "Pi-style pendants, AI Pin-like devices). 'teacher_ai_tools': EdTech AI for "
            "high-school English and History teachers (MagicSchool, Diffit, Brisk, etc.). "
            "'wearable_devices': lifelogging pendants, pins, smart glasses. "
            "Use exa_search (returns full text) or tavily_extract after serper for each "
            "device/tool you include — read the actual product page or article, not just "
            "the headline. Return a JSON array of {category, findings, urls}, one entry "
            "per subsection."
        ),
        default=[],
    ),
    SectorSpec(
        name="triadic_ontology",
        shape="deep",
        instruction=(
            "Deep research: relational ontologies, triadic logic, quantum perichoresis, "
            "non-linear triadic dynamics, trinitarianism in contemporary metaphysics. "
            "IMMEDIATE FIRST ACTION — call this tool right now, before any reasoning:\n"
            "  exa_search(query='triadic ontology relational metaphysics 2025 2026', "
            "search_type='auto', num_results=3, text_max_chars=3000)\n"
            "Then call a second search if needed:\n"
            "  exa_search(query='quantum perichoresis trinitarian philosophy new paper', "
            "search_type='auto', num_results=2, text_max_chars=3000)\n"
            "IMPORTANT: the same series (e.g. Karl-Alber 'Studies on Triadic "
            "Ontology') may appear in prior coverage. Prefer to find the NEXT uncovered "
            "volume, paper, or author — check prior_urls and avoid repeating what is there. "
            "Begin your findings prose with the specific TITLE and AUTHOR of each paper or "
            "volume discussed so that covered-headline matching works correctly. "
            "CRITICAL: 'findings' MUST be a single prose string (500-1000 chars), NOT an "
            "array or list. Return exactly: {\"findings\": \"<prose>\", \"urls\": [...]}."
        ),
        default={"findings": "", "urls": []},
    ),
    SectorSpec(
        name="ai_systems",
        shape="deep",
        instruction=(
            "Deep research: multi-agent research systems, reasoning models, autonomous "
            "research pipelines, prompt-engineering advances. "
            "IMMEDIATE FIRST ACTION — call this tool right now, before any reasoning:\n"
            "  exa_search(query='multi-agent AI research systems autonomous pipeline 2026', "
            "search_type='auto', num_results=3, text_max_chars=3000)\n"
            "Then optionally:\n"
            "  exa_search(query='reasoning model prompt engineering advances 2025 2026', "
            "search_type='auto', num_results=2, text_max_chars=3000)\n"
            "CRITICAL: 'findings' MUST be a single prose string (500-1000 chars), NOT an "
            "array or list. Return exactly: {\"findings\": \"<prose string>\", \"urls\": [...]}. "
            "Do not put an array in the findings field."
        ),
        default={"findings": "", "urls": []},
    ),
    SectorSpec(
        name="uap",
        shape="deep",
        instruction=(
            "Deep research: UAP disclosure, congressional hearings, non-human intelligence "
            "declassification. Recent developments only. "
            "CRITICAL: 'findings' MUST be a single prose string (≤250 words), NOT an array "
            "or list. Return exactly: {\"findings\": \"<prose string>\", \"urls\": [...]}."
        ),
        default={"findings": "", "urls": []},
    ),
    SectorSpec(
        name="newyorker",
        shape="newyorker",
        instruction=(
            "Call fetch_new_yorker_talk_of_the_town() exactly once. It returns "
            "{available, title, section, dek, byline, date, text, url, source}. "
            "Return that result verbatim as a JSON object. If available=false, return the object as-is."
        ),
        default={"available": False, "title": "", "section": "", "dek": "",
                 "byline": "", "date": "",
                 "text": "", "url": "", "source": "The New Yorker"},
    ),
    SectorSpec(
        name="enriched_articles",
        shape="enriched",
        instruction=(
            "You'll receive a list of candidate URLs from today's sectors. "
            "Pick the 5 most novel and globally significant — PRIORITY ORDER:\n"
            "  1. Global/international news (Reuters, BBC, Guardian, AP, etc.)\n"
            "  2. Long-form intellectual journal articles (NYRB, Aeon, LRB, etc.)\n"
            "  3. Wearable AI / tech product pages\n"
            "  4. UAP / triadic ontology / AI systems sources\n"
            "  5. Edmonds local news (lowest priority — already well-covered)\n"
            "IMPORTANT — Reuters blocks direct fetches with 401. Before picking Reuters URLs, "
            "prefer alternative sources covering the same story (BBC, Guardian, AP, Al Jazeera). "
            "Call tavily_extract on your 5 chosen URLs in ONE batch call. "
            "Fall back to fetch_article_text for any Tavily refuses. "
            "If a URL fails (401, 403, timeout, or fetch_failed=true in the result), DO NOT "
            "include it — immediately replace it with the next best candidate from the priority "
            "list and call tavily_extract again on the replacement. "
            "Your final array must have 5 entries with fetch_failed=false. "
            "Return a JSON array of {url, source, title, fetch_failed, text} — one entry "
            "per extracted URL. For the 'text' field include ONLY the first 500 characters "
            "of extracted content — do not paste full article text into the JSON."
        ),
        default=[],
    ),
    SectorSpec(
        name="literary_pick",
        shape="literary_pick",
        instruction=(
            "Research one book published between 2004 and 2024 that is considered by many "
            "critics and readers to be a current classic or a plausible future canonical work "
            "of literary fiction or non-fiction. "
            "Use exa_search with a query like: "
            "\"literary fiction nonfiction 2004 2024 future classic canonical critically acclaimed\". "
            "Do NOT pick a title already in dedup.covered_headlines. Vary the selection day-over-day. "
            "Return: {\"available\": true, \"title\": \"...\", \"author\": \"...\", "
            "\"year\": NNNN, \"summary\": \"...\", \"url\": \"...\"}. "
            "If no suitable result is found, return {\"available\": false}."
        ),
        default={"available": False, "title": "", "author": "", "year": None,
                 "summary": "", "url": ""},
    ),
]


CONTEXT_HEADER = """You are researching ONE sector of Mister Michael Lang's daily briefing.

Context:
- Date: {date} (UTC). Treat as authoritative.
- One week ago: {seven_days_ago} (use this as a freshness floor — see below).
- Location: Edmonds, Washington (47.810652, -122.377355).
- Household: Mister Michael Lang, Mrs. Sarah Lang (wife, music teacher, choral),
  Piper (2-year-old daughter).

Prior coverage URLs (already briefed, do not revisit):
{prior_urls_sample}

Dedup guidance: if you encounter any URL in the prior list above, skip it.
Do not fabricate sources; every URL you include must come from a tool response.
{story_continuity}
**FRESHNESS WINDOW — MANDATORY for non-breaking sectors:**
Default search providers favour evergreen high-authority pages. Without a
freshness filter, the same articles re-rank into top results day after day,
producing repetitive briefings. For every search call, bias toward content
published in the last 7 days:
  - serper_search: pass tbs='qdr:w' (last 7 days) or tbs='qdr:d' (last 24h
    for breaking).
  - tavily_search: pass time_range='week' (or 'day' for breaking).
  - exa_search: pass start_published_date='{seven_days_ago}'.
Override this rule ONLY when a sector instruction explicitly asks for
open-ended results (e.g., literary_pick covers 2004–2024). For all other
sectors, queries without a freshness parameter are considered defective.

**SOURCE-ROTATION GUIDANCE — read carefully:**
The user has explicitly asked: when an article from a given source has been
covered yesterday, prefer the next-most-relevant article from THAT SAME source
today, not a different source. Apply this rule when a sector hits 4+ candidate
articles from the same publisher: keep one (the most relevant), and prefer
articles from publishers NOT yet in `prior_urls_sample` for the rest. Do not
repeatedly cite an article that you can see (by URL or headline match) is
already in the prior coverage.

**MANDATORY FIRST STEP — search before you write:**
Your training-data knowledge is STALE. You MUST call at least one search tool
and receive live results before writing any findings. Do NOT output your final
JSON until you have called a search tool in this session. Any output that
includes no URLs from tool responses will be discarded as hallucinated.

**CRITICAL — read before you write:**
Do not write findings based on headlines or snippets alone. For every article
you plan to include in your output:
- If found via exa_search: the result already contains full text — use it.
- If found via serper_search or tavily_search: call tavily_extract on the
  URL (batch up to 5 URLs per call) to read the actual content before writing
  your findings for it. A summary based on a headline is not a summary.
- fetch_article_text is a fallback for URLs tavily_extract cannot reach.
Write findings only from content you have actually read, not guessed.

Research discipline — complete at least TWO rounds before writing your output:
  Round 1 (search): dispatch 2-4 search tools in parallel.
  Round 2 (read): call tavily_extract on top results that exa did NOT already
  return full text for (batch up to 5 URLs per call); OR run a second targeted
  search to fill coverage gaps.
Only after Round 2 should you write the final JSON. A single search round
followed immediately by output is shallow research and produces a thin
briefing. Aim for 6-10 total tool calls. Do NOT stop early.
{quota_summary}
SECTOR: {sector_name}
INSTRUCTION: {instruction}

When you have enough findings, STOP calling tools and output JSON matching the
instruction's shape. No markdown fences. No prose before or after the JSON.
For a string-shape sector, output the raw string (no quotes)."""


def _python_repr_to_json(s: str) -> str:
    """Convert Python repr-style tokens to JSON equivalents.

    Kimi occasionally returns ``{'key': 'value', 'flag': True}`` because
    LlamaIndex's ``str(dict)`` conversion produces Python repr rather than
    JSON (single quotes, capitalised booleans/None).  This is the most
    common cause of JSONDecodeError in production.
    """
    s = re.sub(r"\bTrue\b", "true", s)
    s = re.sub(r"\bFalse\b", "false", s)
    s = re.sub(r"\bNone\b", "null", s)
    # Replace single-quoted strings with double-quoted ones.
    # The regex handles the common case where values don't contain internal
    # apostrophes.  It does NOT handle "it's" inside a single-quoted string —
    # those rare cases fall through to the LLM repair retry.
    s = re.sub(r"'([^'\\]*(?:\\.[^'\\]*)*)'", r'"\1"', s)
    return s


def _remove_trailing_commas(s: str) -> str:
    """Strip trailing commas before closing brackets/braces.

    Some models (and truncated streams) leave a trailing comma on the last
    element: ``[{"a": 1},]`` or ``{"k": "v",}``.  Standard ``json.loads``
    rejects these.
    """
    return re.sub(r",\s*([}\]])", r"\1", s)


def _recover_truncated_array(s: str) -> str | None:
    """Salvage complete items from a NIM stream-truncated JSON array.

    When NIM drops the streaming connection mid-response the JSON array is
    left open: ``[{"a":"x"},{"b":"y``.  We find the last complete ``}`` and
    close the array there, preserving every fully-received item.

    Returns the repaired string, or None if no salvageable content was found.
    """
    if not s.lstrip().startswith("["):
        return None
    last_close = s.rfind("}")
    if last_close < 0:
        return None
    candidate = s[: last_close + 1].rstrip().rstrip(",")
    return candidate + "]"


def _try_normalize_json(fragment: str, *, is_array: bool) -> Any | None:
    """Apply deterministic lightweight normalizations to fix common JSON errors.

    Tries a progressive sequence of cheap transforms so that the vast majority
    of real-world parse failures are resolved without an extra LLM call:

    0. Single-object-to-array coercion (checked FIRST when is_array=True) —
       if the shape expects a list but the model returned a bare ``{...}``
       object, wrap it immediately before any other attempt so earlier steps
       don't accidentally return a dict.
    1. Python repr → JSON  (``True``/``False``/``None``, single-quoted keys)
    2. Trailing-comma removal
    3. Combinations of 1 + 2
    4. Truncation recovery (arrays only) — salvage complete items from a
       mid-stream-dropped response, then re-apply repr + comma fixes

    Returns the parsed Python value on the first successful attempt, or None
    if all normalizations fail (caller should escalate to LLM repair retry).
    """
    def _try(s: str) -> Any | None:
        try:
            return json.loads(s)
        except (json.JSONDecodeError, ValueError):
            return None

    # 0. Bare-object-to-array coercion — must run BEFORE steps 1-3 so a valid
    #    JSON object doesn't get returned as a dict when the shape wants a list.
    if is_array and fragment.strip().startswith("{"):
        wrapped = f"[{fragment.strip()}]"
        if (v := _try(wrapped)) is not None:
            log.debug("_try_normalize_json: pass 'bare_obj_to_array' succeeded.")
            return v
        if (v := _try(_remove_trailing_commas(_python_repr_to_json(wrapped)))) is not None:
            log.debug("_try_normalize_json: pass 'bare_obj_to_array+repr+comma' succeeded.")
            return v

    # 1. Python repr conversion alone.
    repr_fixed = _python_repr_to_json(fragment)
    if (v := _try(repr_fixed)) is not None:
        log.debug("_try_normalize_json: pass 'python_repr' succeeded.")
        return v

    # 2. Trailing commas alone.
    comma_fixed = _remove_trailing_commas(fragment)
    if (v := _try(comma_fixed)) is not None:
        log.debug("_try_normalize_json: pass 'trailing_comma' succeeded.")
        return v

    # 3. Both combined.
    both_fixed = _remove_trailing_commas(repr_fixed)
    if (v := _try(both_fixed)) is not None:
        log.debug("_try_normalize_json: pass 'python_repr+trailing_comma' succeeded.")
        return v

    # 4. Truncation recovery (arrays only).
    if is_array:
        recovered = _recover_truncated_array(fragment)
        if recovered:
            if (v := _try(recovered)) is not None:
                log.debug("_try_normalize_json: pass 'truncation_recovery' succeeded.")
                return v
            # Also try repr + comma fixes on the recovered fragment.
            if (v := _try(_remove_trailing_commas(_python_repr_to_json(recovered)))) is not None:
                log.debug("_try_normalize_json: pass 'truncation_recovery+repr+comma' succeeded.")
                return v

    return None


class _ParseFailed:
    """Sentinel: _parse_sector_output could not extract valid JSON from the output.

    Distinct from spec.default so run_sector can trigger a JSON repair retry
    rather than silently returning an empty section.
    """
    __slots__ = ("raw",)

    def __init__(self, raw: str) -> None:
        self.raw = raw

    def __repr__(self) -> str:
        return f"_ParseFailed(raw_len={len(self.raw)})"


def _parse_sector_output(raw: str, spec: SectorSpec) -> Any:
    """Coerce the agent's final text into the sector-shape value.

    Returns a :class:`_ParseFailed` instance (not spec.default) when the
    output is present but malformed or missing a JSON token — this signals
    run_sector to attempt a JSON repair retry rather than silently dropping
    the section.
    """

    text = (raw or "").strip()
    log.debug("sector %s: _parse_sector_output raw=%d chars, shape=%s.", spec.name, len(text), spec.shape)
    # Strip common markdown fences.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()

    if spec.shape == "string":
        log.debug("sector %s: string shape — returning %d chars.", spec.name, len(text))
        return text

    # Find the outermost JSON token.
    if spec.shape in ("list", "enriched"):
        start, end = text.find("["), text.rfind("]")
    else:
        start, end = text.find("{"), text.rfind("}")

    _is_array = spec.shape in ("list", "enriched")

    if start < 0 or end <= start:
        # No bracket found — try deterministic normalizations on the full text
        # before escalating to LLM repair (e.g. truncated array with no `]`).
        fixed = _try_normalize_json(text, is_array=_is_array)
        if fixed is not None:
            log.info("sector %s: no JSON bracket found but deterministic repair succeeded.", spec.name)
            parsed = fixed
        else:
            log.warning(
                "sector %s: no JSON %s found in output; will attempt repair retry",
                spec.name, "array" if _is_array else "object",
            )
            return _ParseFailed(raw or "")
    else:
        fragment = text[start : end + 1]
        try:
            parsed = json.loads(fragment)
            log.debug("sector %s: JSON extracted cleanly (%d chars).", spec.name, len(fragment))
        except json.JSONDecodeError as e:
            # Try deterministic normalizations first — cheaper than an LLM call.
            fixed = _try_normalize_json(fragment, is_array=_is_array)
            if fixed is not None:
                log.info(
                    "sector %s: JSON parse error (%s) repaired deterministically.", spec.name, e
                )
                parsed = fixed
            else:
                log.warning(
                    "sector %s: JSON parse failed: %s; will attempt repair retry", spec.name, e
                )
                return _ParseFailed(raw or "")

    # For enriched sectors, enforce the 500-char text cap regardless of model
    # compliance — avoids bloated session JSON and downstream NIM context issues.
    if spec.shape == "enriched" and isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict) and isinstance(entry.get("text"), str):
                if len(entry["text"]) > 500:
                    entry["text"] = entry["text"][:500]

    # For list sectors, drop any item that carries a "urls" key but has an
    # empty list — that is the fingerprint of Kimi answering from training
    # data without calling any search tool.  An uncited finding is worse
    # than no finding: it will reach the write phase without a source link.
    if spec.shape == "list" and isinstance(parsed, list):
        cleaned = [
            item for item in parsed
            if not (isinstance(item, dict) and "urls" in item and not item["urls"])
        ]
        dropped = len(parsed) - len(cleaned)
        if dropped:
            log.warning(
                "sector %s: dropped %d uncited item(s) with empty urls — "
                "Kimi likely answered from training data without searching.",
                spec.name, dropped,
            )
        parsed = cleaned if cleaned else spec.default

    # Quality filter (inspired by preset-URL pipelines that gate on content
    # richness before accepting a result): drop list items whose "findings"
    # string is non-empty but trivially short (< 20 chars after strip).
    # These are noise entries — "N/A", "No results.", truncated model outputs —
    # that add nothing to the briefing and confuse the write phase.
    # Guard: never empty the array; if all items would be dropped, keep them.
    if spec.shape == "list" and isinstance(parsed, list):
        quality_filtered = [
            item for item in parsed
            if not (
                isinstance(item, dict)
                and "findings" in item
                and item["findings"].strip()
                and len(item["findings"].strip()) < 20
            )
        ]
        if quality_filtered and len(quality_filtered) < len(parsed):
            log.warning(
                "sector %s: quality filter dropped %d item(s) with trivially "
                "short findings (< 20 chars).",
                spec.name, len(parsed) - len(quality_filtered),
            )
            parsed = quality_filtered

    # For deep sectors, if urls is empty the findings are almost certainly
    # from training data.  Return the default so write phase gets nothing
    # rather than stale or fabricated research.
    if spec.shape == "deep" and isinstance(parsed, dict):
        if not parsed.get("urls"):
            log.warning(
                "sector %s: deep sector has no cited URLs — discarding findings.",
                spec.name,
            )
            return spec.default

    return parsed


def _build_user_prompt(
    spec: SectorSpec,
    run_date: str,
    prior_urls_sample: list[str],
    extra: str = "",
    *,
    quota_summary: str = "",
    story_continuity: str = "",
    prior_sources_by_host: dict[str, list[str]] | None = None,
) -> str:
    prior_block = "\n".join(prior_urls_sample) if prior_urls_sample else "(none)"
    quota_block = (
        f"\n**Provider quota remaining:** {quota_summary}\n"
        if quota_summary
        else ""
    )
    continuity_block = (
        f"\n**Story continuity (prior briefings):**\n{story_continuity}\n"
        if story_continuity
        else ""
    )
    # Freshness floor for the FRESHNESS WINDOW directive in CONTEXT_HEADER.
    # Computed from run_date so the prompt's date arithmetic stays correct
    # even when the pipeline runs with --date for a backfill.
    try:
        from datetime import date as _date, timedelta as _timedelta

        _rd = _date.fromisoformat(run_date)
        seven_days_ago = (_rd - _timedelta(days=7)).isoformat()
    except Exception:
        seven_days_ago = run_date  # fallback: same date — still better than no anchor
    # Source-rotation block: per-host titles cited in prior briefings.
    # Cap to 30 hosts × 3 titles each so the prompt stays under the TPM ceiling.
    sources_block = ""
    if prior_sources_by_host:
        rows: list[str] = []
        for host, titles in list(prior_sources_by_host.items())[:30]:
            if not titles:
                continue
            sample = "; ".join(titles[:3])
            rows.append(f"- {host}: {sample}")
        if rows:
            sources_block = (
                "\n**Source-rotation hints** (host → titles already covered):\n"
                + "\n".join(rows)
                + "\n\nFor any host listed above, prefer a DIFFERENT article from "
                "that same host today; do not re-cite the listed titles.\n"
            )
    base = CONTEXT_HEADER.format(
        date=run_date,
        seven_days_ago=seven_days_ago,
        prior_urls_sample=prior_block,
        sector_name=spec.name,
        instruction=spec.instruction,
        quota_summary=quota_block,
        story_continuity=continuity_block,
    )
    if sources_block:
        base = base + sources_block
    return f"{base}\n\n{extra}" if extra else base


def _quota_snapshot(ledger) -> dict[str, int]:
    """Snapshot per-provider used-count for change detection."""
    state = ledger._state.get("providers", {})
    return {name: d.get("used", 0) for name, d in state.items()}


def _quota_increased(before: dict[str, int], ledger) -> bool:
    """True if any search provider recorded new calls since the snapshot."""
    state = ledger._state.get("providers", {})
    return any(d.get("used", 0) > before.get(name, 0) for name, d in state.items())


def _is_retryable_network_error(exc: Exception) -> bool:
    """True for transient NIM streaming errors (peer drop, timeout, reset)."""
    msg = str(exc).lower()
    return any(phrase in msg for phrase in (
        "peer closed connection",
        "incomplete chunked read",
        "connection reset",
        "read timeout",
        "server disconnected",
    ))


def _is_nim_rate_limit(exc: Exception) -> bool:
    """True when NIM responded 429 Too Many Requests."""
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


# Sectors whose agents call non-quota tools (fetch_new_yorker_talk_of_the_town
# or fetch_article_text) — skip the quota-increment check for these.
_NO_QUOTA_CHECK = frozenset({"newyorker"})

# Fallback exa queries for deep sectors when the quota guard fires (Kimi answered
# from training data without calling any search tool).
_DEEP_FALLBACK_QUERIES: dict[str, str] = {
    "triadic_ontology": "triadic ontology relational metaphysics 2025 2026",
    "ai_systems": "multi-agent AI research autonomous pipeline reasoning model 2026",
    "uap": "UAP disclosure congressional hearing non-human intelligence 2026",
}


async def _deep_sector_forced_retry(
    cfg: Config,
    spec: SectorSpec,
    prior_urls_sample: list[str],
    ledger,
    sector_max_tokens: int,
) -> Any:
    """One forced-search retry for deep sectors where the quota guard fired.

    Kimi sometimes answers triadic_ontology / ai_systems from training data
    without calling any search tool, triggering the quota guard. This retry
    uses a stripped-down system + user prompt that gives Kimi no room to
    reason before calling exa_search, preventing the training-data bypass.
    """
    from llama_index.core.agent.workflow import FunctionAgent

    from .llm import build_kimi_llm
    from .tools import all_search_tools

    query = _DEEP_FALLBACK_QUERIES.get(spec.name, f"{spec.name.replace('_', ' ')} research 2026")
    forced_system = (
        "Your ONLY job: call exa_search IMMEDIATELY, then return JSON. "
        "No reasoning. No preamble. Your first response MUST be a tool call."
    )
    forced_user = (
        f"Call exa_search right now with these exact parameters:\n"
        f"  query='{query}'\n"
        f"  search_type='auto'\n"
        f"  num_results=3\n"
        f"  text_max_chars=3000\n\n"
        f"After you get results, return ONLY this JSON object (no markdown, no preamble):\n"
        f'  {{"findings": "<single prose string, 300-600 chars summarising the results>", '
        f'"urls": [<url strings from exa results>]}}'
    )

    pre_quota = _quota_snapshot(ledger)
    tools_r = all_search_tools(cfg, ledger, set(prior_urls_sample))
    llm_r = build_kimi_llm(cfg, max_tokens=sector_max_tokens)
    agent_r = FunctionAgent(
        tools=tools_r, llm=llm_r,
        system_prompt=forced_system,
        verbose=cfg.verbose,
    )

    log.info("sector %s: attempting forced-search retry.", spec.name)
    try:
        response_r = await agent_r.run(forced_user)
    except Exception as exc:
        log.warning("sector %s: forced retry crashed (%s); returning default", spec.name, exc)
        return spec.default

    if not _quota_increased(pre_quota, ledger):
        log.warning(
            "sector %s: forced retry also skipped search tools; returning default.", spec.name
        )
        return spec.default

    raw_r = str(response_r)
    result_r = _parse_sector_output(raw_r, spec)
    if isinstance(result_r, _ParseFailed):
        log.warning("sector %s: forced retry also produced malformed JSON; returning default.", spec.name)
        return spec.default
    log.info(
        "sector %s: forced retry succeeded — parsed %s",
        spec.name, type(result_r).__name__,
    )
    return result_r


# JSON schema hints for the repair retry — tells Kimi the exact structure to emit
# so it doesn't have to infer it from the malformed original output.
_REPAIR_SHAPE_HINT: dict[str, str] = {
    "list": '[{"category": "...", "source": "...", "findings": "...", "urls": ["..."]}]',
    "enriched": '[{"title": "...", "url": "...", "source": "...", "text": "..."}]',
    "dict": '{"findings": "...", "urls": ["..."]}',
    "deep": '{"findings": "...", "urls": ["..."]}',
    "newyorker": '{"available": true, "title": "...", "section": "...", "dek": "...", "byline": "...", "date": "...", "text": "...", "url": "...", "source": "The New Yorker"}',
    "literary_pick": '{"available": true, "title": "...", "author": "...", "year": 2020, "summary": "...", "url": "..."}',
}


async def _json_repair_retry(
    cfg: Config,
    spec: SectorSpec,
    failed: _ParseFailed,
    ledger,
    sector_max_tokens: int,
) -> Any:
    """Repair-retry for sectors where the main run produced malformed or missing JSON.

    Two cases:
    - *Malformed JSON* (``failed.raw`` is non-empty): send the raw output back
      to a fresh minimal agent and ask it to reformat as valid JSON with no
      additional reasoning.
    - *Empty output* (``failed.raw`` is empty — e.g., enriched_articles where
      all tool calls had None id/name so no JSON was ever emitted): ask Kimi to
      produce the JSON directly from the sector instruction without any tool
      calls.  This is a last-resort measure — the output won't have live search
      data, but it is vastly preferable to an empty section in the briefing.

    Uses a no-tools FunctionAgent (empty tool list) so Kimi is forced to
    produce JSON immediately rather than looping through tool calls again.
    """
    from llama_index.core.agent.workflow import FunctionAgent

    from .llm import build_kimi_llm

    shape_hint = _REPAIR_SHAPE_HINT.get(spec.shape, '{"findings": "...", "urls": ["..."]}')
    bracket_open = "[" if spec.shape in ("list", "enriched") else "{"
    bracket_close = "]" if spec.shape in ("list", "enriched") else "}"

    repair_system = (
        "You are a JSON repair assistant. Your ONLY output must be valid JSON. "
        "No markdown fences. No prose before or after. No explanations. "
        f"The output must start with `{bracket_open}` and end with `{bracket_close}`."
    )

    if failed.raw.strip():
        # Malformed JSON — ask Kimi to reformat what it already produced.
        repair_user = (
            f"The following text is the output of a research agent for sector '{spec.name}'.\n"
            f"It contains useful findings but the JSON is malformed or improperly formatted.\n\n"
            f"RAW OUTPUT (may be truncated or contain single-quoted keys):\n"
            f"---\n{failed.raw[:4000]}\n---\n\n"
            f"Reformat the content above as valid JSON matching this shape:\n{shape_hint}\n\n"
            f"Rules:\n"
            f"- Use double quotes for all keys and string values.\n"
            f"- Extract as many items/findings as you can from the raw text above.\n"
            f"- Do NOT add new findings — only reformat what is already there.\n"
            f"- Output ONLY the JSON. Nothing else."
        )
        log.info("sector %s: repair retry — reformatting malformed output (%d chars).", spec.name, len(failed.raw))
    else:
        # Empty output — produce JSON directly from the sector instruction.
        repair_user = (
            f"The research agent for sector '{spec.name}' produced no output "
            f"(all tool calls failed). You must produce the best possible JSON "
            f"output for this sector based on your knowledge.\n\n"
            f"SECTOR INSTRUCTION:\n{spec.instruction[:2000]}\n\n"
            f"Return valid JSON matching this shape:\n{shape_hint}\n\n"
            f"Rules:\n"
            f"- Use double quotes for all keys and string values.\n"
            f"- If you lack specific knowledge, use plausible placeholder text rather than empty strings.\n"
            f"- Output ONLY the JSON. Nothing else."
        )
        log.info("sector %s: repair retry — generating JSON from empty output.", spec.name)

    llm_r = build_kimi_llm(cfg, max_tokens=sector_max_tokens)
    agent_r = FunctionAgent(
        tools=[],
        llm=llm_r,
        system_prompt=repair_system,
        verbose=cfg.verbose,
    )

    try:
        response_r = await agent_r.run(repair_user)
    except Exception as exc:
        log.warning("sector %s: repair retry crashed (%s); returning default.", spec.name, exc)
        return spec.default

    raw_r = str(response_r)
    if not raw_r or not raw_r.strip():
        log.warning(
            "sector %s: repair retry LLM returned empty/None response; returning default.",
            spec.name,
        )
        return spec.default
    result_r = _parse_sector_output(raw_r, spec)
    if isinstance(result_r, _ParseFailed):
        log.warning("sector %s: repair retry also produced malformed JSON; returning default.", spec.name)
        return spec.default

    log.info(
        "sector %s: repair retry succeeded — parsed %s",
        spec.name, type(result_r).__name__,
    )
    return result_r


async def run_sector(
    cfg: Config,
    spec: SectorSpec,
    prior_urls_sample: list[str],
    ledger,
    *,
    extra_user: str = "",
    quota_summary: str = "",
    story_continuity: str = "",
    prior_sources_by_host: dict[str, list[str]] | None = None,
) -> Any:
    """Run one sector's agent and return the parsed sector-shape value."""

    # Fast path: newyorker bypasses the LLM agent entirely.
    # fetch_talk_of_the_town is pure Python — no LLM needed or wanted.
    # Routing it through Kimi introduces three hallucination vectors:
    #   1. Kimi can skip the tool call and answer from training data
    #      (quota guard doesn't fire — newyorker is in _NO_QUOTA_CHECK).
    #   2. _json_repair_retry "empty output" path explicitly tells Kimi to
    #      synthesise JSON from its own knowledge.
    #   3. Kimi may call search tools instead of the TOTT tool and fabricate content.
    # Calling the function directly guarantees real fetched content or available=false.
    if spec.name == "newyorker":
        from .tools.talk_of_the_town import fetch_talk_of_the_town

        log.info("sector newyorker: direct Python fetch (no agent).")
        try:
            raw = fetch_talk_of_the_town(
                set(prior_urls_sample), jina_api_key=cfg.jina_api_key
            )()
        except Exception as exc:
            log.warning("sector newyorker: fetch crashed (%s); returning default.", exc)
            return spec.default
        parsed = _parse_sector_output(raw, spec)
        if isinstance(parsed, _ParseFailed):
            log.warning("sector newyorker: JSON parse failed; returning default.")
            return spec.default
        log.info(
            "sector newyorker: direct fetch done (available=%s).",
            parsed.get("available") if isinstance(parsed, dict) else "?",
        )
        return parsed

    from llama_index.core.agent.workflow import FunctionAgent

    from .llm import build_kimi_llm
    from .tools import all_search_tools

    # Each sector gets its own agent, LLM, and tool instances so no state
    # leaks across runs (the quota ledger is the only shared object and is
    # inherently cumulative).
    #
    # Deep sectors read full article text (up to 2000 chars × 3-5 results) and
    # synthesise it into prose findings.  Kimi's chain-of-thought over that
    # input can exceed 4000 tokens, making NIM drop the streaming connection
    # mid-response ("peer closed connection").  Halving max_tokens keeps each
    # streaming response shorter and faster, preventing the drop.
    #
    # Enriched sectors emit a compact JSON array (5 entries × ≤500-char text).
    # Despite the instruction, Kimi often includes full article texts, bloating
    # the output to 3000+ tokens and causing a 4+ minute NIM response that
    # eventually truncates mid-JSON.  Capping at 2048 limits response time;
    # _parse_sector_output enforces the 500-char text limit after parse.
    _deep_max_tokens = 4096
    _enriched_max_tokens = 2048
    sector_max_tokens = (
        _deep_max_tokens if spec.shape == "deep"
        else _enriched_max_tokens if spec.shape == "enriched"
        else 8192
    )

    tools = all_search_tools(cfg, ledger, set(prior_urls_sample))
    llm = build_kimi_llm(cfg, max_tokens=sector_max_tokens)

    user_msg = _build_user_prompt(
        spec, cfg.run_date.isoformat(), prior_urls_sample, extra_user,
        quota_summary=quota_summary, story_continuity=story_continuity,
        prior_sources_by_host=prior_sources_by_host,
    )
    _system_prompt = (
        "You are the per-sector research agent for Jeeves. "
        "CRITICAL: Your internal training-data knowledge is considered STALE "
        "and must NOT be used as a source of findings. You MUST call at least "
        "one search tool (serper_search, tavily_search, exa_search, or "
        "gemini_grounded_synthesize) and receive live results before writing "
        "any findings. Output that contains no URLs returned by tools in this "
        "session will be rejected as hallucinated.\n\n"
        "PROVIDER SELECTION — pick the cheapest tool that fits:\n"
        "  breaking/local/time-filtered   → serper_search (tbs='qdr:d' for last 24h)\n"
        "  intellectual/long-form/similar → exa_search\n"
        "  multi-source synthesized answer → tavily_search\n"
        "  narrative 'state of X' question → gemini_grounded_synthesize\n"
        "  article full-text after ranking → tavily_extract (preferred) or fetch_article_text\n"
        "  JS-heavy / paywall (last resort) → playwright_extract (only if both above fail)\n\n"
        "EMPTY SECTOR PROTOCOL — if after 2 searches a sector yields zero usable results:\n"
        "  return an empty array/object for that sector plus the key _empty_reason with a\n"
        "  short explanation (e.g. 'no_results_after_2_searches'). Do NOT fabricate sources.\n\n"
        "Follow the user's instruction exactly, then return ONLY the requested "
        "JSON (or raw string for string-shape)."
    )
    agent = FunctionAgent(
        tools=tools,
        llm=llm,
        system_prompt=_system_prompt,
        verbose=cfg.verbose,
    )

    pre_quota = _quota_snapshot(ledger)

    log.info("sector %s: agent starting.", spec.name)
    response = None
    # Two separate retry budgets:
    #   - Network drops (peer closed, timeout): 3 retries at 10/30/60s
    #   - NIM 429 rate limit: 2 retries at 60/120s (longer window to clear quota)
    _net_delays = [10, 30, 60]
    _ratelimit_delays = [60, 120]
    net_attempts = 0
    rl_attempts = 0
    last_exc: Exception | None = None
    for _loop_guard in range(20):  # hard cap prevents infinite loop
        try:
            if response is None and net_attempts == 0 and rl_attempts == 0:
                response = await agent.run(user_msg)
            else:
                # Rebuild agent with fresh instances for every retry so no
                # stale streaming state leaks from the previous crashed connection.
                tools_r = all_search_tools(cfg, ledger, set(prior_urls_sample))
                llm_r = build_kimi_llm(cfg, max_tokens=sector_max_tokens)
                agent_r = FunctionAgent(
                    tools=tools_r, llm=llm_r,
                    system_prompt=_system_prompt,
                    verbose=cfg.verbose,
                )
                response = await agent_r.run(user_msg)
            break  # success — exit retry loop
        except Exception as e:
            last_exc = e
            if _is_nim_rate_limit(e):
                if rl_attempts >= len(_ratelimit_delays):
                    log.warning(
                        "sector %s: NIM 429 on all %d rate-limit retries (%s); "
                        "returning default.",
                        spec.name, rl_attempts + 1, e,
                    )
                    return spec.default
                delay = _ratelimit_delays[rl_attempts]
                log.warning(
                    "sector %s: NIM 429 rate-limit (attempt %d) — sleeping %ds.",
                    spec.name, rl_attempts + 1, delay,
                )
                await asyncio.sleep(delay)
                rl_attempts += 1
            elif _is_retryable_network_error(e):
                if net_attempts >= len(_net_delays):
                    log.warning(
                        "sector %s: network error on all %d retries (%s); "
                        "returning default.",
                        spec.name, net_attempts + 1, e,
                    )
                    return spec.default
                delay = _net_delays[net_attempts]
                log.warning(
                    "sector %s: transient network error (attempt %d, %s) — "
                    "retrying in %ds.",
                    spec.name, net_attempts + 1, e, delay,
                )
                await asyncio.sleep(delay)
                net_attempts += 1
            else:
                log.warning("sector %s: agent crashed (%s); returning default", spec.name, e)
                return spec.default
    else:
        log.warning("sector %s: retry loop guard triggered; returning default.", spec.name)
        return spec.default

    # Guard: if no search-provider quota moved, Kimi answered entirely from
    # training data without calling any external tools.  For deep sectors, try
    # one forced-search retry before giving up.  For all others, return default
    # so the write phase never sees hallucinated findings.
    if spec.name not in _NO_QUOTA_CHECK and not _quota_increased(pre_quota, ledger):
        if spec.shape == "deep":
            log.warning(
                "sector %s: no search provider called — attempting forced-search retry.",
                spec.name,
            )
            return await _deep_sector_forced_retry(
                cfg, spec, prior_urls_sample, ledger, sector_max_tokens
            )
        log.warning(
            "sector %s: no search provider was called — output likely hallucinated; "
            "returning default.",
            spec.name,
        )
        return spec.default

    if response is None:
        log.warning("sector %s: agent.run() returned None; attempting repair retry.", spec.name)
        return await _json_repair_retry(
            cfg, spec, _ParseFailed(""), ledger, sector_max_tokens
        )

    raw = str(response)
    parsed = _parse_sector_output(raw, spec)

    # If the output was present but malformed (or completely absent due to
    # degenerate None/None tool calls), attempt a repair retry before giving up.
    # The repair agent has no tools — it either reformats the raw output as valid
    # JSON or, when raw is empty, synthesises a best-effort JSON from its own
    # knowledge.  Either outcome is vastly preferable to silently dropping the
    # section from the briefing.
    if isinstance(parsed, _ParseFailed):
        log.warning(
            "sector %s: triggering JSON repair retry (raw_len=%d).",
            spec.name, len(parsed.raw),
        )
        parsed = await _json_repair_retry(cfg, spec, parsed, ledger, sector_max_tokens)

    log.info(
        "sector %s: parsed %s (len=%s)",
        spec.name, type(parsed).__name__,
        len(parsed) if hasattr(parsed, "__len__") else "-",
    )
    return parsed


# Domains that produce ephemeral redirect URLs (Google grounding API, Vertex AI
# search) rather than canonical article URLs.  These must be excluded from the
# covered_urls dedup set and from the enriched_articles seed — they expire
# quickly, yield 404s on re-visit, and pollute the rolling prior-URL window.
_REDIRECT_ARTIFACT_HOSTS = frozenset({
    "vertexaisearch.cloud.google.com",
    "search.app.goo.gl",
})


def _is_redirect_artifact(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        return urlparse(url).netloc in _REDIRECT_ARTIFACT_HOSTS
    except Exception:
        return False


def collect_urls_from_sector(value: Any) -> list[str]:
    """Best-effort URL extraction for dedup accumulation + enriched_articles seeding.

    Redirect-artifact URLs (Gemini grounding API, Vertex search) are excluded
    so they never pollute the rolling covered_urls window.
    """

    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(collect_urls_from_sector(item))
    elif isinstance(value, dict):
        for k, v in value.items():
            if k == "urls" and isinstance(v, list):
                out.extend(
                    str(u) for u in v
                    if u and not _is_redirect_artifact(str(u))
                )
            elif k == "url" and isinstance(v, str) and v and not _is_redirect_artifact(v):
                out.append(v)
            else:
                out.extend(collect_urls_from_sector(v))
    return out


_HEADLINE_KEYS = {"title", "headline", "subject", "role", "event", "district"}

# String-valued keys treated like "findings" — first sentence extracted for dedup.
# Covers the family shape {choir: '...', toddler: '...'} which has no "findings" key.
_FINDINGS_LIKE_KEYS = {"findings", "choir", "toddler"}


def _first_sentence(text: str, max_chars: int = 250) -> str:
    """Extract a short dedup-usable label from a findings string.

    Slices at the first sentence-ending punctuation that lands within
    max_chars, or truncates at max_chars if no such punctuation exists.
    Default raised from 150 → 250 so titles/headlines aren't truncated
    mid-phrase, which previously broke cross-sector matching.
    """
    text = text.strip()
    for end in (".", "!", "?", ";"):
        i = text.find(end)
        if 0 < i < max_chars:
            return text[: i + 1].strip()
    return text[:max_chars].strip()


def _first_two_sentences(text: str, max_chars: int = 300) -> str:
    """Extract up to the first two sentences from a findings string.

    Used for ``findings`` entries in news/deep sectors where the first
    sentence often gives only a topic header and the second carries the
    distinguishing detail (title/author/place) that makes cross-day dedup
    actually catch repeats.
    """
    text = text.strip()
    if not text:
        return ""
    end_chars = (".", "!", "?", ";")
    first_end = -1
    for end in end_chars:
        i = text.find(end)
        if 0 < i and (first_end == -1 or i < first_end):
            first_end = i
    if first_end == -1:
        return text[:max_chars].strip()
    second_end = -1
    for end in end_chars:
        i = text.find(end, first_end + 1)
        if 0 < i and (second_end == -1 or i < second_end):
            second_end = i
    if second_end == -1 or second_end >= max_chars:
        # Only one sentence fits within the budget.
        return text[: first_end + 1].strip()
    return text[: second_end + 1].strip()


def collect_headlines_from_sector(value: Any) -> list[str]:
    """Pull human-facing labels out of a sector's parsed JSON for day-over-day dedup.

    Extracts both explicit headline-keyed fields (title, headline, role, etc.)
    AND the first sentence of any ``findings`` string — the latter is critical
    for news/deep sectors whose Finding objects carry no title field.
    """

    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(collect_headlines_from_sector(item))
    elif isinstance(value, dict):
        for k, v in value.items():
            if k in _HEADLINE_KEYS and isinstance(v, str) and v.strip():
                out.append(v.strip())
            elif k in _FINDINGS_LIKE_KEYS and isinstance(v, str) and v.strip():
                # Two sentences: first often a topic header ("AI policy update."),
                # second carries the distinguishing title/place/author needed for
                # cross-day dedup matching.
                sentence = _first_two_sentences(v)
                if sentence:
                    out.append(sentence)
            elif isinstance(v, (dict, list)):
                out.extend(collect_headlines_from_sector(v))
    return out


# Sector fields scanned for cross-sector URL collisions. The same article
# regularly surfaces in 2-3 of these (e.g. a ProPublica feature lands in
# global_news, intellectual_journals, AND enriched_articles). Recording
# the collision in the session lets the write phase synthesise once
# rather than repeat across 3 sections.
_CROSS_SECTOR_FIELDS = (
    "local_news",
    "global_news",
    "intellectual_journals",
    "wearable_ai",
    "enriched_articles",
)


def _find_cross_sector_dupes(session: dict) -> list[str]:
    """Return URLs that appear in 2+ research sectors.

    The write phase reads ``session.dedup.cross_sector_dupes`` and treats
    those URLs as already-covered after their first appearance — preventing
    the same story from being narrated three times under different headers.
    Order is the order in which dupes were discovered (stable-ish; based on
    the iteration order of sector → item → urls).
    """

    url_to_sectors: dict[str, list[str]] = {}
    for field in _CROSS_SECTOR_FIELDS:
        items = session.get(field) or []
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            urls = item.get("urls") or []
            if not isinstance(urls, list):
                continue
            for url in urls:
                if not url or not isinstance(url, str):
                    continue
                url_to_sectors.setdefault(url, []).append(field)

    dupes: list[str] = []
    seen: set[str] = set()
    for url, sectors in url_to_sectors.items():
        if len(sectors) > 1 and url not in seen:
            seen.add(url)
            dupes.append(url)
    return dupes


def extract_correspondence_references(handoff_text: str) -> list[str]:
    """Pull `email | <sender>` identifiers out of a correspondence handoff
    text so tomorrow's research sees which threads were cited and Jeeves can
    skim-vs-skip based on that.

    Input format is one line per message from `build_handoff_text`:
        - [escalation] Sarah Lang: picks up milk, confirms...
    """

    refs: list[str] = []
    for line in (handoff_text or "").splitlines():
        line = line.strip().lstrip("- ").strip()
        if not line or not line.startswith("["):
            continue
        # `[classification] Sender Name: summary`
        try:
            after_class = line.split("]", 1)[1].strip()
            sender = after_class.split(":", 1)[0].strip()
        except IndexError:
            continue
        if sender:
            refs.append(f"email | {sender}")
    return refs
