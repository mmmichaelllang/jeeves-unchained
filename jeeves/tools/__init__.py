"""Tool registry — exposes search/enrichment tools to the research FunctionAgent.

Naming taxonomy (sprint-19 slice E)
-----------------------------------

Tools are grouped by *role*. Roles are stable; concrete tool names may grow
as canaries promote. Prompts reference roles ("dispatch a web_search +
deep_research pair") but agent tool-pick is description-keyed, so renames
are deliberately avoided — see ``TOOL_TAXONOMY`` below for the role → tools
map. New peers register under an existing role rather than introducing a
new tool surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llama_index.core.tools import FunctionTool

    from ..config import Config
    from .quota import QuotaLedger


# ---------------------------------------------------------------------------
# Role taxonomy — keep flat. One row per registered tool name. Role choice
# tells the eval harness, the rate_limits tier table, and the research
# prompt's budget block what each tool is *for*. Adding a new tool means
# adding a row here AND updating jeeves/prompts/research_system.md.
# ---------------------------------------------------------------------------

TOOL_TAXONOMY: dict[str, dict[str, str]] = {
    # -- web_search: ranked SERP, possibly with snippet text --
    "serper_search":          {"role": "web_search",     "tier": "1", "billing": "monthly"},
    "tavily_search":          {"role": "web_search",     "tier": "2", "billing": "monthly"},
    "jina_search":            {"role": "web_search",     "tier": "1", "billing": "daily"},
    "tinyfish_search":        {"role": "web_search",     "tier": "3", "billing": "daily"},
    "playwright_search":      {"role": "web_search",     "tier": "3", "billing": "free"},
    # -- semantic_search: neural / long-form discovery --
    "exa_search":             {"role": "semantic_search","tier": "1", "billing": "monthly"},
    # -- deep_research: synthesis + grounding --
    "gemini_grounded_synthesize": {"role": "deep_research", "tier": "2", "billing": "daily"},
    "vertex_grounded_search":     {"role": "deep_research", "tier": "2", "billing": "daily"},
    "jina_deepsearch":            {"role": "deep_research", "tier": "3", "billing": "daily"},
    # -- rerank: reorder candidate union --
    "jina_rerank":            {"role": "rerank",         "tier": "2", "billing": "daily"},
    # -- extract: full-text fetch --
    "tavily_extract":         {"role": "extract",        "tier": "2", "billing": "monthly"},
    "fetch_article_text":     {"role": "extract",        "tier": "1", "billing": "free"},
    "tinyfish_extract":       {"role": "extract",        "tier": "3", "billing": "daily"},
    "playwright_extract":     {"role": "extract",        "tier": "3", "billing": "free"},
    # -- curated_feed: single-source publication scraper --
    "fetch_new_yorker_talk_of_the_town": {"role": "curated_feed", "tier": "1", "billing": "free"},
}


def tools_for_role(role: str) -> list[str]:
    """Return the registered tool names that fulfil *role*.

    Used by the eval harness to enumerate web_search providers without
    re-listing them and by the research prompt's budget table.
    """
    return [name for name, meta in TOOL_TAXONOMY.items() if meta.get("role") == role]


def all_search_tools(
    cfg: "Config",
    ledger: "QuotaLedger",
    prior_urls: set[str],
) -> list["FunctionTool"]:
    """Return the full set of search + enrichment tools for the research agent."""

    from llama_index.core.tools import FunctionTool

    from .enrichment import fetch_article_text
    from .exa import make_exa_search
    from .gemini_grounded import make_gemini_grounded
    from .serper import make_serper_search
    from .talk_of_the_town import fetch_talk_of_the_town
    from .tavily import make_tavily_extract, make_tavily_search
    from .vertex_search import make_vertex_grounded

    tools = [
        FunctionTool.from_defaults(
            fn=make_serper_search(cfg, ledger),
            name="serper_search",
            description=(
                "Google SERP via Serper.dev. Best for: breaking news, local events, "
                "time-filtered queries. Cheapest search. Args: query (str), num (int=10), "
                "tbs (str|None, e.g. 'qdr:d' for last 24h, 'qdr:w' for last 7 days)."
            ),
        ),
        FunctionTool.from_defaults(
            fn=make_tavily_search(cfg, ledger),
            name="tavily_search",
            description=(
                "Tavily AI-native search with synthesized answer. Best for: multi-source "
                "research questions. Args: query (str), max_results (int=8), "
                "depth ('basic'|'advanced' — use 'advanced' sparingly, 2x credits), "
                "time_range (str|None: 'day'|'week'|'month'|'year' — biases toward "
                "freshness; pass 'week' for non-breaking sectors to prevent evergreen "
                "results re-ranking into top results day-over-day, 'day' for breaking)."
            ),
        ),
        FunctionTool.from_defaults(
            fn=make_tavily_extract(cfg, ledger),
            name="tavily_extract",
            description=(
                "Full-text extraction for up to 20 URLs via Tavily. Preferred enrichment "
                "path after ranking search results. Args: urls (list[str])."
            ),
        ),
        FunctionTool.from_defaults(
            fn=make_exa_search(cfg, ledger),
            name="exa_search",
            description=(
                "Exa neural semantic search with full-text content. Best for: "
                "intellectual journals, long-form essays, 'find similar to X' "
                "queries, academic-adjacent content. Returns both snippet and "
                "capped full text, so you often do NOT need to follow up with "
                "tavily_extract on Exa hits. Args: query (str), "
                "num_results (int=10), category (str|None — valid values: "
                "'news', 'research paper', 'company', 'pdf', 'personal site', "
                "'financial report', 'people' — use None if unsure), "
                "search_type (str='auto' — also 'fast', 'instant', 'deep-lite', "
                "'deep', 'deep-reasoning'), text_max_chars (int=20000), "
                "start_published_date (str|None, 'YYYY-MM-DD' — restricts to "
                "content published on or after the date; pass the value of "
                "{seven_days_ago} from the prompt to bias against evergreen "
                "pages re-ranking day-over-day)."
            ),
        ),
        FunctionTool.from_defaults(
            fn=make_gemini_grounded(cfg, ledger),
            name="gemini_grounded_synthesize",
            description=(
                "Gemini 2.5 Flash with Google Search grounding (standard API). Returns a "
                "synthesized answer plus citation URLs. Hard daily cap: 1,490/day (Google "
                "free tier is 1,500 — stops 10 below to guarantee no charges). Use for "
                "'current state of X' questions where a narrative answer is more useful "
                "than a raw result list. Args: question (str)."
            ),
        ),
        FunctionTool.from_defaults(
            fn=make_vertex_grounded(cfg, ledger),
            name="vertex_grounded_search",
            description=(
                "Vertex AI Gemini with Dynamic Google Search grounding. Only invokes "
                "Search when model confidence < 0.3 (Dynamic Retrieval) — minimises "
                "actual search calls while still grounding answers in current web content. "
                "Hard daily cap: 1,490/day. Returns {answer, citations}. Disabled "
                "silently if GOOGLE_CLOUD_PROJECT is not set. Args: question (str)."
            ),
        ),
        FunctionTool.from_defaults(
            fn=fetch_article_text,
            name="fetch_article_text",
            description=(
                "Full-text fetcher (httpx + trafilatura, with automatic Playwright "
                "fallback when both fail). Use when Tavily extract returns thin or "
                "missing content for a URL. Args: url (str). Returns "
                "{url, title, text, fetch_failed}."
            ),
        ),
        FunctionTool.from_defaults(
            fn=_make_playwright_extract_tool(ledger),
            name="playwright_extract",
            description=(
                "Last-resort full-text fetcher: headless Chromium (Playwright) + "
                "OpenRouter free-model markdown crystallizer. Use this ONLY when "
                "tavily_extract AND fetch_article_text have both failed for a URL "
                "(common on JS-heavy SPAs, soft paywalls, or Cloudflare-fronted "
                "sites). Slower than other extractors (~5-15s). Args: url (str). "
                "Returns JSON string with {url, title, text, success, error?}. "
                "Returns success=false if playwright is not installed in this "
                "environment — soft-fail; pick another URL in that case."
            ),
        ),
        FunctionTool.from_defaults(
            fn=fetch_talk_of_the_town(prior_urls, jina_api_key=cfg.jina_api_key),
            name="fetch_new_yorker_talk_of_the_town",
            description=(
                "Fetch the latest New Yorker 'Talk of the Town' article not already "
                "covered. Returns {available, title, section, dek, text, url, source}. "
                "Call once per run. Args: none."
            ),
        ),
    ]

    # ---- Optional: TinyFish managed extractor (sprint-18 canary) ----
    # Registered only when BOTH the secret is present AND the explicit opt-in
    # flag is set. Mirrors the JEEVES_PW_USE_LLM_CRYSTALLIZE pattern so the
    # agent surface is unchanged on default runs while we collect comparison
    # data via the shadow path.
    import os as _os

    if (
        _os.environ.get("JEEVES_USE_TINYFISH", "").strip() == "1"
        and _os.environ.get("TINYFISH_API_KEY", "").strip()
    ):
        tools.append(
            FunctionTool.from_defaults(
                fn=_make_tinyfish_extract_tool(ledger),
                name="tinyfish_extract",
                description=(
                    "Managed-browser extractor (TinyFish). Use as a peer to "
                    "playwright_extract on JS-heavy SPAs, soft paywalls, and "
                    "Cloudflare-fronted hosts when tavily_extract and "
                    "fetch_article_text both fail. Faster cold-start than "
                    "playwright; counts against a 30/day hard cap. Args: "
                    "url (str). Returns JSON string with "
                    "{url, title, text, success, extracted_via, error?}."
                ),
            )
        )

    # ---- Sprint-19: search-agent canaries (Jina suite + tinyfish_search +
    # playwright_search). All default-off, individually flagged so each can
    # promote independently per EVAL_GATE thresholds. Ordering is descriptive
    # — the agent picks tools by description text (see CLAUDE.md sprint-19
    # rationale), not list order.
    if (
        _os.environ.get("JEEVES_USE_JINA_SEARCH", "").strip() == "1"
        and cfg.jina_api_key
    ):
        from .jina import make_jina_search

        tools.append(
            FunctionTool.from_defaults(
                fn=make_jina_search(cfg, ledger),
                name="jina_search",
                description=(
                    "Jina AI search (s.jina.ai). CHOOSE WHEN you need ranked "
                    "URLs WITH clean extracted snippets in one call. PREFER "
                    "OVER serper_search when the same query also wants "
                    "follow-up text — Jina's snippets often eliminate a "
                    "tavily_extract follow-up. DO NOT USE for navigational "
                    "lookups (use serper_search). Args: query (str), "
                    "num (int=8), site (str|None for site-scope filter). "
                    "Hard cap: 200/day. Returns JSON {provider, query, "
                    "results: [{title, url, snippet, published_at, source, "
                    "provider}]}."
                ),
            )
        )

    if (
        _os.environ.get("JEEVES_USE_JINA_DEEPSEARCH", "").strip() == "1"
        and cfg.jina_api_key
    ):
        from .jina import make_jina_deepsearch

        tools.append(
            FunctionTool.from_defaults(
                fn=make_jina_deepsearch(cfg, ledger),
                name="jina_deepsearch",
                description=(
                    "Jina DeepSearch — agentic multi-hop search-read-reason. "
                    "CHOOSE WHEN a single question needs 5+ citations from "
                    "multiple sources (e.g. 'state of triadic ontology in "
                    "2026'). PREFER OVER gemini_grounded_synthesize when "
                    "you need a deeper citation set than a 3-paragraph "
                    "narrative. Slow (15-90s) but ONE call replaces 5+ "
                    "Serper/Tavily/extract chains. Hard cap: 20/day. "
                    "Args: question (str), reasoning_effort "
                    "('low'|'medium'|'high'='low'). Returns JSON "
                    "{provider, question, answer, citations: "
                    "[{url,title}], visited_urls}."
                ),
            )
        )

    if (
        _os.environ.get("JEEVES_USE_JINA_RERANK", "").strip() == "1"
        and cfg.jina_api_key
    ):
        from .jina import make_jina_rerank

        tools.append(
            FunctionTool.from_defaults(
                fn=make_jina_rerank(cfg, ledger),
                name="jina_rerank",
                description=(
                    "Jina semantic reranker. CHOOSE WHEN you have ≥10 "
                    "candidate results unioned from 2+ search_* calls and "
                    "want to pick the top N before extraction. Cheap "
                    "(~ms/pair). Args: query (str), documents "
                    "(list[str] — 'title || url || snippet' joined per "
                    "candidate works), top_n (int=8). Hard cap: 100/day. "
                    "Returns JSON {provider, query, ranked: "
                    "[{index, score, document}]}."
                ),
            )
        )

    if (
        _os.environ.get("JEEVES_USE_TINYFISH_SEARCH", "").strip() == "1"
        and _os.environ.get("TINYFISH_API_KEY", "").strip()
    ):
        tools.append(
            FunctionTool.from_defaults(
                fn=_make_tinyfish_search_tool(ledger),
                name="tinyfish_search",
                description=(
                    "TinyFish managed-browser search. CHOOSE WHEN the "
                    "target is a JS-heavy site (LinkedIn, X, Instagram, "
                    "paywalled SPAs) where serper_search returns thin "
                    "metadata, OR when site-scoped queries need real "
                    "rendering. PREFER OVER serper_search for "
                    "site:linkedin.com / site:x.com queries. Set "
                    "include_raw_content=True to return SERP + full "
                    "rendered markdown in ONE call. Hard cap: 8/day "
                    "(weighted ~2 credits each, raw=5). Args: query (str), "
                    "num (int=10), include_raw_content (bool=False), "
                    "site (str|None). Returns JSON {provider, query, "
                    "success, results: [{title, url, snippet, content?}]}."
                ),
            )
        )

    if _os.environ.get("JEEVES_USE_PLAYWRIGHT_SEARCH", "").strip() == "1":
        tools.append(
            FunctionTool.from_defaults(
                fn=_make_playwright_search_tool(ledger),
                name="playwright_search",
                description=(
                    "Headless-browser SERP scrape (DuckDuckGo/Bing/Brave). "
                    "CHOOSE WHEN you need a free Serper peer for diversity "
                    "(union with serper_search before jina_rerank) or when "
                    "Serper quota is exhausted. Zero API cost; ~1.2s/call "
                    "on warm singleton. Args: query (str), engine "
                    "('ddg'|'bing'|'brave'='ddg'), num (int=10). "
                    "Hard cap: 60/day (wall-clock guard). Returns JSON "
                    "{provider, query, engine, success, results: "
                    "[{title, url, snippet, provider}]}."
                ),
            )
        )

    return tools


def _make_tinyfish_extract_tool(ledger: "QuotaLedger"):
    """Build a TinyFish extractor tool that records quota usage.

    Records monthly + daily counters per call so the research_sectors quota
    guard recognises a TinyFish-only sector as having performed real work
    AND so the daily 30-call hard cap fires before runaway spend.
    """
    def _tinyfish_extract_tool(url: str) -> str:
        """FunctionTool wrapper around tinyfish.extract_article.

        Returns a JSON string so LlamaIndex's _parse_tool_output() produces
        a valid JSON TextBlock in the NIM context (matching the contract
        enforced on every other tool — see notes in research_sectors.py).
        """
        import json as _json

        try:
            from .tinyfish import extract_article

            result = extract_article(
                url, timeout_seconds=30, max_chars=4000, ledger=ledger
            )
        except Exception as e:
            return _json.dumps({
                "url": url,
                "success": False,
                "title": "",
                "text": "",
                "extracted_via": "tinyfish",
                "error": f"tinyfish extractor crashed: {e}",
            })
        return _json.dumps(result)

    return _tinyfish_extract_tool


def _make_tinyfish_search_tool(ledger: "QuotaLedger"):
    """Build a TinyFish search tool that returns a JSON string and records
    quota usage (sprint-19)."""

    def _tinyfish_search_tool(
        query: str = "",
        num: int = 10,
        include_raw_content: bool = False,
        site: str | None = None,
    ) -> str:
        import json as _json

        try:
            from .tinyfish import search as _tf_search

            result = _tf_search(
                query,
                num=num,
                include_raw_content=include_raw_content,
                site=site,
                ledger=ledger,
            )
        except Exception as e:
            return _json.dumps(
                {
                    "provider": "tinyfish_search",
                    "query": query,
                    "success": False,
                    "results": [],
                    "error": f"tinyfish_search crashed: {e}",
                }
            )
        return _json.dumps(result)

    return _tinyfish_search_tool


def _make_playwright_search_tool(ledger: "QuotaLedger"):
    """Build a Playwright SERP-scrape tool that returns a JSON string and
    records quota usage (sprint-19)."""

    def _playwright_search_tool(
        query: str = "",
        engine: str = "ddg",
        num: int = 10,
    ) -> str:
        import json as _json

        try:
            from .playwright_extractor import search as _pw_search

            result = _pw_search(query, engine=engine, num=num, ledger=ledger)
        except Exception as e:
            return _json.dumps(
                {
                    "provider": "playwright_search",
                    "query": query,
                    "engine": engine,
                    "success": False,
                    "results": [],
                    "error": f"playwright_search crashed: {e}",
                }
            )
        return _json.dumps(result)

    return _playwright_search_tool


def _make_playwright_extract_tool(ledger: "QuotaLedger"):
    """Build a Playwright extractor tool that records quota usage.

    Records a "playwright" quota entry per call so the research_sectors quota
    guard recognises a Playwright-only sector as having performed real work
    (and therefore not a hallucinated empty default).
    """
    def _playwright_extract_tool(url: str) -> str:
        """FunctionTool wrapper around playwright_extractor.extract_article.

        Returns a JSON string so LlamaIndex's _parse_tool_output() produces a
        valid JSON TextBlock in the NIM context (matching the contract enforced
        on every other tool — see notes in research_sectors.py).
        """
        import json as _json

        try:
            from .playwright_extractor import extract_article

            result = extract_article(url, timeout_seconds=30, max_chars=4000)
        except Exception as e:
            return _json.dumps({
                "url": url,
                "success": False,
                "title": "",
                "text": "",
                "error": f"playwright extractor crashed: {e}",
            })

        try:
            ledger.record("playwright", 1)
        except Exception:
            pass
        return _json.dumps(result)

    return _playwright_extract_tool
