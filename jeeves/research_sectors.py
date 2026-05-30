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
from collections.abc import Iterable
from typing import Any

from .config import Config

log = logging.getLogger(__name__)


# Map FunctionTool.name -> the QuotaLedger provider key it bills against.
# Tools missing from this map are never excluded by quota guard (e.g. local
# functions like fetch_article_text don't bill against an external quota).
_TOOL_TO_QUOTA_PROVIDER: dict[str, str] = {
    "serper_search": "serper",
    "tavily_search": "tavily",
    "tavily_extract": "tavily",
    "exa_search": "exa",
    "gemini_grounded_synthesize": "gemini",
    "vertex_grounded_search": "gemini",
    "jina_search": "jina_search",
    "jina_deepsearch": "jina_deepsearch",
    "jina_rerank": "jina_rerank",
    "tinyfish_extract": "tinyfish",
    "tinyfish_search": "tinyfish_search",
    "playwright_search": "playwright_search",
    "playwright_extract": "playwright",
    "stealth_extract": "stealth",
}


def _apply_quota_aware_exclusion(tools, ledger):
    """Drop tools whose provider is at >=95% of monthly cap.

    Gated by JEEVES_USE_QUOTA_AWARE_EXCLUSION=1 — default off so existing
    behaviour is unchanged until the user opts in.

    Threshold bumped 0.85 → 0.95 on 2026-05-21: at 85% we were dropping
    providers with ~150 calls of headroom left, sometimes mid-day, which
    forced the sector through more-expensive fallbacks before the cap was
    actually breached. 95% leaves only ~50 calls of headroom (per 1000-cap
    tavily) — close enough to imminent overage to matter, far enough to
    not over-fire on normal daily fluctuation.

    Returns the (possibly filtered) tool list. Never empties the list —
    if every tool would be excluded, returns the original list and logs
    a warning (better an over-budget agent than no agent).
    """
    import os as _os

    if _os.environ.get("JEEVES_USE_QUOTA_AWARE_EXCLUSION", "").strip() != "1":
        return tools

    threshold = float(_os.environ.get("JEEVES_QUOTA_EXCLUSION_THRESHOLD", "0.95"))
    kept = []
    dropped = []
    for t in tools:
        name = getattr(t.metadata, "name", "")
        provider = _TOOL_TO_QUOTA_PROVIDER.get(name)
        if provider is None:
            kept.append(t)
            continue
        try:
            state = ledger._state["providers"].get(provider, {})
            cap = state.get("free_cap", 0) or 0
            used = state.get("used", 0) or 0
            if cap > 0 and (used / cap) >= threshold:
                dropped.append(f"{name}({provider}:{used}/{cap})")
                continue
        except Exception:
            pass
        kept.append(t)

    if not kept:
        log.warning(
            "quota-aware exclusion dropped ALL tools — falling back to full list. "
            "Dropped: %s", dropped,
        )
        return tools

    if dropped:
        log.info("quota-aware exclusion dropped %d tools: %s", len(dropped), dropped)
    return kept


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
    # Optional per-sector tool allowlist. When set AND
    # JEEVES_PER_SECTOR_TOOLS=1, the agent is given only these tools,
    # saving ~1k tokens per sector by dropping unused tool descriptions.
    # When unset OR the env flag is off, the full toolbox is provided
    # (back-compat). Each entry is the registered FunctionTool name
    # (e.g. "serper_search", "tavily_extract").
    tools: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Tool-allowlist bundles (2026-05-21) — composed into per-sector tools tuples.
#
# Tools not currently registered (canaries behind unset env flags) are
# silently skipped by tools_for_sector — listing them here is safe and
# means the sector's toolbox automatically picks them up the moment their
# flag is flipped, with no code change required.
# ---------------------------------------------------------------------------

# Generic web search — every news/topic sector wants these.
_TOOLS_WEB_SEARCH = (
    "serper_search",
    "tavily_search",
    "exa_search",
    "jina_search",          # canary; picked up when JEEVES_USE_JINA_SEARCH=1
    "tinyfish_search",      # canary
    "playwright_search",    # canary
)

# Grounded synthesis — narrative "state of X" answers.
_TOOLS_GROUNDED = (
    "gemini_grounded_synthesize",
    "vertex_grounded_search",
    "jina_deepsearch",      # canary; deep multi-hop
)

