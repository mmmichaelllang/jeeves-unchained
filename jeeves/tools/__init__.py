"""Tool registry — exposes search/enrichment tools to the research FunctionAgent."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from llama_index.core.tools import FunctionTool

    from ..config import Config
    from .quota import QuotaLedger


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
                "depth ('basic'|'advanced'). Use 'advanced' sparingly (2x credits)."
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
                "'deep', 'deep-reasoning'), text_max_chars (int=20000)."
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
                "Last-resort full-text fetcher (trafilatura). Use when Tavily extract "
                "fails. Args: url (str). Returns {url, title, text, fetch_failed}."
            ),
        ),
        FunctionTool.from_defaults(
            fn=fetch_talk_of_the_town(prior_urls),
            name="fetch_new_yorker_talk_of_the_town",
            description=(
                "Fetch the latest New Yorker 'Talk of the Town' article not already "
                "covered. Returns {available, title, section, dek, text, url, source}. "
                "Call once per run. Args: none."
            ),
        ),
    ]
    return tools
