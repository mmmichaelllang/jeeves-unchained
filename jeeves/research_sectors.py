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


# NIM stream-drop / tool-call-leak markers that occasionally land inside
# `findings` strings when the streaming response is truncated mid tool-call.
# Sprint-19 hardening: truncate at the first match before _parse_sector_output
# returns so the corrupt suffix never reaches the write phase.
#
# Two-tier detection:
#   * literal markers — explicit tool-call delimiters (NIM Hermes-style).
#   * regex markers   — catch partial-word merges produced by streaming drops
#     (e.g. ``...pieces thatily_extract:5`` from the 2026-05-04 corruption).
_NIM_TOOL_CALL_MARKERS: tuple[str, ...] = (
    "<|tool_call_argument_begin|>",
    "<|tool_call_argument_end|>",
    "<|tool_call",
    "<|tool ",
    "functions.tavily_extract:",
    "functions.tavily_search:",
    "functions.serper_search:",
    "functions.exa_search:",
    "functions.gemini_grounded",
    "functions.fetch_article_text",
    "functions.playwright_extract",
    "functions.tinyfish_extract",
    "functions.vertex_grounded",
)

# Bare tool-name regex — catches ``tavily_extract:``, ``...thatily_extract:5``
# (streaming drop ate the leading consonants), ``functions.tavily_extract``,
# and ``<|tool…``. The suffix ``_(extract|search|grounded|synthesize)`` is
# unlikely in normal prose but a reliable fingerprint of leaked tool-call
# JSON.
_NIM_TOOL_CALL_REGEX = re.compile(
    r"\w*_(?:extract|search|grounded|synthesize)\s*[:=]"
    r"|functions\.[a-z_]+\s*[:=]?"
    r"|<\|tool",
    re.IGNORECASE,
)


def _strip_tool_call_markup(text: str) -> str:
    """Truncate at the first NIM tool-call leak marker; preserve clean prefix."""
    if not isinstance(text, str) or not text:
        return text
    earliest = -1
    for marker in _NIM_TOOL_CALL_MARKERS:
        idx = text.find(marker)
        if idx != -1 and (earliest == -1 or idx < earliest):
            earliest = idx
    m = _NIM_TOOL_CALL_REGEX.search(text)
    if m is not None and (earliest == -1 or m.start() < earliest):
        earliest = m.start()
    if earliest == -1:
        return text
    cleaned = text[:earliest].rstrip()
    # Don't return a sentence-fragment ending mid-clause — drop a trailing
    # incomplete final sentence if we cut mid-stream.
    if cleaned and cleaned[-1] not in ".!?\"'”’":
        cut = max(
            cleaned.rfind("."),
            cleaned.rfind("!"),
            cleaned.rfind("?"),
        )
        if cut > 50:  # keep at least one full prior sentence
            cleaned = cleaned[: cut + 1]
    return cleaned