# Full-text extraction tier — the article-body fetchers.
_TOOLS_EXTRACT = (
    "tavily_extract",
    "fetch_article_text",
    "playwright_extract",
    "tinyfish_extract",     # canary
)


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
        # weather: 3-tier parallel + 3-tier fallback all web-search shape.
        # No body-extraction needed — synthesized answer is the deliverable.
        tools=_TOOLS_WEB_SEARCH + ("gemini_grounded_synthesize",),
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
        # local_news: heavy use of search + extract; needs grounded synthesis too.
        tools=_TOOLS_WEB_SEARCH + _TOOLS_GROUNDED + _TOOLS_EXTRACT,
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
        # career: district HR pages + extraction. No grounded synth needed.
        tools=_TOOLS_WEB_SEARCH + _TOOLS_EXTRACT,
    ),
    SectorSpec(
        name="english_lesson_plans",
        shape="dict",
        instruction=(
            "High-school English/Language-Arts lesson plans, classroom-management "
            "strategies, and digital token-economy systems Mister Lang (teacher "
            "candidate) can adapt or steal from. Two subkeys:\n"
            "  - 'classroom_ready': complete, freely-available lesson plans, unit "
            "    rubrics, discussion-question sets, OR concrete classroom-management "
            "    /token-economy mechanics. Acceptable anchors include common HS "
            "    texts (The Great Gatsby, Macbeth, Their Eyes Were Watching God, "
            "    1984, Beloved, Frankenstein, A Raisin in the Sun) or HS skills "
            "    (close reading, argumentative essay, rhetorical analysis, Socratic "
            "    seminar) AS WELL AS classroom-management plays (silent-discussion "
            "    protocols, late-work policies, restorative-circle scripts, "
            "    points/badges/leaderboards, group-work norms).\n"
            "  - 'pedagogy_pieces': short essays or community threads on HS English "
            "    pedagogy or classroom management that shipped recently.\n\n"
            "PRIORITY SOURCE LIST (2026-05-10 — these are the highest-yield sites "
            "for the niche the user actually wants; not exclusive — pick whichever "
            "publishes the genuinely-best item this week):\n"
            "  - reddit.com/r/ELATeachers, reddit.com/r/Teachers, "
            "    reddit.com/r/ClassroomManagement (use site:reddit.com/r/<sub> "
            "    queries; live community trade-craft is the gold-standard signal)\n"
            "  - GitHub Education, github.com search 'high-school English curriculum' "
            "    repos, learn-static.github.io / github.com/learn-static, individual "
            "    teacher-author repos that publish unit plans as Markdown\n"
            "  - edutopia.org/community, edutopia.org articles on classroom management\n"
            "  - shakeuplearning.com (Kasey Bell's Shake Up Learning blog — Google-"
            "    classroom + EdTech how-tos)\n"
            "  - cultofpedagogy.com (Jennifer Gonzalez)\n"
            "  - liveschool.io (LiveSchool digital token economy — points, behaviour "
            "    tracking, parent communication)\n"
            "  - classroomzen.com (Classroom Zen — mindfulness + management)\n"
            "  - edugems.io (EduGems — gamified classroom rewards)\n"
            "  - publish.obsidian.md ecosystem (search 'Obsidian Publish English "
            "    teacher' or specific pubs known to publish ELA notes)\n"
            "  - Then conventional anchors: Folger Shakespeare Library, "
            "    ReadWriteThink, Stanford History Education Group, NCTE / English "
            "    Journal, Facing History and Ourselves, ASCD, Common Sense "
            "    Education.\n\n"
            "MANDATORY FIRST STEP — dispatch FIVE searches in parallel right now:\n"
            "1. serper_search(query='site:reddit.com/r/ELATeachers OR site:reddit.com/r/"
            "Teachers OR site:reddit.com/r/ClassroomManagement lesson plan OR token "
            "economy OR classroom management 2026', tbs='qdr:m', num=10)\n"
            "2. serper_search(query='LiveSchool OR Cult of Pedagogy OR Shake Up "
            "Learning OR Classroom Zen OR EduGems classroom management token economy "
            "2026', tbs='qdr:m', num=10)\n"
            "3. serper_search(query='site:github.com OR site:github.io high school "
            "English curriculum OR ELA unit plan OR classroom management', "
            "tbs='qdr:y', num=10)\n"
            "4. serper_search(query='site:publish.obsidian.md English teacher OR "
            "ELA OR classroom', num=10) — explore the Obsidian Publish ecosystem.\n"
            "5. exa_search(query='high school English language arts lesson plan OR "
            "classroom token economy OR digital points 2026', search_type='auto', "
            "num_results=5, text_max_chars=4000)\n"
            "If you find Reddit threads, USE tavily_extract to read the actual "
            "thread content (top comments often contain the actual lesson plan or "
            "management play). Do not summarise from titles. For GitHub repos, "
            "tavily_extract the README.\n"
            "MANDATORY DEDUP RULE: any URL in `prior_urls` MUST be filtered out. "
            "If all top hits are already covered, run another search with a NARROWER "
            "query (e.g. a specific text, skill, or management technique not yet "
            "covered) until at least one URL per subkey is new. Returning a "
            "previously-covered URL is a hard failure.\n"
            "DIVERSITY RULE: at least 3 distinct hosts must appear across "
            "classroom_ready + pedagogy_pieces. Do not return 4 items all from one "
            "blog. Mix Reddit/GitHub/Obsidian-Publish with the conventional anchors.\n"
            "Return a JSON object: "
            "{classroom_ready: [{title, source, url, grade_band, topic, summary}, ...], "
            "pedagogy_pieces: [{title, source, url, summary}, ...], "
            "notes: '...'}. "
            "Aim for 3-5 classroom_ready items and 1-2 pedagogy_pieces. "
            "If the field is genuinely thin, return an empty array for the thin "
            "subkey rather than padding with off-topic results."
        ),
        default={"classroom_ready": [], "pedagogy_pieces": [], "notes": ""},
        # english_lesson_plans: site-scoped searches + heavy extract (Reddit
        # threads, GitHub READMEs). No grounded synth.
        tools=_TOOLS_WEB_SEARCH + _TOOLS_EXTRACT,
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
        # family: search-only — no extract needed for events / auditions
        # (the search snippet usually carries the audition date + venue).
        tools=_TOOLS_WEB_SEARCH + ("tavily_extract", "fetch_article_text"),
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
        # global_news: full toolbox — search, grounded synth, extract,
        # vertex_grounded fallback all explicitly invoked.
        tools=_TOOLS_WEB_SEARCH + _TOOLS_GROUNDED + _TOOLS_EXTRACT,
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
            "from the same journal.\n\n"
            "MANDATORY DEDUP RULE — read carefully:\n"
            "  After every search, FILTER OUT any URL that already appears in "
            "  `prior_urls`. Do NOT include a URL that is in `prior_urls`. The "
            "  following essays have shipped REPEATEDLY across the last week and "
            "  are HARD FAILURES if returned again:\n"
            "  - aeon.co/essays/the-wests-forgotten-republican-heritage "
            "    (Sean Irving on republican heritage)\n"
            "  - aeon.co/essays/the-role-of-literature-as-the-key-to-personal-freedom "
            "    (Flora Champy on Proust + Ruskin)\n"
            "  - themarginalian.org/2026/04/30/oliver-sacks-perception "
            "    (Maria Popova on Oliver Sacks)\n"
            "  If those (or any other prior_urls match) are still your top hits, "
            "  run ANOTHER search with a NARROWER query — try: "
            "  'long-form essay this week 2026', 'NYRB May 2026 issue', "
            "  'Aeon new essay [current month] 2026', 'Marginalian post May 2026', "
            "  'LRB New York Review of Books recent essay' — and keep searching "
            "  until at least 4 of your 4-5 final URLs are NOT in `prior_urls`. "
            "  Returning a previously-covered URL with paraphrased findings prose "
            "  is a hard failure for this sector.\n"
            "Read the full text returned by exa for each chosen article — do not summarise "
            "from the title or dek alone. Write findings from the body. "
            "Begin findings with the specific TITLE and AUTHOR so covered-headline matching works.\n"
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
        # intellectual_journals: exa-heavy (returns full text), serper fallback,
        # tavily_extract for non-exa results.
        tools=_TOOLS_WEB_SEARCH + _TOOLS_EXTRACT,
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
            "the headline.\n"
            "MANDATORY DEDUP RULE: FILTER OUT any URL already in `prior_urls`. Audria, "
            "Humy.ai, Nirva, Friend pendant, and the standard MagicSchool/Diffit/Brisk "
            "trio have shipped repeatedly. If your top hits are all in prior_urls, "
            "run another search — try queries like: 'AI wearable launch 2026 [month]', "
            "'EdTech English teacher tool launch 2026', 'lifelogging pendant new product "
            "2026', 'AI glasses launch 2026' — until at least one URL per subsection is "
            "NOT in `prior_urls`. If a subsection genuinely has no new product since "
            "prior coverage, return ONE sentence in findings explicitly stating that "
            "(e.g. 'No new voice-pendant launches since prior coverage.') and include "
            "an empty urls array for that subsection. Returning a previously-covered "
            "URL is a hard failure. "
            "Return a JSON array of {category, findings, urls}, one entry per subsection."
        ),
        default=[],
        # wearable_ai: product pages + EdTech blogs. Exa + serper + extract.
        tools=_TOOLS_WEB_SEARCH + _TOOLS_EXTRACT,
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
            "MANDATORY DEDUP RULE — read carefully:\n"
            "  After every search, FILTER OUT any URL that already appears in "
            "  `prior_urls`. Do NOT include a URL that is in `prior_urls`. The "
            "  Karl-Alber 'Studies on Triadic Ontology' series, Migliorini's "
            "  'Relational Ontologies and Trinitarian Metaphysics', and Tricard's "
            "  'Ultimate Argument Against Nominalistic Relationalism' have ALREADY "
            "  shipped on multiple prior days. If those (or any other prior_urls "
            "  match) are still your top hits, run ANOTHER search with a NARROWER "
            "  query — try: 'process metaphysics paper 2026', 'open theism "
            "  trinitarian 2026', 'Peirce semiotics triadic logic 2026', "
            "  'co-constitution relata 2026' — and keep searching until at least "
            "  one of your final URLs is NOT in `prior_urls`. Returning a "
            "  previously-covered URL with paraphrased findings prose is a hard "
            "  failure for this sector. "
            "Begin your findings prose with the specific TITLE and AUTHOR of each paper or "
            "volume discussed so that covered-headline matching works correctly. "
            "CRITICAL: 'findings' MUST be a single prose string (500-1000 chars), NOT an "
            "array or list. Return exactly: {\"findings\": \"<prose>\", \"urls\": [...]}."
        ),
        default={"findings": "", "urls": []},
        # triadic_ontology: deep-research, exa-driven (returns full text).
        # Allow grounded peers for narrative synthesis attempts.
        tools=("serper_search", "exa_search", "jina_search", "jina_deepsearch",
               "tavily_extract", "fetch_article_text"),
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
            "MANDATORY DEDUP RULE — read carefully:\n"
            "  After every search, FILTER OUT any URL that already appears in "
            "  `prior_urls`. Do NOT include a URL that is in `prior_urls`. The "
            "  DOVA paper (arxiv 2603.13327), Mimosa, and InternAgent-1.5 have "
            "  ALREADY shipped on multiple prior days. If those (or any other "
            "  prior_urls match) are still your top hits, run ANOTHER search with "
            "  a NARROWER query — try: 'multi-agent ScienceAgentBench 2026 paper', "
            "  'reasoning model inference budget paper 2026', 'agent self-evolution "
            "  loop 2026', 'prompt optimization without labels 2026', 'tool-use "
            "  failure recovery agent 2026' — and keep searching until at least one "
            "  of your final URLs is NOT in `prior_urls`. Returning a "
            "  previously-covered URL with paraphrased findings prose is a hard "
            "  failure for this sector. "
            "CRITICAL: 'findings' MUST be a single prose string (500-1000 chars), NOT an "
            "array or list. Return exactly: {\"findings\": \"<prose string>\", \"urls\": [...]}. "
            "Do not put an array in the findings field."
        ),
        default={"findings": "", "urls": []},
        # ai_systems: deep-research, exa-driven. Same shape as triadic_ontology.
        tools=("serper_search", "exa_search", "jina_search", "jina_deepsearch",
               "tavily_extract", "fetch_article_text"),
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
        # uap: deep-research, fewer ongoing sources; same toolbox as the other
        # two deep sectors.
        tools=("serper_search", "exa_search", "jina_search", "jina_deepsearch",
               "tavily_extract", "fetch_article_text"),
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
        # newyorker: bypassed by run_sector's direct-fetch fast path. Allowlist
        # documents intent. The single TOTT fetcher is the only legitimate tool.
        tools=("fetch_new_yorker_talk_of_the_town",),
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
            "MANDATORY — Reuters blocks direct fetches with 401. You MUST replace any "
            "Reuters URL with an equivalent BBC, Guardian, AP, or Al Jazeera URL covering "
            "the same story. A Reuters URL in your output is a hard failure for this sector. "
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
        # enriched_articles: pure extraction. No search — input is the seed URL
        # list from earlier sectors. Allow the full extract chain.
        tools=_TOOLS_EXTRACT,
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
        # literary_pick: single exa query for one book. Allow tavily_extract +
        # fetch_article_text in case agent wants to read a review page.
        tools=("exa_search", "tavily_extract", "fetch_article_text"),
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
producing repetitive briefings. Every search call MUST request content from
the last 7 days:
  - serper_search: pass tbs='qdr:w' (last 7 days) or tbs='qdr:d' (last 24h
    for breaking).
  - tavily_search: pass time_range='week' (or 'day' for breaking).
  - exa_search: pass start_published_date='{seven_days_ago}'.
