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

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from .config import Config

log = logging.getLogger(__name__)


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
            "Today's weather for Edmonds, Washington (47.81, -122.38). Use serper_search "
            "with tbs='qdr:d' or gemini_grounded_synthesize for a quick forecast summary. "
            "Return a single plain-text string (no JSON wrapper), ~300-600 chars, covering "
            "conditions, precipitation, temps, comfort."
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
            "major assaults, armed incidents, missing persons. Reject petty crime. Use "
            "serper_search with tbs='qdr:d' for the last-24h filter. "
            "Return a JSON array of objects: [{category, source, findings, urls}, ...]."
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
            "Return a JSON object: {openings: [{district, role, url, summary}, ...], "
            "notes: '...'}."
        ),
        default={},
    ),
    SectorSpec(
        name="family",
        shape="dict",
        instruction=(
            "Two subkeys. 'choir': Seattle/Puget Sound choral auditions (Seattle Choral Co, "
            "Seattle Pro Musica, Northwest Chorale, etc.). 'toddler': Edmonds activities for "
            "a 2-year-old (library storytime, Imagine Children's Museum, Woodland Park Zoo, "
            "Sno-Isle Libraries). "
            "Return {choir: 'findings string', toddler: 'findings string', urls: [...]}."
        ),
        default={},
    ),
    SectorSpec(
        name="global_news",
        shape="list",
        instruction=(
            "Global news, today. Sources: BBC, CNN, Al Jazeera, The Guardian, NPR, "
            "Memeorandum, NYT. Use serper_search with tbs='qdr:d' plus tavily_search. "
            "Return a JSON array of {category, source, findings, urls} — aim for 4-8 "
            "distinct stories across the sources."
        ),
        default=[],
    ),
    SectorSpec(
        name="intellectual_journals",
        shape="list",
        instruction=(
            "Long-form intellectual journals: NYRB, New Yorker (NOT Talk of the Town), "
            "Aeon, Marginalian, Kottke, ProPublica, The Intercept, Scientific American, "
            "LRB, Arts & Letters Daily, Big Think, Jacobin, OpenSecrets. Prefer exa_search "
            "with search_type='auto' or 'deep-lite'. "
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
            "Use exa_search with search_type='deep' or 'deep-reasoning' for multi-step "
            "synthesis. IMPORTANT: the same series (e.g. Karl-Alber 'Studies on Triadic "
            "Ontology') may appear in prior coverage. Prefer to find the NEXT uncovered "
            "volume, paper, or author — check prior_urls and avoid repeating what is there. "
            "Begin your findings prose with the specific TITLE and AUTHOR of each paper or "
            "volume discussed so that covered-headline matching works correctly. "
            "Return {findings: 'long-form prose 500-1000 chars, opening with title/author "
            "of each item covered', urls: [...]}."
        ),
        default={"findings": "", "urls": []},
    ),
    SectorSpec(
        name="ai_systems",
        shape="deep",
        instruction=(
            "Deep research: multi-agent research systems, reasoning models, autonomous "
            "research pipelines, prompt-engineering advances. Use exa_search with "
            "search_type='deep'. Return {findings, urls}."
        ),
        default={"findings": "", "urls": []},
    ),
    SectorSpec(
        name="uap",
        shape="deep",
        instruction=(
            "Deep research: UAP disclosure, congressional hearings, non-human intelligence "
            "declassification. Recent developments only. Return {findings, urls}."
        ),
        default={"findings": "", "urls": []},
    ),
    SectorSpec(
        name="newyorker",
        shape="newyorker",
        instruction=(
            "Call fetch_new_yorker_talk_of_the_town() exactly once. It returns "
            "{available, title, section, dek, text, url, source}. Return that result "
            "verbatim as a JSON object. If available=false, return the object as-is."
        ),
        default={"available": False, "title": "", "section": "", "dek": "",
                 "text": "", "url": "", "source": "The New Yorker"},
    ),
    SectorSpec(
        name="enriched_articles",
        shape="enriched",
        instruction=(
            "You'll receive a list of candidate URLs that appeared in prior sectors. "
            "Pick the ~5 most novel and important, then call tavily_extract to fetch "
            "their full text. Fall back to fetch_article_text for any Tavily refuses. "
            "Return a JSON array of {url, source, title, fetch_failed, text} — one entry "
            "per extracted URL."
        ),
        default=[],
    ),
]