def _sanitise_findings_markup(parsed: Any, spec: SectorSpec) -> Any:
    """Scrub NIM tool-call leak from ``findings`` strings.

    Only items whose findings were ACTUALLY modified (i.e. contained tool-call
    markup that got stripped) are subject to the post-strip 20-char quality
    floor — items with naturally-short findings are left to the existing
    list-shape quality filter so behaviour stays additive.
    """
    def _scrub_dict(d: dict) -> tuple[dict, bool]:
        modified = False
        if isinstance(d.get("findings"), str):
            cleaned = _strip_tool_call_markup(d["findings"])
            if cleaned != d["findings"]:
                modified = True
                log.warning(
                    "sector %s: stripped NIM tool-call markup from findings (%d → %d chars)",
                    spec.name, len(d["findings"]), len(cleaned),
                )
                d["findings"] = cleaned
        return d, modified

    if isinstance(parsed, list):
        scrubbed: list = []
        for item in parsed:
            if isinstance(item, dict):
                item, modified = _scrub_dict(item)
                if (
                    modified
                    and isinstance(item.get("findings"), str)
                    and len(item["findings"].strip()) < 20
                ):
                    log.warning(
                        "sector %s: dropping item — findings collapsed below "
                        "20 chars after tool-call markup strip.",
                        spec.name,
                    )
                    continue
            scrubbed.append(item)
        return scrubbed
    if isinstance(parsed, dict):
        scrubbed_dict, _ = _scrub_dict(parsed)
        return scrubbed_dict
    return parsed


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

    # Sprint-19: sanitise NIM tool-call markup leaking into prose. When NIM's
    # streaming output is truncated mid tool-call (e.g. ``functions.tavily_extract:5
    # <|tool_call_argument_begin|>{...``) the partial markup ends up inside a
    # `findings` string and ships verbatim into the briefing. Detect and
    # truncate at the first marker, then drop any item whose findings collapses
    # below 20 chars after sanitisation.
    parsed = _sanitise_findings_markup(parsed, spec)

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
    # 2026-05-10: intellectual_journals leaked the same 3 sticky URLs (Champy/
    # Proust, Irving/republican-heritage, Popova/Sacks) for a week. Forced-
    # retry path now applies to this sector too (see retry_when_all_overlap
    # logic below). Query selection biases away from the long-tail aeon /
    # marginalian essays that Kimi memorises.
    "intellectual_journals": (
        "NYRB LRB long-form essay this week 2026 -republican -proust "
        "-sacks-perception"
    ),
}

# Sectors that should ALSO force-retry when the quota guard passes BUT the
# parsed URLs overlap heavily with prior_urls. Different failure mode from
# the quota-bypass: tools WERE called, but the model picked URLs it has
# memorised that happen to be in prior_urls.
_FORCE_RETRY_ON_OVERLAP: frozenset[str] = frozenset({"intellectual_journals"})
# Threshold — when this fraction or more of the parsed URLs are in prior_urls,
# treat as a sticky-URL failure and force-retry.
_OVERLAP_RETRY_THRESHOLD = 0.5


# 2026-05-10 (PR #113 follow-up). Host-authority table for the
# intellectual_journals sticky-URL retry adoption gate. Higher = more
# trusted long-form publication; default 0.4 for unknown hosts. Used to
# prevent adopting a retry that swaps three sticky high-quality URLs
# (NYRB, Aeon, Marginalian) for four blogspam URLs.
_INTELLECTUAL_JOURNAL_HOST_SCORES: dict[str, float] = {
    "nybooks.com": 0.92, "www.nybooks.com": 0.92,
    "lrb.co.uk": 0.92, "www.lrb.co.uk": 0.92,
    "aeon.co": 0.88, "www.aeon.co": 0.88,
    "themarginalian.org": 0.85, "www.themarginalian.org": 0.85,
    "harpers.org": 0.88, "www.harpers.org": 0.88,
    "newyorker.com": 0.85, "www.newyorker.com": 0.85,
    "propublica.org": 0.85, "www.propublica.org": 0.85,
    "theintercept.com": 0.82, "www.theintercept.com": 0.82,
    "jacobin.org": 0.78, "www.jacobin.org": 0.78,
    "jacobinmag.com": 0.78, "www.jacobinmag.com": 0.78,
    "jewishcurrents.org": 0.78, "www.jewishcurrents.org": 0.78,
    "nplusonemag.com": 0.85, "www.nplusonemag.com": 0.85,
    "dissentmagazine.org": 0.78, "www.dissentmagazine.org": 0.78,
    "thebaffler.com": 0.78, "www.thebaffler.com": 0.78,
    "bostonreview.net": 0.78, "www.bostonreview.net": 0.78,
    "nybooks.org": 0.92,  # alias seen in some redirect URLs
    "scientificamerican.com": 0.72,
    "www.scientificamerican.com": 0.72,
    "bigthink.com": 0.65, "www.bigthink.com": 0.65,
    "kottke.org": 0.7,
    "tabletmag.com": 0.7, "www.tabletmag.com": 0.7,
    # Mass-market quality (lower than literary journals but still substantive)
    "theatlantic.com": 0.7, "www.theatlantic.com": 0.7,
    "newrepublic.com": 0.7, "www.newrepublic.com": 0.7,
    "newstatesman.com": 0.7, "www.newstatesman.com": 0.7,
}
# Default for hosts not in the table — generic blog/unknown.
_INTELLECTUAL_JOURNAL_DEFAULT_SCORE: float = 0.4