Override this rule ONLY when a sector instruction explicitly asks for
open-ended results (e.g., literary_pick covers 2004–2024). For all other
sectors, queries without a freshness parameter are defective and MUST be
re-issued with the freshness filter set.

**SOURCE-ROTATION RULE — MANDATORY:**
When an article from a given source was covered yesterday, you MUST select
the next-most-relevant article from THAT SAME source today, NOT a different
source. When a sector hits 4+ candidate articles from the same publisher,
you MUST keep one (the most relevant) and MUST select articles from
publishers NOT in `prior_urls_sample` for the rest. Citing an article whose
URL or headline appears in prior coverage is a hard failure for the sector
and the result will be discarded.

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

Research discipline — you MUST complete TWO rounds before writing your output:
  Round 1 (search): dispatch 2-4 search tools in parallel.
  Round 2 (read): call tavily_extract on top results that exa did NOT already
  return full text for (batch up to 5 URLs per call); OR run a second targeted
  search to fill coverage gaps.
You MUST NOT write the final JSON before Round 2 has completed. A single
search round followed immediately by output is shallow research and the
result will be discarded. Issue at least 6 tool calls per sector — fewer is
defective. Do NOT stop early.
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

    # 2026-05-22 round 8: generalised bare-string filter for ALL list-shape
    # output. Round-7 fix gated this on shape=="enriched" only — but
    # intellectual_journals (shape="list") hit the same Pydantic crash on
    # 2026-05-22 run #90. OR :floor models return bare URL strings for any
    # list-shape sector when they fall back to training data.
    if spec.shape in ("list", "enriched") and isinstance(parsed, list):
        before_str_filter = len(parsed)
        parsed = [e for e in parsed if isinstance(e, dict)]
        dropped_strs = before_str_filter - len(parsed)
        if dropped_strs:
            log.warning(
                "sector %s: dropped %d bare-string entries from %s output "
                "(model returned flat URL list instead of structured dicts).",
                spec.name, dropped_strs, spec.shape,
            )
        # If filter emptied the list, fall back to spec.default so downstream
        # save_session doesn't ship an empty sector that fails GATE-A.
        if not parsed:
            log.warning(
                "sector %s: all entries were bare strings; falling back to default.",
                spec.name,
            )
            parsed = spec.default

    # For enriched sectors, enforce the 500-char text cap regardless of model
    # compliance — avoids bloated session JSON and downstream NIM context issues.
    if spec.shape == "enriched" and isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry.get("text"), str) and len(entry["text"]) > 500:
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
                + "\n\nFor any host listed above, you MUST select a DIFFERENT article from "
                "that same host today. Re-citing any listed title is a hard failure.\n"
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

    # Site skills (Autobrowse-pattern lift, 2026-05-09). Splice the relevant
    # markdown skills for this sector ahead of the instruction so the agent
    # reads the durable workflow notes BEFORE its first tool call. Keep the
    # block bounded — free-tier NIM eats ~12K input tokens before throttling.
    try:
        from jeeves.site_skills import skills_for_sector, render_skills_block
        skills_for_this = skills_for_sector(spec.name)
        if skills_for_this:
            skills_block = render_skills_block(skills_for_this, max_chars=4000)
            if skills_block:
                base = base + "\n\n" + skills_block
    except Exception as exc:
        # Site-skills loading is enrichment, not load-bearing. A bad skill
        # file MUST NOT break the research pipeline — log and proceed.
        log.warning("site_skills load failed for sector %s: %s", spec.name, exc)

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
    """True for transient streaming/network errors (peer drop, timeout, reset).

    2026-05-21 round 7: added "connection error" to catch httpx.ConnectError
    surfaces as ``Connection error.`` — observed on OR :floor endpoint for
    global_news crawl4ai synthesis.  Rotatable in the crawl4ai OR phase; also
    bubbles up to the main run_sector network-retry branch.
    """
    msg = str(exc).lower()
    return any(phrase in msg for phrase in (
        "peer closed connection",
        "incomplete chunked read",
        "connection reset",
        "connection error",
        "read timeout",
        "server disconnected",
    ))


def _is_nim_rate_limit(exc: Exception) -> bool:
    """True when NIM responded 429 Too Many Requests."""
    msg = str(exc).lower()
    return "429" in msg or "too many requests" in msg


def _is_or_dead_endpoint(exc: Exception) -> bool:
    """True when OpenRouter returns 404 "No endpoints found for X".

    OR deprecates model variants from time to time (e.g.,
    qwen/qwen-2.5-72b-instruct:free 2026-05). When that happens, the
    request returns ``404 - {'error': {'message': 'No endpoints found
    for ...'}}``. The semantically-correct response is the same as a
    429 — rotate to the next chain entry — not "agent crashed". This
    helper lets the retry loop treat dead routes as rotation triggers.
    """
    msg = str(exc).lower()
    return "no endpoints found" in msg and "404" in msg


def _is_stream_timeout(exc: Exception) -> bool:
    """Match agent-crashed-with-timeout shape.

    Covers ``asyncio.TimeoutError``, ``openai.APITimeoutError``, ``httpx``
    timeout variants, and the bare LlamaIndex ``Request timed out.`` message
    that surfaces when NIM closes a streaming connection mid-response.
    Distinct from ``_is_retryable_network_error`` which covers transient
    peer-close shapes.

    The 2026-05-14 run #68 deep sectors (triadic_ontology, ai_systems, uap,
    weather) all crashed with ``str(e) == "Request timed out."`` — the bare
    message takes precedence over any class-name match.
    """
    cls = type(exc).__name__.lower()
    msg = str(exc).strip().lower()
    if msg.startswith("request timed out"):
        return True
    if "timeout" in cls:
        return True
    if "timed out" in msg:
        return True
    return False




_CEREBRAS_BASE = "https://api.cerebras.ai/v1"
_CEREBRAS_MODEL_CHAIN = [
    # 2026-05-21 hotfix: reordered to put known-free-tier models first.
    # gpt-oss-120b and qwen-3-235b require preview/waitlist access on
    # Cerebras Cloud free tier — production runs failed with "Cerebras
    # unavailable (no key or model resolution failed)" because the probe
    # would pick gpt-oss-120b first and the build would 401/404.
    # llama-3.3-70b is guaranteed-available on Cerebras free tier.
    "llama-3.3-70b",
    "llama-4-maverick-17b-128e-instruct",
    "llama-4-scout-17b-16e-instruct",
    "qwen-3-32b",
    # Preview / waitlist models — keep as later fallbacks
    "gpt-oss-120b",
    "qwen-3-235b-a22b-instruct-2507",
    "zai-glm-4.7",
    # Older variants / spelling differences
    "llama3.3-70b",
    "llama-3.1-70b",
    "llama3.1-70b",
    # 2026-05-21 round 5: llama3.1-8b REMOVED from chain entirely. Production
    # Run #48 (commit 39c85c6) confirmed its 8192-token context window crashes
    # deep sectors with "Cerebras: Current length is 9739 while limit is 8192".
    # The crash hits the agent-loop's else branch as "agent crashed" and
    # returns spec.default — no fallthrough to OR. Dropping the entry means
    # rotation falls straight to OpenRouter once the 3 usable Cerebras models
    # (gpt-oss-120b, qwen-3-235b-a22b-instruct-2507, zai-glm-4.7) all 429.
]
# 2026-05-21 round 7: Models too small for our deep sectors (ctx < ~10k tokens).
# Blocked from BOTH _CEREBRAS_MODEL_CHAIN and the _resolve_cerebras_model
# `remaining` fallback — the fallback picks ANY available model alphabetically,
# which previously allowed llama3.1-8b to slip back in after the chain was
# exhausted.  Banning it here ensures the "no untried models" path fires and
# falls through to OpenRouter instead.
_CEREBRAS_CTX_BANNED: frozenset[str] = frozenset({"llama3.1-8b"})