CONTEXT_HEADER = """You are researching ONE sector of Mister Michael Lang's daily briefing.

Context:
- Date: {date} (UTC). Treat as authoritative.
- Location: Edmonds, Washington (47.810652, -122.377355).
- Household: Mister Michael Lang, Mrs. Sarah Lang (wife, music teacher, choral),
  Piper (2-year-old daughter).

Prior coverage URLs (already briefed, do not revisit):
{prior_urls_sample}

Dedup guidance: if you encounter any URL in the prior list above, skip it.
Do not fabricate sources; every URL you include must come from a tool response.

Tool budget for this sector: 10-15 tool calls is plenty. Dispatch in parallel
when possible, then stop calling tools and output the JSON result.

SECTOR: {sector_name}
INSTRUCTION: {instruction}

When you have enough findings, STOP calling tools and output JSON matching the
instruction's shape. No markdown fences. No prose before or after the JSON.
For a string-shape sector, output the raw string (no quotes)."""


def _parse_sector_output(raw: str, spec: SectorSpec) -> Any:
    """Coerce the agent's final text into the sector-shape value."""

    text = (raw or "").strip()
    # Strip common markdown fences.
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()

    if spec.shape == "string":
        return text

    # Find the outermost JSON token.
    if spec.shape in ("list", "enriched"):
        start, end = text.find("["), text.rfind("]")
    else:
        start, end = text.find("{"), text.rfind("}")

    if start < 0 or end <= start:
        log.warning(
            "sector %s: no JSON %s found in output; returning default",
            spec.name, "array" if spec.shape in ("list", "enriched") else "object",
        )
        return spec.default

    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        log.warning("sector %s: JSON parse failed: %s; returning default", spec.name, e)
        return spec.default


def _build_user_prompt(spec: SectorSpec, run_date: str, prior_urls_sample: list[str],
                       extra: str = "") -> str:
    prior_block = "\n".join(prior_urls_sample) if prior_urls_sample else "(none)"
    base = CONTEXT_HEADER.format(
        date=run_date,
        prior_urls_sample=prior_block,
        sector_name=spec.name,
        instruction=spec.instruction,
    )
    return f"{base}\n\n{extra}" if extra else base


async def run_sector(
    cfg: Config,
    spec: SectorSpec,
    prior_urls_sample: list[str],
    ledger,
    *,
    extra_user: str = "",
) -> Any:
    """Run one sector's agent and return the parsed sector-shape value."""

    from llama_index.core.agent.workflow import FunctionAgent

    from .llm import build_kimi_llm
    from .tools import all_search_tools

    # Each sector gets its own agent, LLM, and tool instances so no state
    # leaks across runs (the quota ledger is the only shared object and is
    # inherently cumulative).
    tools = all_search_tools(cfg, ledger, set(prior_urls_sample))
    llm = build_kimi_llm(cfg)

    user_msg = _build_user_prompt(spec, cfg.run_date.isoformat(), prior_urls_sample, extra_user)
    agent = FunctionAgent(
        tools=tools,
        llm=llm,
        system_prompt=(
            "You are the per-sector research agent for Jeeves. Follow the user's "
            "instruction exactly. Stop calling tools once you have enough findings "
            "and return ONLY the requested JSON (or raw string for string-shape). "
            "Zero hallucination — cite only URLs returned by tools."
        ),
        verbose=cfg.verbose,
    )

    log.info("sector %s: agent starting.", spec.name)
    try:
        response = await agent.run(user_msg)
    except Exception as e:
        log.warning("sector %s: agent crashed (%s); returning default", spec.name, e)
        return spec.default

    raw = str(response)
    parsed = _parse_sector_output(raw, spec)
    log.info(
        "sector %s: parsed %s (len=%s)",
        spec.name, type(parsed).__name__,
        len(parsed) if hasattr(parsed, "__len__") else "-",
    )
    return parsed


def collect_urls_from_sector(value: Any) -> list[str]:
    """Best-effort URL extraction for dedup accumulation + enriched_articles seeding."""

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
                out.extend(str(u) for u in v if u)
            elif k == "url" and isinstance(v, str) and v:
                out.append(v)
            else:
                out.extend(collect_urls_from_sector(v))
    return out


_HEADLINE_KEYS = {"title", "headline", "subject", "role", "event", "district"}


def collect_headlines_from_sector(value: Any) -> list[str]:
    """Pull human-facing labels (titles, headlines, event names, job roles) out of
    a sector's parsed JSON so downstream dedup can skip day-over-day repeats.
    Complements `collect_urls_from_sector` for items that don't carry a URL
    (events, job openings, correspondence threads)."""

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
            elif isinstance(v, (dict, list)):
                out.extend(collect_headlines_from_sector(v))
    return out


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