# 2026-05-10 PR #113 follow-up — LLM-judge replaces deterministic table.
# Per-URL cache so the judge never gets called twice for the same URL in
# the same process. Keyed by URL string. Reset on import (one cache per
# Python process; the daily pipeline is a fresh process).
_IJ_LLM_SCORE_CACHE: dict[str, float] = {}

# OpenRouter free-tier judge prompt. We deliberately don't require JSON —
# free-tier models are flaky on JSON; we extract a number from the response.
_IJ_JUDGE_SYSTEM = (
    "You score how well a candidate URL fits the editorial brief for the "
    "INTELLECTUAL JOURNALS section of a daily morning briefing. The brief "
    "wants substantive long-form essay content from serious publications "
    "(NYRB, LRB, Aeon, The New Yorker long-form, Harpers, Marginalian, "
    "ProPublica, Intercept, Jacobin, Jewish Currents, Boston Review, "
    "n+1, The Baffler, Dissent, Lapham's Quarterly, Granta, similar). "
    "Output ONLY a single decimal number between 0.0 and 1.0 — nothing "
    "else. No explanation, no JSON, no prose. Examples:\n"
    "  - NYRB long-form essay → 0.92\n"
    "  - Aeon essay on philosophy → 0.88\n"
    "  - Harpers feature → 0.88\n"
    "  - serious blog post on Substack → 0.55\n"
    "  - SEO content-farm article → 0.20\n"
    "  - homepage of any publication → 0.10\n"
    "  - tag/category index page → 0.15\n"
    "  - off-topic news article (sports/celebrity/local) → 0.30\n"
    "Return only the number."
)


def _llm_score_intellectual_journal_url(
    url: str, finding: str, openrouter_api_key: str,
) -> float | None:
    """Ask an OpenRouter free-tier model to rate this URL's topical fit.

    Returns score in [0, 1] on success; None on any failure (LLM key
    absent, HTTP error, model returned non-numeric, etc.). Caller must
    fall back to the deterministic host-authority table on None.

    Per-URL cache so we don't double-spend on the same URL in one run.
    """
    if not url or not openrouter_api_key:
        return None
    if url in _IJ_LLM_SCORE_CACHE:
        return _IJ_LLM_SCORE_CACHE[url]
    try:
        from openai import OpenAI
    except ImportError:
        return None
    try:
        from jeeves.audit_models import resolve_audit_models
    except Exception:
        # audit_models may not be importable in some smoke contexts;
        # fall back to a small built-in chain.
        def resolve_audit_models() -> tuple[str, ...]:
            return (
                "qwen/qwen3-next-80b-a3b-instruct:free",
                "meta-llama/llama-3.3-70b-instruct:free",
            )

    client = None
    try:
        client = OpenAI(
            api_key=openrouter_api_key,
            base_url="https://openrouter.ai/api/v1",
            timeout=30.0,
        )
    except Exception as exc:
        log.debug("IJ judge: client init failed (%s)", exc)
        return None

    finding_excerpt = (finding or "").strip()
    if len(finding_excerpt) > 800:
        finding_excerpt = finding_excerpt[:800].rstrip() + " […]"
    user_msg = (
        f"URL: {url}\n"
        f"Finding excerpt: {finding_excerpt or '(none provided)'}\n"
        f"Score (0.0-1.0, just the number):"
    )
    import re as _re
    for model_id in resolve_audit_models():
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=[
                    {"role": "system", "content": _IJ_JUDGE_SYSTEM},
                    {"role": "user", "content": user_msg},
                ],
                max_tokens=16,
                temperature=0.0,
            )
            text = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            log.debug("IJ judge [%s] failed: %s", model_id, exc)
            continue
        # Extract the first decimal in [0, 1].
        m = _re.search(r"\b([01](?:\.\d+)?|0?\.\d+)\b", text)
        if not m:
            log.debug("IJ judge [%s] returned non-numeric: %r", model_id, text[:60])
            continue
        try:
            score = float(m.group(1))
        except ValueError:
            continue
        score = max(0.0, min(1.0, score))
        _IJ_LLM_SCORE_CACHE[url] = score
        log.info("IJ judge [%s] %s -> %.2f", model_id, url, score)
        return score
    log.warning("IJ judge: all models exhausted for %s", url)
    return None