_RESOLVED_CEREBRAS_MODEL: str | None = None
_CEREBRAS_TRIED_MODELS: set[str] = set()  # models that 429'd this session
# 2026-05-29: Cerebras chain exhaustion breaker. Once all chain entries
# have 429'd in a given run, set this True so subsequent sectors skip
# the entire Cerebras path (no more /v1/models probes, no more chain
# walks). Chronicle Pattern P3: circuit-breaker + skip beats per-call
# retry when the provider is broken. Production telemetry showed
# repeated "cerebras_chain_exhausted" rows across every sector AFTER
# the first exhaustion, plus a /v1/models probe per sector — all
# wasted time. The breaker collapses that to a single None return.
_CEREBRAS_EXHAUSTED: bool = False


# 2026-05-30 (Phase-D option ii): Groq research tier — intermediate fallback
# between Cerebras-exhausted and OpenRouter for deep-sector synthesis.
#
# Why: Cerebras free-tier exhausts quickly (often on first sector). OpenRouter
# free chain has token-output caps too tight for deep sectors (gpu-poor models
# truncate at 1500-2000 tokens, dropping intellectual_journals to 0).
#
# Risk: Groq shares its 100k/day TPD with the write phase (~82k/day spend).
# Adding research load could cascade into write-phase Part 9 TPD overage
# (chronicle run #69, 2026-05-15). Mitigations:
#   1) Default-OFF flag (JEEVES_USE_GROQ_RESEARCH_TIER) — opt-in per env.
#   2) Per-process char budget (`_GROQ_RESEARCH_DAILY_CAP`) approximates token
#      cost using chars/4. ~15k chars ≈ ~3.75k tokens — caps research-side
#      Groq draw to ≤4% of daily TPD. Trip → cascade to OpenRouter.
#   3) Per-call telemetry so production has visibility into trip rate.
_GROQ_RESEARCH_DAILY_CAP: int = 15000  # chars across all sector calls combined
_GROQ_RESEARCH_USED_CHARS: int = 0
import threading as _threading_groq  # localized import — avoid top-of-file noise
_GROQ_RESEARCH_LOCK = _threading_groq.Lock()


def _groq_research_tier_enabled() -> bool:
    """Runtime flag check — allows tests to flip the env var without re-import."""
    import os as _os
    return _os.environ.get("JEEVES_USE_GROQ_RESEARCH_TIER", "").lower() in (
        "1", "true", "yes",
    )


def _groq_research_budget_remaining() -> int:
    """Chars remaining in today's Groq-research budget. 0 → tripped."""
    with _GROQ_RESEARCH_LOCK:
        return max(0, _GROQ_RESEARCH_DAILY_CAP - _GROQ_RESEARCH_USED_CHARS)


def _groq_research_record_use(chars: int) -> None:
    """Atomically add `chars` to the daily counter."""
    global _GROQ_RESEARCH_USED_CHARS
    with _GROQ_RESEARCH_LOCK:
        _GROQ_RESEARCH_USED_CHARS += max(0, int(chars))


def _reset_groq_research_breaker() -> None:
    """Test helper — clear the daily-char counter so each case starts fresh."""
    global _GROQ_RESEARCH_USED_CHARS
    with _GROQ_RESEARCH_LOCK:
        _GROQ_RESEARCH_USED_CHARS = 0


async def _try_groq_research_synthesis(cfg, spec, messages) -> str | None:
    """Best-effort Groq synthesis for deep sectors when Cerebras is exhausted.

    Returns the response text on success, None on any failure (flag off,
    budget exhausted, missing key, API error). Emits one telemetry row per
    attempt so production can track trip rates.

    Caller pattern (inside _run_crawl4ai_sector after Cerebras exhaustion):
        groq_raw = await _try_groq_research_synthesis(cfg, spec, messages)
        if groq_raw:
            raw = groq_raw  # downstream OR-phase guard skips because raw is set
    """
    if not _groq_research_tier_enabled():
        return None

    # Approximate prompt size by char count. Cheaper than running a tokenizer
    # and good enough for the guard's 1k-char resolution.
    prompt_chars = sum(len(getattr(m, "content", "") or "") for m in messages)
    remaining = _groq_research_budget_remaining()
    if prompt_chars > remaining:
        log.info(
            "sector %s: groq research tier — budget exhausted "
            "(prompt=%d > remaining=%d); cascading to OR.",
            spec.name, prompt_chars, remaining,
        )
        try:
            from .tools.telemetry import emit as _emit
            _emit(
                "llm_call",
                provider="groq",
                label="research_sector",
                sector=spec.name,
                ok=False,
                error="groq_research_daily_budget_exhausted",
            )
        except Exception:
            pass
        return None

    try:
        from .llm import build_groq_llm
        # max_tokens=2048 keeps any single deep-sector synthesis well under
        # the 4k-ish token margin Groq leaves for write-phase TPD headroom.
        llm = build_groq_llm(cfg, temperature=0.65, max_tokens=2048)
    except Exception as exc:
        log.warning(
            "sector %s: groq research tier build_groq_llm failed (%s).",
            spec.name, exc,
        )
        return None

    try:
        import time as _t
        t0 = _t.monotonic()
        resp = await llm.achat(messages)
        raw = (resp.message.content or "").strip()
        latency_ms = (_t.monotonic() - t0) * 1000
        # Record use: prompt + completion chars.
        _groq_research_record_use(prompt_chars + len(raw))
        log.info(
            "sector %s: groq research tier — synthesis ok "
            "(%d chars in %.0f ms).",
            spec.name, len(raw), latency_ms,
        )
        try:
            from .tools.telemetry import emit as _emit
            _emit(
                "llm_call",
                provider="groq",
                label="research_sector",
                sector=spec.name,
                ok=True,
                latency_ms=latency_ms,
                chars=len(raw),
            )
        except Exception:
            pass
        return raw
    except Exception as exc:
        log.warning(
            "sector %s: groq research tier synthesis failed (%s); cascading to OR.",
            spec.name, exc,
        )
        try:
            from .tools.telemetry import emit as _emit
            _emit(
                "llm_call",
                provider="groq",
                label="research_sector",
                sector=spec.name,
                ok=False,
                error=str(exc)[:200],
            )
        except Exception:
            pass
        return None


def _reset_cerebras_breaker() -> None:
    """Test helper — clear the exhaustion breaker plus tried-models set
    and the cached resolution. Used by tests so each case starts from a
    pristine breaker state. Production never resets within a run;
    chain exhaustion is one-way per process lifetime."""
    global _CEREBRAS_EXHAUSTED, _RESOLVED_CEREBRAS_MODEL
    _CEREBRAS_EXHAUSTED = False
    _RESOLVED_CEREBRAS_MODEL = None
    _CEREBRAS_TRIED_MODELS.clear()
    _reset_groq_research_breaker()


