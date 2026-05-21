#!/usr/bin/env python3
"""
M0 probe: Crawl4AI vs trafilatura+Jina on jeeves-target URLs.

Reports TWO extraction strategies per URL:
  strict_fit   — fit_markdown only, BM25 threshold tested at both 1.0 and 0.2
  combined     — fit_markdown if ≥200 chars, else raw_markdown[:8000]

DECISION logic:
  PROCEED only if combined strategy overall score ≥0.8
  Otherwise REVISE M1-M3

Usage:
    uv run python scripts/diagnostics/probe_crawl4ai.py          # full run
    uv run python scripts/diagnostics/probe_crawl4ai.py --quick  # 2 URLs per bucket

Output:
    decisions/crawl4ai-probe-YYYY-MM-DD.md
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import trafilatura

# ---------------------------------------------------------------------------
# URL buckets — actual jeeves research targets including paywalled sources.
# Paywall failure is diagnostic information, not noise.
# ---------------------------------------------------------------------------

FULL_URLS: dict[str, list[str]] = {
    "intellectual_journals": [
        # Paywalled — expected failure is signal
        "https://www.nybooks.com/articles/2024/01/18/the-man-who-broke-the-music-business/",
        "https://www.lrb.co.uk/the-paper/v46/n01/jenny-turner/the-joy-of-boredom",
        # Open-access
        "https://aeon.co/essays/the-philosophical-case-for-caring-about-people-who-don-t-exist-yet",
        "https://marginalrevolution.com/marginalrevolution/2024/01/assorted-links-392.html",
    ],
    "local_news": [
        # Hard paywall
        "https://www.nytimes.com/2024/01/15/nyregion/nyc-migrants-shelter-crisis.html",
        # Open
        "https://www.theguardian.com/us-news/2024/jan/15/new-york-city-migrant-crisis-shelter",
        "https://gothamist.com/news/nyc-to-close-randalls-island-migrant-shelter",
    ],
    "wearable_ai": [
        "https://github.com/BasedHardware/OpenGlass",
        "https://news.ycombinator.com/item?id=39686046",
        "https://www.reddit.com/r/singularity/comments/1b6m5lz/humane_ai_pin_review_roundup/",
    ],
    "global_news": [
        "https://en.wikipedia.org/wiki/2024_United_States_presidential_election",
        "https://www.bbc.com/news/world-us-canada-67945976",
        "https://apnews.com/article/immigration-migrants-new-york-city-shelter-20240115",
    ],
}

QUICK_URLS: dict[str, list[str]] = {k: v[:2] for k, v in FULL_URLS.items()}


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    bucket: str
    # Crawl4AI outputs
    fit_markdown_10: str = ""   # BM25 threshold=1.0 (original)
    fit_markdown_02: str = ""   # BM25 threshold=0.2 (permissive)
    raw_markdown: str = ""
    crawl4ai_latency_s: float = 0.0
    crawl4ai_error: str = ""
    # Comparison fetchers
    trafilatura_text: str = ""
    trafilatura_latency_s: float = 0.0
    trafilatura_error: str = ""
    jina_text: str = ""
    jina_latency_s: float = 0.0
    jina_error: str = ""
    # Scores
    score_strict_10: float = 0.0
    score_strict_02: float = 0.0
    score_combined: float = 0.0   # fit_02 if ≥200 chars, else raw[:8000]
    score_notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# BM25 threshold justification (from crawl4ai source + GitHub issues)
# ---------------------------------------------------------------------------
# crawl4ai BM25ContentFilter: threshold controls the minimum number of
# standard deviations above the mean BM25 score a sentence needs to be
# retained. Default in crawl4ai docs/examples is 0.2–0.5.
# Threshold=1.0 keeps only the top ~16% of sentences (1 std above mean),
# which over-filters nav-heavy pages where BM25 distribution is wide.
# Threshold=0.2 keeps roughly the top ~42% of sentences.
# Both are tested here so the caller can choose.
# References:
#   crawl4ai/content_filter_strategy.py — BM25ContentFilter.__init__
#   https://docs.crawl4ai.com/extraction/fit-markdown/


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------

async def _crawl_with_threshold(url: str, threshold: float, timeout: int) -> str:
    """Return fit_markdown text for a single BM25 threshold."""
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
    from crawl4ai.content_filter_strategy import BM25ContentFilter
    from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

    bm25 = BM25ContentFilter(user_query="news article content", bm25_threshold=threshold)
    md_gen = DefaultMarkdownGenerator(content_filter=bm25)
    cfg = CrawlerRunConfig(
        markdown_generator=md_gen,
        cache_mode=CacheMode.BYPASS,
        page_timeout=timeout * 1000,
        wait_until="domcontentloaded",
    )
    async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
        result = await crawler.arun(url=url, config=cfg)
    if not result.success:
        return ""
    return result.markdown.fit_markdown or "" if result.markdown else ""


async def fetch_crawl4ai(url: str, timeout: int = 30) -> tuple[str, str, str, float, str]:
    """Return (fit_10, fit_02, raw, latency_s, error)."""
    try:
        from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode
        from crawl4ai.content_filter_strategy import BM25ContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator

        t0 = time.monotonic()
        # First pass: BM25=1.0 (strict) — also captures raw_markdown
        bm25_strict = BM25ContentFilter(user_query="news article content", bm25_threshold=1.0)
        md_gen_strict = DefaultMarkdownGenerator(content_filter=bm25_strict)
        cfg = CrawlerRunConfig(
            markdown_generator=md_gen_strict,
            cache_mode=CacheMode.BYPASS,
            page_timeout=timeout * 1000,
            wait_until="domcontentloaded",
        )
        async with AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False)) as crawler:
            result = await crawler.arun(url=url, config=cfg)

        if not result.success:
            latency = time.monotonic() - t0
            return "", "", "", latency, result.error_message or "success=False"

        fit_10 = result.markdown.fit_markdown or "" if result.markdown else ""
        raw = result.markdown.raw_markdown or "" if result.markdown else ""

        # Second pass: BM25=0.2 (permissive) — reuse same page
        fit_02 = await _crawl_with_threshold(url, threshold=0.2, timeout=timeout)

        latency = time.monotonic() - t0
        return fit_10, fit_02, raw, latency, ""
    except Exception as exc:
        return "", "", "", 0.0, str(exc)


def fetch_trafilatura(url: str, timeout: int = 20) -> tuple[str, float, str]:
    try:
        t0 = time.monotonic()
        downloaded = trafilatura.fetch_url(url, no_ssl=True)
        text = trafilatura.extract(downloaded, include_comments=False, include_tables=False) or ""
        return text, time.monotonic() - t0, ""
    except Exception as exc:
        return "", 0.0, str(exc)


async def fetch_jina(url: str, api_key: str = "", timeout: int = 20) -> tuple[str, float, str]:
    jina_url = f"https://r.jina.ai/{url}"
    headers = {"Accept": "text/plain"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        t0 = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(jina_url, headers=headers)
        latency = time.monotonic() - t0
        if resp.status_code != 200:
            return "", latency, f"HTTP {resp.status_code}"
        return resp.text, latency, ""
    except Exception as exc:
        return "", 0.0, str(exc)


# ---------------------------------------------------------------------------
# Scoring (same rubric applied to each strategy)
# ---------------------------------------------------------------------------

def _score_text(text: str, trafilatura_chars: int, trafilatura_error: str) -> tuple[float, list[str]]:
    """Score 0-1 for a single extracted text."""
    notes: list[str] = []
    score = 0.0
    chars = len(text)

    # 1. Content present (40 pts)
    if chars > 500:
        score += 0.4
        notes.append(f"content_present ({chars}c)")
    elif chars > 100:
        score += 0.2
        notes.append(f"content_sparse ({chars}c)")
    else:
        notes.append(f"content_missing ({chars}c)")

    # 2. Nav stripped — low link density (30 pts)
    snippet = text[:1000]
    link_count = snippet.count("](http")
    word_count = max(len(snippet.split()), 1)
    density = link_count / word_count
    if density < 0.15:
        score += 0.3
        notes.append(f"nav_stripped (density={density:.2f})")
    elif density < 0.35:
        score += 0.15
        notes.append(f"nav_partial (density={density:.2f})")
    else:
        notes.append(f"nav_heavy (density={density:.2f})")

    # 3. Competitive with trafilatura (30 pts)
    if trafilatura_error:
        score += 0.2
        notes.append("trafilatura N/A")
    elif chars >= max(trafilatura_chars * 0.7, 200):
        score += 0.3
        notes.append(f"vs_traf OK ({chars} vs {trafilatura_chars})")
    else:
        notes.append(f"vs_traf WORSE ({chars} vs {trafilatura_chars})")

    return round(score, 2), notes


def score_result(res: FetchResult) -> None:
    """Populate all three strategy scores on res."""
    traf_chars = len(res.trafilatura_text)

    if res.crawl4ai_error:
        res.score_notes = [f"FAIL: {res.crawl4ai_error[:80]}"]
        return  # all scores remain 0.0

    res.score_strict_10, notes_10 = _score_text(res.fit_markdown_10, traf_chars, res.trafilatura_error)
    res.score_strict_02, notes_02 = _score_text(res.fit_markdown_02, traf_chars, res.trafilatura_error)

    combined = res.fit_markdown_02 if len(res.fit_markdown_02) >= 200 else res.raw_markdown[:8000]
    res.score_combined, notes_combined = _score_text(combined, traf_chars, res.trafilatura_error)

    res.score_notes = (
        [f"strict_1.0: {notes_10}"]
        + [f"strict_0.2: {notes_02}"]
        + [f"combined:   {notes_combined}"]
    )


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _snippet(text: str, chars: int = 200) -> str:
    if not text:
        return "(empty)"
    return textwrap.shorten(text.replace("\n", " ")[:chars], width=chars, placeholder="…")


def write_report(results: list[FetchResult], output_path: Path) -> tuple[float, float, float]:
    """Write markdown report. Returns (overall_strict_10, overall_strict_02, overall_combined)."""
    lines: list[str] = []
    today = datetime.date.today()
    lines.append(f"# Crawl4AI Probe — {today}\n")
    lines.append(f"Generated: {datetime.datetime.utcnow().isoformat()}Z\n")
    lines.append("crawl4ai version: 0.8.6 | BM25 thresholds tested: 1.0 (strict) + 0.2 (permissive)\n")
    lines.append("")

    # Per-URL table
    lines.append("## Per-URL Results\n")
    lines.append("| URL | strict_1.0 | strict_0.2 | combined | trafilatura_chars | jina_chars |")
    lines.append("|-----|-----------|-----------|---------|-----------------|----------|")
    for res in results:
        short = res.url.replace("https://", "")[:55]
        lines.append(
            f"| {short} | {res.score_strict_10} | {res.score_strict_02} | {res.score_combined}"
            f" | {len(res.trafilatura_text)} | {len(res.jina_text)} |"
        )
    lines.append("")

    # Detailed per-URL section
    for res in results:
        lines.append(f"## {res.bucket} — {res.url}")
        lines.append(f"- crawl4ai latency: {res.crawl4ai_latency_s:.1f}s | error: {res.crawl4ai_error or 'none'}")
        lines.append(f"- fit_1.0: {len(res.fit_markdown_10)}c | fit_0.2: {len(res.fit_markdown_02)}c | raw: {len(res.raw_markdown)}c")
        lines.append(f"- fit_0.2 snippet: `{_snippet(res.fit_markdown_02)}`")
        lines.append(f"- trafilatura: {len(res.trafilatura_text)}c | jina: {len(res.jina_text)}c")
        lines.append(f"- scores: strict_1.0={res.score_strict_10} strict_0.2={res.score_strict_02} combined={res.score_combined}")
        for n in res.score_notes:
            lines.append(f"  - {n}")
        lines.append("")

    # Overall scores
    def avg(key: str) -> float:
        vals = [getattr(r, key) for r in results]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    overall_10 = avg("score_strict_10")
    overall_02 = avg("score_strict_02")
    overall_combined = avg("score_combined")

    lines.append("## Strategy Summary\n")
    lines.append("| Strategy | Overall Score | Pass ≥0.8? |")
    lines.append("|----------|--------------|-----------|")
    lines.append(f"| strict_fit (BM25=1.0) | {overall_10} | {'YES' if overall_10 >= 0.8 else 'NO'} |")
    lines.append(f"| strict_fit (BM25=0.2) | {overall_02} | {'YES' if overall_02 >= 0.8 else 'NO'} |")
    lines.append(f"| combined (fit_0.2 or raw fallback) | {overall_combined} | {'YES' if overall_combined >= 0.8 else 'NO'} |")
    lines.append("")

    # Findings + DECISION
    lines.append("## Findings\n")

    # Paywall analysis
    paywalled = [r for r in results if r.crawl4ai_error or (len(r.fit_markdown_02) < 100 and len(r.raw_markdown) < 100)]
    if paywalled:
        lines.append(f"**Paywalled/inaccessible ({len(paywalled)} URLs):**")
        for r in paywalled:
            lines.append(f"  - {r.url} — traf={len(r.trafilatura_text)}c jina={len(r.jina_text)}c")
        lines.append("  These URLs require Jina as fallback regardless of crawl4ai strategy.\n")

    # BM25 threshold analysis
    improved = sum(1 for r in results if r.score_strict_02 > r.score_strict_10)
    lines.append(f"**BM25 threshold impact:** threshold=0.2 improved score vs 1.0 on {improved}/{len(results)} URLs.\n")

    lines.append(f"OVERALL SCORE (strict_1.0): {overall_10}")
    lines.append(f"OVERALL SCORE (strict_0.2): {overall_02}")
    lines.append(f"OVERALL SCORE (combined): {overall_combined}")

    if overall_combined >= 0.8:
        decision = "PROCEED with strategy=combined_fit_or_raw, threshold=0.2"
    else:
        decision = "REVISE M1-M3"
    lines.append(f"DECISION: {decision}")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return overall_10, overall_02, overall_combined


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def run(urls_by_bucket: dict[str, list[str]], jina_key: str) -> list[FetchResult]:
    results: list[FetchResult] = []
    for bucket, url_list in urls_by_bucket.items():
        for url in url_list:
            print(f"  [{bucket}] {url}")
            res = FetchResult(url=url, bucket=bucket)

            # Crawl4AI (both thresholds + raw in one call)
            fit_10, fit_02, raw, lat, err = await fetch_crawl4ai(url)
            res.fit_markdown_10 = fit_10
            res.fit_markdown_02 = fit_02
            res.raw_markdown = raw
            res.crawl4ai_latency_s = lat
            res.crawl4ai_error = err

            # Trafilatura
            text_t, lat_t, err_t = await asyncio.to_thread(fetch_trafilatura, url)
            res.trafilatura_text = text_t
            res.trafilatura_latency_s = lat_t
            res.trafilatura_error = err_t

            # Jina
            text_j, lat_j, err_j = await fetch_jina(url, api_key=jina_key)
            res.jina_text = text_j
            res.jina_latency_s = lat_j
            res.jina_error = err_j

            score_result(res)
            print(
                f"    → fit1.0={len(fit_10)}c fit0.2={len(fit_02)}c raw={len(raw)}c"
                f"  traf={len(text_t)}c jina={len(text_j)}c"
                f"  scores: {res.score_strict_10}/{res.score_strict_02}/{res.score_combined}"
            )
            results.append(res)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="M0 Crawl4AI probe")
    parser.add_argument("--quick", action="store_true", help="2 URLs per bucket")
    parser.add_argument("--out", default="", help="Override output path")
    args = parser.parse_args()

    urls = QUICK_URLS if args.quick else FULL_URLS
    jina_key = os.environ.get("JINA_API_KEY", "")

    today = datetime.date.today().isoformat()
    out_dir = Path("decisions")
    out_dir.mkdir(exist_ok=True)
    out_path = Path(args.out) if args.out else out_dir / f"crawl4ai-probe-{today}.md"

    total = sum(len(v) for v in urls.values())
    print(f"Probing {total} URLs ({'quick' if args.quick else 'full'} mode)...")
    print("Strategies: strict_fit (BM25=1.0), strict_fit (BM25=0.2), combined (fit_0.2 or raw)")
    results = asyncio.run(run(urls, jina_key))

    print(f"\nWriting report → {out_path}")
    s10, s02, sc = write_report(results, out_path)
    print(f"OVERALL SCORE: strict_1.0={s10}  strict_0.2={s02}  combined={sc}")
    print(f"DECISION: {'PROCEED with strategy=combined_fit_or_raw, threshold=0.2' if sc >= 0.8 else 'REVISE M1-M3'}")
    return 0 if sc >= 0.8 else 1


if __name__ == "__main__":
    raise SystemExit(main())