def _score_intellectual_journals_url(
    url: str, finding: str = "", cfg: Config | None = None,
) -> float:
    """Quality score for an intellectual_journals retry URL.

    PRIMARY: LLM-judge — reads URL + associated finding excerpt, rates 0-1.
    FALLBACK: deterministic host-authority + path-quality table.

    The LLM judge generalises beyond the hand-curated host table —
    catches new high-authority outlets (Granta, Lapham's Quarterly,
    Boston Review specials) without manual upkeep, and catches
    blogspam on otherwise-trusted hosts (NYRB tag pages etc.).

    Falls back deterministically on any LLM failure so hermetic tests
    + LLM outages never break the adoption gate.
    """
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return 0.0
    # LLM judge first — only when cfg + key are available. The free-tier
    # call is small (16 tokens out, single number) so per-PR cost is
    # bounded by the IJ retry frequency × URLs-per-retry × cache.
    if cfg is not None:
        api_key = getattr(cfg, "openrouter_api_key", "") or ""
        if api_key:
            llm = _llm_score_intellectual_journal_url(url, finding, api_key)
            if llm is not None:
                return llm
    # Deterministic fallback — host table + path penalties.
    from urllib.parse import urlparse
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    base = _INTELLECTUAL_JOURNAL_HOST_SCORES.get(
        host, _INTELLECTUAL_JOURNAL_DEFAULT_SCORE
    )
    path = parsed.path.lower()
    if path in ("", "/"):
        base -= 0.4
    for marker in ("/tag/", "/tags/", "/category/", "/categories/",
                   "/topic/", "/topics/", "/search", "/page/", "/issue/"):
        if marker in path:
            base -= 0.25
            break
    import re as _re
    if _re.search(r"/20\d{2}/(0?[1-9]|1[0-2])/", path):
        base += 0.05
    return max(0.0, min(1.0, base))


def _avg_score_intellectual_journals(
    items: Iterable[Any], cfg: Config | None = None,
) -> float:
    """Mean URL-quality score for a set of intellectual_journals candidates.

    Accepts either:
      - iterable of URL strings (legacy / fallback)
      - iterable of (url, finding) tuples (preferred — gives the LLM
        judge content excerpts to score against)

    Empty sequence → 0.0 (always loses the adoption gate).
    """
    items = list(items)
    if not items:
        return 0.0
    total = 0.0
    n = 0
    for it in items:
        if isinstance(it, tuple) and len(it) == 2:
            url, finding = it
        else:
            url, finding = it, ""
        total += _score_intellectual_journals_url(url, finding=finding, cfg=cfg)
        n += 1
    return total / n if n else 0.0