def _resolve_cerebras_model(api_key: str) -> str | None:
    """Probe /v1/models and resolve the best available model from the chain.

    Skips models already in _CEREBRAS_TRIED_MODELS (429'd this session).
    Caches the result in _RESOLVED_CEREBRAS_MODEL for subsequent calls.
    Soft-fails to the first untried chain entry when the probe request fails.

    Returns None only when all chain entries are exhausted.

    Once the chain is fully exhausted in a run, _CEREBRAS_EXHAUSTED is
    set to True and ALL subsequent calls return None immediately without
    re-probing /v1/models. This is the chronicle Pattern P3 breaker
    pattern (skip > retry when provider is broken). Production
    telemetry showed sectors-after-exhaustion each issuing a fresh
    /v1/models probe; the breaker collapses that to a single
    short-circuit.
    """
    global _RESOLVED_CEREBRAS_MODEL, _CEREBRAS_EXHAUSTED

    # Breaker short-circuit — every sector after exhaustion hits this.
    if _CEREBRAS_EXHAUSTED:
        return None

    if _RESOLVED_CEREBRAS_MODEL is not None:
        return _RESOLVED_CEREBRAS_MODEL

    try:
        import httpx

        resp = httpx.get(
            f"{_CEREBRAS_BASE}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            available = {m["id"] for m in resp.json().get("data", [])}
            log.info("Cerebras models available: %s", sorted(available))
            for candidate in _CEREBRAS_MODEL_CHAIN:
                if candidate in available and candidate not in _CEREBRAS_TRIED_MODELS:
                    _RESOLVED_CEREBRAS_MODEL = candidate
                    log.info("Cerebras: resolved model → %s", candidate)
                    return candidate
            remaining = sorted(available - _CEREBRAS_TRIED_MODELS - _CEREBRAS_CTX_BANNED)
            if remaining:
                _RESOLVED_CEREBRAS_MODEL = remaining[0]
                log.warning(
                    "Cerebras: no preferred model found; using %s",
                    _RESOLVED_CEREBRAS_MODEL,
                )
                return _RESOLVED_CEREBRAS_MODEL
            # 2026-05-29: chain fully exhausted. Trip the breaker so
            # subsequent sectors short-circuit before re-probing /v1/models.
            _CEREBRAS_EXHAUSTED = True
            log.warning(
                "Cerebras: /v1/models listed no untried models — tripping "
                "exhaustion breaker for rest of run"
            )
            return None
        else:
            log.warning("Cerebras /v1/models returned %d", resp.status_code)
    except Exception as e:
        log.warning("Cerebras model probe failed: %s", e)

    # Probe failed or non-200 — blind fallback to first untried chain entry
    for candidate in _CEREBRAS_MODEL_CHAIN:
        if candidate not in _CEREBRAS_TRIED_MODELS:
            _RESOLVED_CEREBRAS_MODEL = candidate
            return candidate
    # 2026-05-29: chain exhausted via the blind-fallback path too.
    _CEREBRAS_EXHAUSTED = True
    log.warning(
        "Cerebras: chain exhausted via blind-fallback path — tripping breaker"
    )
    return None


def _rotate_on_429(failed_model: str) -> str | None:
    """Mark failed_model as 429'd and return the next candidate from the chain.

    Invalidates the cached model resolution so the next _build_cerebras_llm
    call re-resolves from the updated _CEREBRAS_TRIED_MODELS set.

    Returns the next untried model name, or None when all entries are exhausted.
    """
    global _RESOLVED_CEREBRAS_MODEL, _CEREBRAS_TRIED_MODELS, _CEREBRAS_EXHAUSTED

    _CEREBRAS_TRIED_MODELS.add(failed_model)
    _RESOLVED_CEREBRAS_MODEL = None  # force re-resolution next call

    for candidate in _CEREBRAS_MODEL_CHAIN:
        if candidate not in _CEREBRAS_TRIED_MODELS:
            log.info("Cerebras 429 on %s → rotating to %s", failed_model, candidate)
            return candidate

    # 2026-05-29: 429-driven chain exhaustion is the most common path
    # to exhaustion in practice. Trip the breaker so subsequent sectors
    # short-circuit before re-probing /v1/models or walking the chain.
    _CEREBRAS_EXHAUSTED = True
    log.warning(
        "Cerebras: all models in chain exhausted after 429 on %s — "
        "tripping exhaustion breaker for rest of run",
        failed_model,
    )
    return None


def _build_cerebras_llm(max_tokens: int = 8192):
    """Build a Cerebras LLM via OpenAILike for use as NIM fallback.

    Cerebras exposes an OpenAI-compatible /v1/chat/completions endpoint
    with native tool-calling support. Used when NIM circuit breaker trips
    so remaining sectors can still produce data instead of returning empty.

    Calls _resolve_cerebras_model on first invocation (or after _rotate_on_429
    invalidates the cache). Returns None when no key or model is available.
    """
    import os

    api_key = os.environ.get("CEREBRAS_API_KEY", "").strip()
    if not api_key:
        return None

    model = _resolve_cerebras_model(api_key)
    if model is None:
        log.warning("Cerebras unavailable (no key or model resolution failed)")
        return None

    try:
        from llama_index.llms.openai_like import OpenAILike

        return OpenAILike(
            model=model,
            api_base=_CEREBRAS_BASE,
            api_key=api_key,
            max_tokens=max_tokens,
            temperature=0.3,
            timeout=60.0,
            is_chat_model=True,
            is_function_calling_model=True,
            max_retries=0,  # jeeves owns retry/backoff; disable SDK auto-retry
        )
    except Exception as e:
        log.warning("Failed to build Cerebras LLM: %s", e)
        return None


# 2026-05-21 hotfix: OpenRouter free tier model rotation. The base llama-3.3-70b:free
# daily cap is ~50 req/day per model AND each free model is capped at 8 RPM
# (provider-side, shared across all OR users). One sector burst trips both.
# When the first model 429s, callers iterate through this list.
#
# 2026-05-21 round 4: appended PAID backstop entries with :floor suffix
# (sort providers by price). Spend ~$0.002 per sector when free is hot
# instead of returning empty. User has $10+ credit and a $5 OR account
# spending cap so the worst case is bounded.
_OPENROUTER_MODEL_CHAIN = [
    # Free tier — try first; 8 RPM per model, daily cap shared across users.
    # 2026-05-21 round 5: qwen/qwen-2.5-72b-instruct:free DROPPED. OR has
    # deprecated the route and returns 404 "No endpoints found for
    # qwen/qwen-2.5-72b-instruct:free" — same dead-route fix that landed
    # in correspondence.py round 2 (commit fbcff57), now propagated here.
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-2-27b-it:free",
    "deepseek/deepseek-chat-v3:free",
    "mistralai/mistral-small-3.1-24b-instruct:free",
    # PAID backstop — :floor picks cheapest healthy provider for each.
    # ~$0.13/M input + ~$0.40/M output for llama-3.3-70b on paid endpoints.
    # 13-sector run if EVERY sector falls to paid = ~$0.03. $5 cap = ~165 runs.
    "meta-llama/llama-3.3-70b-instruct:floor",
    "mistralai/mistral-small-3.1-24b-instruct:floor",
]

# Back-compat alias — internal name renamed 2026-05-21 round 4 from
# _OPENROUTER_FREE_MODEL_CHAIN. Keep the old name working until any
# downstream consumers (tests, scripts) finish migrating.
_OPENROUTER_FREE_MODEL_CHAIN = _OPENROUTER_MODEL_CHAIN


# Module-level cumulative tried set for OpenRouter, mirroring the Cerebras
# rotation pattern. Once an OR model 429s in this process, subsequent sector
# calls skip it. Reset between processes (each GHA fire is a fresh process).
_OPENROUTER_TRIED_MODELS: set[str] = set()


def _rotate_openrouter_on_429(failed_model: str) -> str | None:
    """Mark failed_model as 429'd and return the next candidate from the OR chain.

    Symmetric with _rotate_on_429 for Cerebras. Returns the next untried
    model name from _OPENROUTER_MODEL_CHAIN, or None when all entries
    are exhausted.
    """
    global _OPENROUTER_TRIED_MODELS
    _OPENROUTER_TRIED_MODELS.add(failed_model)
    for candidate in _OPENROUTER_MODEL_CHAIN:
        if candidate not in _OPENROUTER_TRIED_MODELS:
            log.info(
                "OpenRouter 429 on %s → rotating to %s", failed_model, candidate,
            )
            return candidate
    log.warning(
        "OpenRouter: all models in chain exhausted after 429 on %s",
        failed_model,
    )
    return None


def _next_untried_openrouter_model() -> str | None:
    """First untried entry from _OPENROUTER_MODEL_CHAIN, or None if exhausted."""
    for candidate in _OPENROUTER_MODEL_CHAIN:
        if candidate not in _OPENROUTER_TRIED_MODELS:
            return candidate
    return None


def _build_openrouter_llm(max_tokens: int = 8192, model: str | None = None):
    """OpenRouter model as Cerebras fallback for research sectors.

    2026-05-21 hotfix: pass model explicitly to enable per-sector rotation when
    the default daily cap is hit. max_retries=0 disables SDK auto-retry so a 429
    cascade doesn't compound (SDK retried 3× with 19-30s backoff on top of jeeves
    own retry, costing ~5 min per sector before exhausting).

    2026-05-21 round 4: when no model is passed, pick the first UNTRIED entry
    instead of always defaulting to chain[0]. This makes the LLM builder
    aware of the cumulative TRIED set so a fresh sector after rotation
    starts from the right place.
    """
    from llama_index.llms.openai_like import OpenAILike
    import os as _os
    api_key = _os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return None
    resolved_model = model or _next_untried_openrouter_model()
    if resolved_model is None:
        log.warning(
            "OpenRouter: all chain models 429'd in this process — no LLM built.",
        )
        return None
    return OpenAILike(
        model=resolved_model,
        api_base="https://openrouter.ai/api/v1",
        api_key=api_key,
        is_chat_model=True,
        is_function_calling_model=True,
        max_tokens=max_tokens,
        temperature=0.3,
        timeout=120.0,
        max_retries=0,  # hotfix: jeeves owns retry logic; prevent SDK retry amplification
    )


# Sectors whose agents call non-quota tools (fetch_new_yorker_talk_of_the_town
# or fetch_article_text) — skip the quota-increment check for these.
_NO_QUOTA_CHECK = frozenset({"newyorker"})

# ---------------------------------------------------------------------------
# M2 — Crawl4AI research path (JEEVES_USE_CRAWL4AI_RESEARCH=1)
# ---------------------------------------------------------------------------

# Sectors eligible for the Crawl4AI+Cerebras synthesis path.
# Deep sectors (triadic_ontology, ai_systems, uap) are always excluded —
# they require FunctionAgent's multi-step tool calls and large context windows.
# newyorker uses a direct Python fetch, not an agent, so also excluded.
_CRAWL4AI_ELIGIBLE_SECTORS: frozenset[str] = frozenset({
    "local_news", "global_news", "weather", "career", "family", "wearable_ai",
})

# Simple search queries used by _run_crawl4ai_sector to seed URL discovery.
# These are intentionally short — Crawl4AI does the deep extraction, not the agent.
_SECTOR_SEARCH_QUERIES: dict[str, str] = {
    "local_news": "Edmonds WA news today",
    "global_news": "world news today",
    "weather": "Edmonds WA weather forecast today",
    "career": "high school English teacher jobs Edmonds Washington",
    "family": "family events kids activities Edmonds Seattle this week",
    "wearable_ai": "wearable AI technology news 2026",
}


async def _run_crawl4ai_sector(
    cfg: Config,
    spec: SectorSpec,
    prior_urls_sample: list[str],
    ledger,
) -> Any:
    """Crawl4AI+Cerebras research path for news_short-eligible sectors.

    Replaces the FunctionAgent loop for eligible sectors when
    JEEVES_USE_CRAWL4AI_RESEARCH=1. Flow:
      1. Direct serper search → top URLs (no agent overhead).
      2. batch_extract those URLs via Crawl4AI.
      3. ONE Cerebras synthesis call → sector JSON.

    Falls back to spec.default on any failure.
    """
    import json as _json

    from .tools.serper import make_serper_search
    from .tools.crawl4ai_extract import batch_extract

    query = _SECTOR_SEARCH_QUERIES.get(spec.name, f"{spec.name} news today")
    log.info("sector %s: crawl4ai path — query=%r", spec.name, query)

    # 1. Search for candidate URLs.
    serper_fn = make_serper_search(cfg, ledger)
    try:
        search_raw = serper_fn(query=query, num=10)
        search_data = _json.loads(search_raw)
        prior_set = set(prior_urls_sample)
        # NOTE: make_serper_search wraps Serper's raw response into
        # {"provider":..., "query":..., "results":[{"title","url","snippet",...}, ...]}.
        # Earlier code keyed off the raw API shape ("organic" / "link") and
        # therefore returned 0 URLs for every sector — silent starve since
        # M2 ship. 2026-05-21 fix uses the wrapper shape: "results" / "url".
        urls = [
            r["url"]
            for r in search_data.get("results", [])
            if r.get("url") and r["url"] not in prior_set
        ][:8]
    except Exception as exc:
        log.warning("sector %s: crawl4ai serper failed (%s); returning default.", spec.name, exc)
        return spec.default

    if not urls:
        log.warning("sector %s: crawl4ai no fresh URLs from search; returning default.", spec.name)
        return spec.default

    # 2. Extract content via Crawl4AI.
    try:
        extractions = await batch_extract(urls, query=query, max_chars=6000)
    except Exception as exc:
        log.warning("sector %s: crawl4ai batch_extract failed (%s); returning default.", spec.name, exc)
        return spec.default

    content_parts = [
        f"=== {url} (mode: {mode}) ===\n{text}"
        for url, (text, mode) in zip(urls, extractions)
        if text
    ]
    if not content_parts:
        log.warning("sector %s: crawl4ai no content extracted; returning default.", spec.name)
        return spec.default

    # 3. Synthesis call with TWO-phase rotation.
    #
    # Phase 1: Cerebras (fast + free). Rotates through _CEREBRAS_MODEL_CHAIN
    # on 429, mirroring the FunctionAgent path's _is_nim_rate_limit handler.
    #
    # Phase 2 (2026-05-21 round 6): when the Cerebras chain is fully
    # exhausted (cumulative TRIED set populated by earlier sectors), fall
    # through to the OpenRouter chain — same rotation pattern, terminates
    # on :floor paid backstops. Run #48 confirmed Crawl4AI sectors were
    # being starved because Cerebras had already burnt out on the 3 deep
    # sectors; phase 2 unblocks the 6 Crawl4AI-eligible sectors.
    from llama_index.core.llms import ChatMessage

    content_block = "\n\n".join(content_parts)
    synthesis_prompt = (
        f"Sector instruction: {spec.instruction}\n\n"
        f"Extracted content from {len(content_parts)} URLs:\n"
        f"{content_block[:20000]}\n\n"
        f"Return ONLY valid JSON matching the instruction format. "
        f"Use only facts from the extracted content above. "
        f"Include real URLs from the extracted content."
    )
    messages = [ChatMessage(role="user", content=synthesis_prompt)]

    raw: str = ""

    # ── Phase 1: Cerebras rotation ─────────────────────────────────────
    cerebras_chain_exhausted = False
    for _rotation_attempt in range(len(_CEREBRAS_MODEL_CHAIN) + 1):
        llm = _build_cerebras_llm(max_tokens=8192)
        if llm is None:
            log.info(
                "sector %s: crawl4ai Cerebras unavailable (chain exhausted); "
                "falling through to OpenRouter.",
                spec.name,
            )
            cerebras_chain_exhausted = True
            break
        current_model = getattr(llm, "model", None)
        try:
            resp = await llm.achat(messages)
            raw = (resp.message.content or "").strip()
            log.info(
                "sector %s: crawl4ai synthesis done on cerebras/%s (%d chars).",
                spec.name, current_model, len(raw),
            )
            break  # success
        except Exception as exc:
            if _is_nim_rate_limit(exc) and current_model:
                next_model = _rotate_on_429(current_model)
                if next_model is not None:
                    log.info(
                        "sector %s: crawl4ai cerebras 429 on %s → rotating to %s.",
                        spec.name, current_model, next_model,
                    )
                    continue  # rebuild LLM with rotated model on next iter
                # Cerebras chain exhausted via rotation — fall to OR.
                cerebras_chain_exhausted = True
                break
            # Non-rate-limit exception (timeout, parse error, etc.) — bail.
            log.warning(
                "sector %s: crawl4ai cerebras synthesis failed on %s (%s); "
                "returning default.",
                spec.name, current_model, exc,
            )
            return spec.default
    else:
        cerebras_chain_exhausted = True

    # ── Phase 1.5: Groq research tier (opt-in via JEEVES_USE_GROQ_RESEARCH_TIER)
    # Inserted 2026-05-30 (Phase-D option ii). Default-OFF — safe no-op when
    # flag is unset. When enabled, tries Groq once before falling through to
    # OpenRouter; daily-char budget guard prevents cascade into write-phase
    # TPD overage. Sets `raw` on success so the OR-phase guard below skips.
    if not raw and cerebras_chain_exhausted:
        groq_raw = await _try_groq_research_synthesis(cfg, spec, messages)
        if groq_raw:
            raw = groq_raw

    # ── Phase 2: OpenRouter rotation (only if Cerebras fully exhausted) ──
    if not raw and cerebras_chain_exhausted:
        log.info(
            "sector %s: crawl4ai synthesis → OR rotation phase.", spec.name,
        )
        for _or_attempt in range(len(_OPENROUTER_MODEL_CHAIN) + 1):
            or_llm = _build_openrouter_llm(max_tokens=4096)
            if or_llm is None:
                log.warning(
                    "sector %s: crawl4ai OR unavailable (chain exhausted or "
                    "no key); returning default.",
                    spec.name,
                )
                return spec.default
            or_model = getattr(or_llm, "model", None)
            try:
                resp = await or_llm.achat(messages)
                raw = (resp.message.content or "").strip()
                log.info(
                    "sector %s: crawl4ai synthesis done on openrouter/%s "
                    "(%d chars).",
                    spec.name, or_model, len(raw),
                )
                break  # success
            except Exception as exc:
                # OR rotates on 429, dead-endpoint 404, AND transient network
                # errors (Connection error., peer closed, etc.) — 2026-05-21
                # round 7: global_news lost to Connection error on :floor endpoint;
                # rotating to next OR model is the right recovery.
                rotatable = (
                    _is_nim_rate_limit(exc)
                    or _is_or_dead_endpoint(exc)
                    or _is_retryable_network_error(exc)
                )
                if rotatable and or_model:
                    next_or = _rotate_openrouter_on_429(or_model)
                    if next_or is not None:
                        log.info(
                            "sector %s: crawl4ai or 429/404 on %s → rotating "
                            "to %s.",
                            spec.name, or_model, next_or,
                        )
                        continue
                log.warning(
                    "sector %s: crawl4ai or synthesis failed on %s (%s); "
                    "returning default.",
                    spec.name, or_model, exc,
                )
                return spec.default
        else:
            log.warning(
                "sector %s: crawl4ai synthesis exhausted both Cerebras and "
                "OR chains; returning default.",
                spec.name,
            )
            return spec.default

    if not raw:
        log.warning(
            "sector %s: crawl4ai synthesis produced empty output; "
            "returning default.",
            spec.name,
        )
        return spec.default

    parsed = _parse_sector_output(raw, spec)
    if isinstance(parsed, _ParseFailed):
        log.warning("sector %s: crawl4ai parse failed; returning default.", spec.name)
        return spec.default

    return parsed


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

    # Tag every telemetry event fired during this sector's execution with
    # the sector name. Fixes the sector="?" gap observed in production
    # telemetry where all 37 tool_call events on 2026-05-20 were unattributed.
    # contextvar propagates through asyncio await boundaries automatically.
    from .tools.telemetry import sector_context

    with sector_context(spec.name):
        return await _run_sector_inner(
            cfg, spec, prior_urls_sample, ledger,
            extra_user=extra_user,
            quota_summary=quota_summary,
            story_continuity=story_continuity,
            prior_sources_by_host=prior_sources_by_host,
        )


async def _run_sector_inner(
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
    """Original run_sector body, now wrapped by the sector_context manager."""

    # Fast path: newyorker bypasses the LLM agent entirely.
    # fetch_talk_of_the_town is pure Python — no LLM needed or wanted.
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

    # Crawl4AI path: always active for eligible (news_short) sectors.
    # Deep sectors (shape=="deep") use FunctionAgent — they require
    # multi-step tool calls and large context windows.
    if spec.name in _CRAWL4AI_ELIGIBLE_SECTORS and spec.shape != "deep":
        return await _run_crawl4ai_sector(cfg, spec, prior_urls_sample, ledger)

    # Research now uses Cerebras as primary, OpenRouter as fallback.
    # NIM Kimi removed from research path (broken streaming protocol since 2026-05-13).
    # Newyorker fast-path above already handled the no-LLM case.
    _use_cerebras_fallback = _build_cerebras_llm(
        max_tokens=(
            2048 if spec.shape == "enriched"
            else 4096 if spec.shape == "deep"
            else 8192
        )
    )
    _use_openrouter_fallback = None
    if _use_cerebras_fallback is None:
        log.warning(
            "sector %s: Cerebras unavailable (no key or model resolution failed); "
            "trying OpenRouter.", spec.name,
        )
        _use_openrouter_fallback = _build_openrouter_llm(
            max_tokens=4096 if spec.shape != "enriched" else 2048
        )
        if _use_openrouter_fallback is None:
            log.warning(
                "sector %s: no LLM available (Cerebras + OpenRouter both unconfigured); "
                "returning default.", spec.name,
            )
            return spec.default

    from llama_index.core.agent.workflow import FunctionAgent

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

    # Per-sector tool subset (sprint-2026-05-21).
    # When JEEVES_PER_SECTOR_TOOLS=1 AND spec.tools is set, the agent
    # receives only the tools the sector actually needs — saves ~1k
    # tokens per sector by dropping unused tool descriptions. Default
    # behaviour: full toolbox (back-compat). See jeeves.tools.tools_for_sector.
    # Quota-aware exclusion layered on: tools whose provider is at >=85%
    # of monthly cap are dropped from the toolbox when
    # JEEVES_USE_QUOTA_AWARE_EXCLUSION=1.
    from .tools import tools_for_sector as _tfs
    tools = _tfs(cfg, ledger, set(prior_urls_sample), allowlist=spec.tools)
    tools = _apply_quota_aware_exclusion(tools, ledger)
    if _use_cerebras_fallback is not None:
        llm = _use_cerebras_fallback
        _provider_label = "cerebras"
    elif _use_openrouter_fallback is not None:
        llm = _use_openrouter_fallback
        _provider_label = "openrouter"
    else:
        # Should never reach here — earlier code returns spec.default.
        return spec.default

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
    # Three separate retry budgets:
    #   - Network drops (peer closed, chunked read): 3 retries at 10/30/60s
    #   - 429 rate limit: 6 retries at 10s each (Cerebras + OpenRouter free-tier cooldown)
    #   - Stream timeout: 1 retry at 10s
    _net_delays = [10, 30, 60]
    _ratelimit_delays = [10, 10, 10, 10, 10, 10]
    _timeout_delays = [10]
    net_attempts = 0
    rl_attempts = 0
    timeout_attempts = 0
    last_exc: Exception | None = None
    # Sector-level LLM-call telemetry. Records ONE event per agent.run()
    # invocation — call-count + latency + ok. Only the SUCCESSFUL or
    # FINALLY-FAILED outcome lands; individual retries are not double-counted.
    import time as _t
    from .tools.telemetry import emit_llm_call

    _sector_t0 = _t.monotonic()

    for _loop_guard in range(20):  # hard cap prevents infinite loop
        try:
            if response is None and net_attempts == 0 and rl_attempts == 0 and timeout_attempts == 0:
                response = await agent.run(user_msg)
            else:
                # Rebuild agent with fresh instances for every retry so no
                # stale streaming state leaks from the previous crashed connection.
                tools_r = all_search_tools(cfg, ledger, set(prior_urls_sample))
                agent_r = FunctionAgent(
                    tools=tools_r, llm=llm,
                    system_prompt=_system_prompt,
                    verbose=cfg.verbose,
                )
                response = await agent_r.run(user_msg)
            emit_llm_call(
                provider=_provider_label,
                model=getattr(llm, "model", "unknown"),
                label="research_sector",
                sector=spec.name,
                latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                ok=True,
            )
            break  # success — exit retry loop
        except Exception as e:
            last_exc = e
            # 2026-05-21 round 5: dead-endpoint 404 from OpenRouter is a
            # rotation trigger, NOT an "agent crashed" terminal error. OR
            # periodically removes :free routes (e.g., qwen-2.5-72b-instruct
            # 2026-05); the right response is to mark the model TRIED and
            # try the next one, exactly like a 429.
            is_rate_or_dead = (
                _is_nim_rate_limit(e)
                or (_provider_label == "openrouter" and _is_or_dead_endpoint(e))
            )
            if is_rate_or_dead:
                # 2026-05-21: Cerebras free-tier RPM is per-model, not per-key.
                # Sleeping 10s and retrying the same model rarely clears the
                # 429 because gpt-oss-120b (resolved first) is hammered by all
                # users on the free tier. _rotate_on_429 (defined module-level)
                # marks the failed model as TRIED and picks the next available
                # model from _CEREBRAS_MODEL_CHAIN. The TRIED set is cumulative
                # across sectors within the same process so subsequent sectors
                # skip already-hot models without re-probing.
                if _provider_label == "cerebras":
                    current_model = getattr(llm, "model", None)
                    next_model = (
                        _rotate_on_429(current_model) if current_model else None
                    )
                    if next_model is not None:
                        rotated_llm = _build_cerebras_llm(
                            max_tokens=sector_max_tokens,
                        )
                        if rotated_llm is not None:
                            log.info(
                                "sector %s: cerebras 429 on %s → rotating to %s.",
                                spec.name,
                                current_model,
                                getattr(rotated_llm, "model", "?"),
                            )
                            llm = rotated_llm
                            rl_attempts = 0  # fresh budget per rotated model
                            net_attempts = 0
                            timeout_attempts = 0
                            last_exc = None
                            tools = all_search_tools(
                                cfg, ledger, set(prior_urls_sample),
                            )
                            agent = FunctionAgent(
                                tools=tools, llm=llm,
                                system_prompt=_system_prompt,
                                verbose=cfg.verbose,
                            )
                            continue  # retry immediately with rotated model
                    # No more Cerebras models — fall to OpenRouter directly.
                    emit_llm_call(
                        provider=_provider_label,
                        model=getattr(llm, "model", "unknown"),
                        label="research_sector",
                        sector=spec.name,
                        latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                        ok=False,
                        error="cerebras_chain_exhausted",
                    )
                    if _use_openrouter_fallback is None:
                        _use_openrouter_fallback = _build_openrouter_llm(
                            max_tokens=4096 if spec.shape != "enriched" else 2048
                        )
                    if _use_openrouter_fallback is not None:
                        log.warning(
                            "sector %s: all Cerebras models 429'd; "
                            "falling through to OpenRouter.",
                            spec.name,
                        )
                        llm = _use_openrouter_fallback
                        _provider_label = "openrouter"
                        rl_attempts = 0
                        net_attempts = 0
                        timeout_attempts = 0
                        last_exc = None
                        tools = all_search_tools(
                            cfg, ledger, set(prior_urls_sample),
                        )
                        agent = FunctionAgent(
                            tools=tools, llm=llm,
                            system_prompt=_system_prompt, verbose=cfg.verbose,
                        )
                        continue
                    log.warning(
                        "sector %s: all Cerebras models exhausted and no "
                        "OpenRouter fallback; returning default.",
                        spec.name,
                    )
                    return spec.default

                # OpenRouter 429 path — rotate through _OPENROUTER_MODEL_CHAIN
                # before giving up. Free models share an 8-RPM cap per model
                # but the chain ends with :floor paid backstops which have
                # provider-native limits (~3000 RPM) and use account credit.
                if _provider_label == "openrouter":
                    current_model = getattr(llm, "model", None)
                    next_model = (
                        _rotate_openrouter_on_429(current_model)
                        if current_model else None
                    )
                    if next_model is not None:
                        rotated = _build_openrouter_llm(
                            max_tokens=4096 if spec.shape != "enriched" else 2048,
                            model=next_model,
                        )
                        if rotated is not None:
                            log.info(
                                "sector %s: openrouter 429 on %s → rotating to %s.",
                                spec.name, current_model, next_model,
                            )
                            llm = rotated
                            rl_attempts = 0
                            net_attempts = 0
                            timeout_attempts = 0
                            last_exc = None
                            tools = all_search_tools(
                                cfg, ledger, set(prior_urls_sample),
                            )
                            agent = FunctionAgent(
                                tools=tools, llm=llm,
                                system_prompt=_system_prompt,
                                verbose=cfg.verbose,
                            )
                            continue  # retry with rotated OR model
                    # OR chain fully exhausted — bail.
                    emit_llm_call(
                        provider=_provider_label,
                        model=getattr(llm, "model", "unknown"),
                        label="research_sector",
                        sector=spec.name,
                        latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                        ok=False,
                        error="openrouter_chain_exhausted",
                    )
                    log.warning(
                        "sector %s: openrouter chain exhausted (%s); "
                        "returning default.",
                        spec.name, e,
                    )
                    return spec.default

                # Unknown provider 429 (shouldn't happen — _provider_label is
                # set to "cerebras" or "openrouter" elsewhere). Conservative
                # same-model retry with sleep.
                if rl_attempts >= len(_ratelimit_delays):
                    emit_llm_call(
                        provider=_provider_label,
                        model=getattr(llm, "model", "unknown"),
                        label="research_sector",
                        sector=spec.name,
                        latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                        ok=False,
                        error="rate_limit_exhausted",
                    )
                    log.warning(
                        "sector %s: %s 429 on all %d rate-limit retries (%s); "
                        "returning default.",
                        spec.name, _provider_label, rl_attempts + 1, e,
                    )
                    return spec.default
                delay = _ratelimit_delays[rl_attempts]
                log.warning(
                    "sector %s: %s 429 rate-limit (attempt %d) — sleeping %ds.",
                    spec.name, _provider_label, rl_attempts + 1, delay,
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
                    emit_llm_call(
                        provider=_provider_label,
                        model=getattr(llm, "model", "unknown"),
                        label="research_sector",
                        sector=spec.name,
                        latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                        ok=False,
                        error=type(e).__name__,
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
            elif _is_stream_timeout(e):
                if timeout_attempts >= len(_timeout_delays):
                    log.warning(
                        "sector %s: stream timeout on all %d retries (%s); "
                        "returning default.",
                        spec.name, timeout_attempts + 1, e,
                    )
                    emit_llm_call(
                        provider=_provider_label,
                        model=getattr(llm, "model", "unknown"),
                        label="research_sector",
                        sector=spec.name,
                        latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                        ok=False,
                        error="stream_timeout_exhausted",
                    )
                    return spec.default
                delay = _timeout_delays[timeout_attempts]
                log.warning(
                    "sector %s: stream timeout (attempt %d, %s) — "
                    "retrying in %ds.",
                    spec.name, timeout_attempts + 1, e, delay,
                )
                await asyncio.sleep(delay)
                timeout_attempts += 1
            else:
                log.warning(
                    "sector %s: agent crashed (%s); returning default",
                    spec.name, e,
                )
                return spec.default
    else:
        log.warning("sector %s: retry loop guard triggered; returning default.", spec.name)
        return spec.default

    # Guard: if no search-provider quota moved, the agent answered from
    # training data without calling any external tools — return default so
    # the write phase never sees hallucinated findings.
    if spec.name not in _NO_QUOTA_CHECK and not _quota_increased(pre_quota, ledger):
        log.warning(
            "sector %s: no search provider was called — output likely hallucinated; "
            "returning default.",
            spec.name,
        )
        return spec.default

    if response is None:
        log.warning("sector %s: agent.run() returned None; returning default.", spec.name)
        return spec.default

    raw = str(response)
    parsed = _parse_sector_output(raw, spec)

    if isinstance(parsed, _ParseFailed):
        log.warning(
            "sector %s: malformed JSON (raw_len=%d); returning default.",
            spec.name, len(parsed.raw),
        )
        return spec.default

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


# Proper-noun cluster extractor — used to build distinguishing dedup labels
# from findings strings whose first sentence is a generic topic header
# ("AI policy update.") and whose distinguishing detail lives in sentence 2+.
# Captures sequences of 2-5 Title-Case tokens (people, places, orgs, paper
# titles) plus uppercase acronyms ≥2 chars (UN, NATO, OFAC).
_PROPER_NOUN_RE = re.compile(
    r"\b(?:[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+){1,4}|[A-Z]{2,6})\b"
)


def _distinguishing_label(text: str, fallback_max_chars: int = 250) -> str:
    """Return a dedup label that captures the *distinguishing* portion of a
    findings string, not just its left prefix.

    Strategy:
      1. If the text has 1+ proper-noun cluster (length ≥2 tokens OR
         acronym ≥2 chars), join the FIRST TWO clusters with " | ". This
         catches "Trump tariffs | Asia" vs "Trump tariffs | Europe" which
         a left-prefix scheme misses when both stories start "Trump tariffs
         continue to..."
      2. Otherwise fall back to ``_first_two_sentences`` (the previous
         behaviour) — preserves dedup signal for stories without
         identifiable proper nouns (rare in news but common in vague
         summaries).

    The result is intentionally ≤80 chars in the common case so the write
    phase's 80-char prompt-truncation doesn't lop off the second cluster.
    """
    text = (text or "").strip()
    if not text:
        return ""
    clusters = _PROPER_NOUN_RE.findall(text)
    # Drop near-duplicates (case-insensitive) preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for c in clusters:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            unique.append(c)
    if len(unique) >= 2:
        label = f"{unique[0]} | {unique[1]}"
        return label[:77] + "…" if len(label) > 80 else label
    if len(unique) == 1:
        # One cluster — combine with first 40 chars of surrounding text for
        # context (avoids two unrelated "OpenAI" stories collapsing to one
        # dedup key).
        cluster = unique[0]
        # Find the cluster's position; take ~40 chars after it as context.
        pos = text.find(cluster)
        if pos != -1:
            tail = text[pos + len(cluster) : pos + len(cluster) + 50].strip()
            label = f"{cluster}: {tail}" if tail else cluster
            return label[:77] + "…" if len(label) > 80 else label
        return cluster
    return _first_two_sentences(text, max_chars=fallback_max_chars)


def collect_headlines_from_sector(value: Any) -> list[str]:
    """Pull human-facing labels out of a sector's parsed JSON for day-over-day dedup.

    Extracts both explicit headline-keyed fields (title, headline, role, etc.)
    AND a distinguishing label from any ``findings`` string. The fallback
    label generation prefers proper-noun clusters over a generic left-prefix
    truncation — see ``_distinguishing_label`` rationale.
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
                # Proper-noun-anchored label (was: _first_two_sentences which
                # lost distinguishing detail to write-phase 80-char prefix
                # truncation when sentence 1 was a generic topic header).
                label = _distinguishing_label(v)
                if label:
                    out.append(label)
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

    Comparison runs on `canonical_url` (lowercase host, www/m/amp stripped,
    utm-family query params dropped, fragment dropped) so the same article
    landing in three sectors with three different decorations
    (`?utm_source=email`, `m.foo.com`, trailing `#section`) collapses to one
    dupe entry instead of three distinct ones the write phase ignores.

    Returns canonical URLs (the write phase compares against canonical-form
    URLs computed identically downstream).
    """
    from .dedup import canonical_url

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
                key = canonical_url(url)
                if not key:
                    continue
                url_to_sectors.setdefault(key, []).append(field)

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