def _extract_url_finding_pairs(parsed: Any) -> list[tuple[str, str]]:
    """Walk a parsed sector result and return (url, finding) pairs.

    For list-of-dicts shape (intellectual_journals), each item carries
    its own findings prose; we pair each URL in the item with that text.
    For other shapes (deep, dict-with-subkeys), findings may be None;
    pairs default the finding to "".
    """
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(url: Any, finding: Any) -> None:
        if not isinstance(url, str):
            return
        u = url.strip()
        if not u.startswith(("http://", "https://")) or u in seen:
            return
        seen.add(u)
        f = finding if isinstance(finding, str) else ""
        pairs.append((u, f))

    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            finding = item.get("findings") or item.get("summary") or ""
            for u in item.get("urls") or []:
                _add(u, finding)
            single = item.get("url")
            if single:
                _add(single, finding)
    elif isinstance(parsed, dict):
        # Deep-sector shape OR dict-with-subkeys.
        finding = parsed.get("findings") or ""
        for u in parsed.get("urls") or []:
            _add(u, finding)
        for v in parsed.values():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        sub_f = item.get("summary") or item.get("findings") or ""
                        sub_u = item.get("url") or ""
                        _add(sub_u, sub_f)
    return pairs


def _extract_urls_from_parsed(parsed: Any) -> list[str]:
    """Pull every URL string out of a parsed sector result (any shape).

    Used by the sticky-URL forced-retry detector. Handles list-of-dicts
    (most sectors), dict-with-urls (deep sectors), and dict-with-subkeys
    (english_lesson_plans, family). Returns deduplicated list preserving
    insertion order.
    """
    seen: list[str] = []
    seen_set: set[str] = set()

    def _add(u: Any) -> None:
        if not isinstance(u, str):
            return
        u = u.strip()
        if u.startswith(("http://", "https://")) and u not in seen_set:
            seen.append(u)
            seen_set.add(u)

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("url", "link", "href"):
                    _add(v)
                elif k in ("urls", "links"):
                    if isinstance(v, list):
                        for u in v:
                            _add(u)
                    else:
                        _walk(v)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(parsed)
    return seen


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
    # Sector-level LLM-call telemetry. Records ONE event per agent.run()
    # invocation — this is the high-water mark for NIM TPM pressure since
    # each run loops over many tool calls + Kimi inferences. Token usage is
    # rarely surfaced through the FunctionAgent abstraction (NIM streams),
    # so we record call-count + latency + ok and let the rollup show what's
    # available. No retry-attempt is double-counted: only the SUCCESSFUL or
    # FINALLY-FAILED outcome lands.
    import time as _t
    from .tools.telemetry import emit_llm_call

    _sector_t0 = _t.monotonic()

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
            emit_llm_call(
                provider="nim",
                model="kimi-k2",
                label="research_sector",
                sector=spec.name,
                latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                ok=True,
            )
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
                    emit_llm_call(
                        provider="nim",
                        model="kimi-k2",
                        label="research_sector",
                        sector=spec.name,
                        latency_ms=(_t.monotonic() - _sector_t0) * 1000,
                        ok=False,
                        error="rate_limit_exhausted",
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
                    emit_llm_call(
                        provider="nim",
                        model="kimi-k2",
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
            else:
                log.warning("sector %s: agent crashed (%s); returning default", spec.name, e)
                return spec.default
    else:
        log.warning("sector %s: retry loop guard triggered; returning default.", spec.name)
        return spec.default

    # Guard: if no search-provider quota moved, Kimi answered entirely from
    # training data without calling any external tools.  For deep sectors AND
    # sectors flagged for IJ-style forced retry, try one forced-search retry
    # before giving up. For all others, return default so the write phase
    # never sees hallucinated findings.
    if spec.name not in _NO_QUOTA_CHECK and not _quota_increased(pre_quota, ledger):
        if spec.shape == "deep" or spec.name in _FORCE_RETRY_ON_OVERLAP:
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

    # 2026-05-10 — sticky-URL forced retry. Some sectors (intellectual_journals
    # observed) call search tools but return URLs the model has memorised that
    # are already in prior_urls. The quota guard above passes (tools were
    # called) so the leak goes through. Detect by computing overlap between
    # parsed URLs and prior_urls; if at or above _OVERLAP_RETRY_THRESHOLD,
    # force-retry with the deep-sector pattern.
    if (
        spec.name in _FORCE_RETRY_ON_OVERLAP
        and not isinstance(parsed, _ParseFailed)
    ):
        parsed_urls = _extract_urls_from_parsed(parsed)
        if parsed_urls:
            prior_set = {u.strip() for u in prior_urls_sample if u}
            overlap = sum(1 for u in parsed_urls if u in prior_set)
            ratio = overlap / len(parsed_urls)
            if ratio >= _OVERLAP_RETRY_THRESHOLD:
                log.warning(
                    "sector %s: %d/%d parsed URLs (%.0f%%) already in prior_urls "
                    "— sticky-URL leak detected; attempting forced-retry.",
                    spec.name, overlap, len(parsed_urls), ratio * 100,
                )
                retry_parsed = await _deep_sector_forced_retry(
                    cfg, spec, prior_urls_sample, ledger, sector_max_tokens
                )
                # 2026-05-10 (PR #113 follow-up). Adoption gate now scores
                # the URLs by host-authority + path-quality. Adopt the
                # retry only when both:
                #   (a) it has at least as many NEW (not-in-prior) URLs as
                #       the original had sticky URLs, AND
                #   (b) the average quality score of the retry's NEW URLs
                #       is at least 0.05 above the original's sticky URLs.
                # Prevents trading three high-authority sticky URLs (NYRB
                # / Aeon / Marginalian) for four blogspam URLs.
                retry_urls = _extract_urls_from_parsed(retry_parsed)
                retry_new_urls = [u for u in retry_urls if u not in prior_set]
                sticky_urls = [u for u in parsed_urls if u in prior_set]
                if spec.name == "intellectual_journals":
                    # 2026-05-10 PR #113 follow-up: score with LLM judge
                    # using the FINDING text associated with each URL,
                    # not just the URL string. Falls back to deterministic
                    # host table when LLM unavailable.
                    retry_pairs = _extract_url_finding_pairs(retry_parsed)
                    sticky_pairs = _extract_url_finding_pairs(parsed)
                    retry_new_pairs = [(u, f) for u, f in retry_pairs
                                       if u not in prior_set]
                    sticky_only_pairs = [(u, f) for u, f in sticky_pairs
                                         if u in prior_set]
                    new_avg = _avg_score_intellectual_journals(
                        retry_new_pairs, cfg=cfg,
                    )
                    sticky_avg = _avg_score_intellectual_journals(
                        sticky_only_pairs, cfg=cfg,
                    )
                else:
                    # Non-IJ sectors fall back to count-based gate.
                    new_avg = float(len(retry_new_urls))
                    sticky_avg = float(len(sticky_urls))
                quality_uplift = new_avg - sticky_avg
                count_threshold = len(retry_new_urls) >= len(sticky_urls)
                quality_threshold = (
                    spec.name != "intellectual_journals"
                    or quality_uplift >= 0.05
                )
                if retry_urls and count_threshold and quality_threshold:
                    log.info(
                        "sector %s: forced-retry adopted "
                        "(new=%d, sticky=%d, new_avg=%.3f, sticky_avg=%.3f, uplift=%+.3f).",
                        spec.name, len(retry_new_urls), len(sticky_urls),
                        new_avg, sticky_avg, quality_uplift,
                    )
                    parsed = retry_parsed
                else:
                    log.warning(
                        "sector %s: forced-retry rejected "
                        "(new=%d, sticky=%d, new_avg=%.3f, sticky_avg=%.3f, "
                        "uplift=%+.3f) — keeping original.",
                        spec.name, len(retry_new_urls), len(sticky_urls),
                        new_avg, sticky_avg, quality_uplift,
                    )

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
