#!/usr/bin/env python3
"""Skill-body graduator (partial — works from session JSON only).

The Autobrowse pattern's full leverage is iterative: run → study trace →
strategy.md → iterate → graduate. Jeeves doesn't currently capture full
agent traces, so the graduation we can do today operates on the artifact
we DO have — the daily session JSON.

What this script does
---------------------
For each skill in jeeves/site_skills/registry/, walks the last N days of
session-*.json for the skill's sectors and writes a freshly-rendered
"## Observed in last N days" section into the skill markdown:

  - Host distribution (which domains the sector actually pulled from)
  - New-vs-prior URL ratio (signal for whether the sector is rotating
    or stuck)
  - Stable producers (hosts that yielded a NEW URL on >= K of the last N
    days — those queries are working; keep them)
  - Stuck producers (hosts that yielded the SAME URL on >= K days — those
    queries should rotate)

The section is bracketed by sentinel markers so subsequent runs replace
their own previous output cleanly.

What this script doesn't do
---------------------------
Re-write the "## Workflow" section. That requires actual agent traces
(tool-call sequences, query strings, costs per turn) which we don't yet
record. Once telemetry-*.jsonl carries query strings + tokens-per-call
the body graduator can be extended to suggest concrete query rewrites.
For now, the observed-data block is the actionable input a human (or a
future-stronger graduator) uses to edit the Workflow section.

Usage
-----
    python scripts/graduate_skill_body.py
    python scripts/graduate_skill_body.py --days 14 --stable-floor 4
    python scripts/graduate_skill_body.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Reuse helpers from graduate_skip_lists to keep canonicalisation aligned.
from scripts.graduate_skip_lists import (  # noqa: E402
    REGISTRY_DIR,
    SESSIONS_DIR,
    _collect_sector_urls,
    _parse_skill_metadata,
    _strip_url,
    _utc_today,
    _walk_sessions,
)

OBSERVED_HEADING = "## Observed in last N days — auto-graduated"
BEGIN_MARKER = "<!-- observed:begin -->"
END_MARKER = "<!-- observed:end -->"


# ---------------------------------------------------------------------------
# Telemetry-driven query analysis (Patch I, 2026-05-09)
#
# `tool_call` telemetry events from sessions/telemetry-*.jsonl record the
# `query` and `provider` of every search-tool invocation. By correlating
# query-frequency with the same window's stuck/stable producer analysis from
# session JSON, we can suggest concrete query rewrites: queries that fired
# many times but produced stuck-host hits are candidates to drop or narrow.
#
# Limitation: tool_call events do NOT currently record the URLs returned per
# call, only the count. So we can't directly attribute a stuck URL to its
# originating query. The suggester is heuristic: it lists queries by usage
# frequency and flags sectors with high stuck-producer counts as needing
# query rotation generally.
# ---------------------------------------------------------------------------

def _walk_tool_call_events(sessions_dir: Path, days: int) -> list[dict]:
    """Read tool_call events from telemetry-*.jsonl in the same window."""
    today = _utc_today()
    out: list[dict] = []
    for delta in range(days):
        d = today - timedelta(days=delta)
        path = sessions_dir / f"telemetry-{d.isoformat()}.jsonl"
        if not path.exists():
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(ev, dict) and ev.get("event") == "tool_call":
                        ev["_source_date"] = d.isoformat()
                        out.append(ev)
        except OSError:
            continue
    return out


SEARCH_PROVIDERS = frozenset({
    "serper", "exa", "tavily", "jina_search", "jina_deepsearch",
    "gemini_grounded", "vertex_grounded", "tinyfish_search",
    "playwright_search",
})


def _queries_for_sector_hosts(
    events: list[dict], hosts: list[str],
) -> list[tuple[str, str, int, int]]:
    """Aggregate tool_call queries that hit the relevant providers.

    Returns list of (provider, query, calls, ok_calls) sorted by calls desc.
    Filters to queries from search providers (serper, exa, tavily, jina,
    gemini, vertex, tinyfish, playwright). Hosts arg is currently unused
    by this function — see ``_queries_returning_stuck_urls`` for per-URL
    correlation now that tool_call telemetry carries ``urls_returned``.
    """
    g: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"calls": 0, "ok": 0}
    )
    for ev in events:
        provider = str(ev.get("provider") or "")
        query = str(ev.get("query") or "").strip()
        if not query or provider not in SEARCH_PROVIDERS:
            continue
        key = (provider, query)
        g[key]["calls"] += 1
        if ev.get("ok", True):
            g[key]["ok"] += 1
    return sorted(
        [(p, q, v["calls"], v["ok"]) for (p, q), v in g.items()],
        key=lambda t: -t[2],
    )


def _queries_returning_stuck_urls(
    events: list[dict], stuck_urls: list[str],
) -> list[tuple[str, str, int]]:
    """Correlate queries with stuck-URL appearances using ``urls_returned``.

    Reads tool_call events emitted by serper/exa/tavily/jina (post 2026-05-09)
    that include the top-10 returned URLs. For each (provider, query) pair,
    counts how many of the stuck URLs it has returned over the window.
    Returns list of (provider, query, stuck_hits) sorted by stuck_hits desc.

    Used to give the rewrite suggester a precise rather than heuristic
    target — the queries with the highest stuck-hit count are the actual
    rotation candidates.
    """
    if not stuck_urls:
        return []
    stuck_set = {u for u in stuck_urls if u}
    if not stuck_set:
        return []

    g: dict[tuple[str, str], int] = defaultdict(int)
    for ev in events:
        provider = str(ev.get("provider") or "")
        query = str(ev.get("query") or "").strip()
        if not query or provider not in SEARCH_PROVIDERS:
            continue
        urls_returned = ev.get("urls_returned") or []
        if not isinstance(urls_returned, list):
            continue
        for u in urls_returned:
            if not isinstance(u, str):
                continue
            if u in stuck_set or _strip_url(u) in stuck_set:
                g[(provider, query)] += 1
    return sorted(
        [(p, q, hits) for (p, q), hits in g.items()],
        key=lambda t: -t[2],
    )


def _host_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _per_day_url_sets(
    sessions: list[tuple[date, dict]], sector: str,
) -> dict[date, set[str]]:
    return {d: set(_collect_sector_urls(s, sector)) for d, s in sessions}


def _rolling_prior_set(
    per_day: dict[date, set[str]], up_to: date,
) -> set[str]:
    """Union of all sets strictly BEFORE `up_to` (the prior set as the
    sector would have seen on `up_to`)."""
    prior: set[str] = set()
    for d, urls in per_day.items():
        if d < up_to:
            prior |= urls
    return prior


def _analyze_sector(
    sessions: list[tuple[date, dict]], sector: str, *, stable_floor: int,
) -> dict:
    per_day = _per_day_url_sets(sessions, sector)
    days_with_data = [d for d, urls in per_day.items() if urls]
    total_days = len(days_with_data)
    if total_days == 0:
        return {
            "total_days": 0,
            "host_dist": [],
            "new_vs_prior": [],
            "stable_producers": [],
            "stuck_producers": [],
        }

    # Host distribution across the window (day-weighted: each day counts a
    # host once even if it shipped 5 URLs).
    host_day_count: Counter = Counter()
    for d in days_with_data:
        hosts = {_host_of(u) for u in per_day[d] if u}
        for h in hosts:
            if h:
                host_day_count[h] += 1

    # New-vs-prior per day: of today's URLs, how many were NOT in the prior
    # rolling set?
    new_vs_prior = []
    for d in sorted(days_with_data):
        today_urls = per_day[d]
        prior = _rolling_prior_set(per_day, d)
        new_count = len([u for u in today_urls if u not in prior])
        new_vs_prior.append({
            "date": d.isoformat(),
            "new_count": new_count,
            "total_count": len(today_urls),
            "ratio": round(new_count / max(1, len(today_urls)), 2),
        })

    # Stable producers — hosts that produced a NEW URL on >= stable_floor
    # of the days. Stuck — hosts that produced the SAME URL on >=
    # stable_floor days.
    host_new_days: Counter = Counter()
    url_days: Counter = Counter()
    for d in sorted(days_with_data):
        prior = _rolling_prior_set(per_day, d)
        for u in per_day[d]:
            url_days[u] += 1
            if u not in prior:
                host_new_days[_host_of(u)] += 1

    stable = sorted(
        [(h, c) for h, c in host_new_days.items() if c >= stable_floor],
        key=lambda t: -t[1],
    )
    stuck_urls = sorted(
        [(u, c) for u, c in url_days.items() if c >= stable_floor],
        key=lambda t: -t[1],
    )

    return {
        "total_days": total_days,
        "host_dist": sorted(host_day_count.items(), key=lambda t: -t[1])[:15],
        "new_vs_prior": new_vs_prior,
        "stable_producers": stable[:10],
        "stuck_producers": stuck_urls[:15],
    }


def _render_query_block(
    *,
    queries: list[tuple[str, str, int, int]],
    stuck_count: int,
    days: int,
    stuck_query_correlation: list[tuple[str, str, int]] | None = None,
) -> str:
    """Render the queries-observed + suggested-rewrite block.

    `queries` is the output of `_queries_for_sector_hosts`. `stuck_count` is
    the number of stuck-producer URLs detected in session JSON; high values
    mean the current query set yields repeats. `stuck_query_correlation`
    (when telemetry has urls_returned) is the precise per-query stuck-hit
    count — used to name specific queries to rotate rather than guessing.
    """
    if not queries:
        return (
            "\n_No `tool_call` telemetry events recorded in window — "
            "set `JEEVES_TELEMETRY=1` to enable query analysis._\n"
        )
    lines: list[str] = []
    lines.append("**Queries observed** (provider · query · calls · ok-calls):")
    lines.append("")
    for provider, query, calls, ok in queries[:15]:
        q_short = query if len(query) <= 100 else query[:97] + "…"
        lines.append(f"- `{provider}` · `{q_short}` — {calls} call(s), {ok} ok")

    # Precise per-query stuck-correlation, available when serper/exa/tavily/
    # jina emit urls_returned (post-2026-05-09 telemetry).
    if stuck_query_correlation:
        lines.append("")
        lines.append(
            "**Stuck-URL correlation** (precise — queries that returned URLs "
            "currently in the stuck-producer list; rotate these first):"
        )
        lines.append("")
        for provider, query, hits in stuck_query_correlation[:10]:
            q_short = query if len(query) <= 100 else query[:97] + "…"
            lines.append(
                f"- `{provider}` · `{q_short}` — returned {hits} stuck URL(s)"
            )

    if stuck_count >= 3 and queries:
        lines.append("")
        if stuck_query_correlation:
            top = stuck_query_correlation[0]
            lines.append(
                f"**Suggested rewrite**: {stuck_count} URL(s) are stuck on "
                f">=4-day streaks. The query `{top[1][:80]}` (provider "
                f"`{top[0]}`) returned {top[2]} of them — rotate it FIRST. "
                "Adopt one of the narrower-query examples from the skill's "
                "Workflow section until at least one new URL surfaces."
            )
        else:
            lines.append(
                f"**Suggested rewrite**: {stuck_count} URL(s) are stuck on "
                ">=4-day ship streaks but `urls_returned` telemetry is "
                "missing — the precise per-query attribution is unavailable. "
                "Top queries above are likely contributing; rotate the "
                "highest-call ones first. To get precise attribution next "
                "run, ensure search-provider tool emits include "
                "`urls_returned=...` (serper/exa/tavily/jina patched 2026-05-09)."
            )
    elif stuck_count == 0 and queries:
        lines.append("")
        lines.append(
            "**Suggested rewrite**: 0 stuck URLs in window — current query "
            "set is producing diverse content. No rotation needed."
        )
    return "\n".join(lines) + "\n"


def _render_observed_section(
    *, sectors: list[str], analyses: dict, days: int, stable_floor: int,
    sector_queries: dict | None = None,
    sector_query_stuck_corr: dict | None = None,
) -> str:
    today = _utc_today().isoformat()
    out = [
        f"_Last graduated_: {today}  •  _Window_: {days}-day  •  "
        f"_Stable threshold_: {stable_floor} days",
        "",
    ]
    for sector in sectors:
        a = analyses.get(sector) or {}
        if not a or a.get("total_days", 0) == 0:
            out.append(f"### Sector: `{sector}` — _no data in window_")
            out.append("")
            continue
        out.append(f"### Sector: `{sector}` — {a['total_days']} day(s) with output")
        out.append("")
        if a["host_dist"]:
            out.append("**Host distribution** (host : day-count, top 15):")
            out.append("")
            for h, c in a["host_dist"]:
                out.append(f"- `{h}` — {c} day(s)")
            out.append("")
        if a["stable_producers"]:
            out.append(
                f"**Stable producers** (host yielded a NEW URL on >= {stable_floor} days "
                "— queries pointing at these hosts are working):"
            )
            out.append("")
            for h, c in a["stable_producers"]:
                out.append(f"- `{h}` — new on {c} day(s)")
            out.append("")
        if a["stuck_producers"]:
            out.append(
                f"**Stuck URLs** (URL shipped on >= {stable_floor} days — "
                "rotate the query to surface a different one):"
            )
            out.append("")
            for u, c in a["stuck_producers"]:
                out.append(f"- `{u}` — shipped {c} day(s)")
            out.append("")
        if a["new_vs_prior"]:
            out.append("**New-vs-prior ratio per day** (1.0 = all URLs unseen, 0.0 = all repeats):")
            out.append("")
            out.append("| Date | New | Total | Ratio |")
            out.append("|---|---:|---:|---:|")
            for row in a["new_vs_prior"]:
                out.append(f"| {row['date']} | {row['new_count']} | {row['total_count']} | {row['ratio']} |")
            out.append("")
        # Per-sector query block — pulled from telemetry tool_call events.
        # `sector_query_stuck_corr` (optional in the kw-arg below) carries
        # the precise (provider, query, stuck-hits) tuples computed from
        # urls_returned telemetry, when available.
        corr = (
            sector_query_stuck_corr.get(sector)
            if sector_query_stuck_corr
            else None
        )
        if sector_queries and sector in sector_queries:
            out.append(
                _render_query_block(
                    queries=sector_queries[sector],
                    stuck_count=len(a.get("stuck_producers", [])),
                    days=days,
                    stuck_query_correlation=corr,
                )
            )
    return "\n".join(out).rstrip() + "\n"


def _splice_observed(skill_text: str, body: str) -> str:
    section = (
        f"{OBSERVED_HEADING}\n\n"
        f"{BEGIN_MARKER}\n"
        f"{body.rstrip()}\n"
        f"{END_MARKER}\n"
    )
    if BEGIN_MARKER in skill_text and END_MARKER in skill_text:
        pattern = re.compile(
            re.escape(OBSERVED_HEADING) + r".*?" + re.escape(END_MARKER) + r"\n?",
            re.DOTALL,
        )
        return pattern.sub(section, skill_text, count=1)
    # Insert just BEFORE the auto-graduated skip-list (so observed data
    # comes first, then the actionable skip-list), or before Empty-feed,
    # or append.
    for anchor in (
        "## Skip-list — auto-graduated",
        "## Empty-feed protocol",
    ):
        if anchor in skill_text:
            return skill_text.replace(anchor, section + "\n" + anchor, 1)
    return skill_text.rstrip() + "\n\n" + section


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-graduate skill body observed-data block.")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--stable-floor", type=int, default=4,
                    help="Days a host must produce NEW (or stuck) URLs to count as stable. Default 4.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not REGISTRY_DIR.is_dir() or not SESSIONS_DIR.is_dir():
        logging.error("registry or sessions dir missing")
        return 2

    sessions = _walk_sessions(args.days)
    logging.info("loaded %d sessions in %d-day window", len(sessions), args.days)

    # Pull tool_call events ONCE for the whole window — same set is filtered
    # per skill below. Empty list when telemetry is off or absent — caller
    # already handles that gracefully.
    tool_events = _walk_tool_call_events(SESSIONS_DIR, args.days)
    logging.info("loaded %d tool_call events in window", len(tool_events))

    reports: list[dict] = []
    for path in sorted(REGISTRY_DIR.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        meta = _parse_skill_metadata(text)
        sectors = meta.get("sectors") or []
        if not isinstance(sectors, list):
            sectors = [sectors]
        hosts = meta.get("hosts") or []
        if not isinstance(hosts, list):
            hosts = [hosts]
        if not sectors:
            reports.append({"name": meta.get("name", path.stem),
                            "skipped": "no sectors"})
            continue

        analyses = {
            sector: _analyze_sector(
                sessions, sector, stable_floor=args.stable_floor,
            )
            for sector in sectors
        }
        # Filter tool_events to those that match the skill's sectors. The
        # tool_call event currently does not carry a `sector` field; until
        # it does, ALL tool_events go to ALL skills (over-inclusive but
        # accurate for the rewrite-suggester's purposes).
        sector_queries = {
            sector: _queries_for_sector_hosts(tool_events, hosts)
            for sector in sectors
        }
        # Precise per-query stuck-URL correlation. Requires urls_returned
        # in tool_call events (serper/exa/tavily/jina, 2026-05-09 patch).
        sector_query_stuck_corr = {
            sector: _queries_returning_stuck_urls(
                tool_events,
                stuck_urls=[u for u, _c in analyses[sector].get("stuck_producers", [])],
            )
            for sector in sectors
        }
        body = _render_observed_section(
            sectors=sectors, analyses=analyses,
            days=args.days, stable_floor=args.stable_floor,
            sector_queries=sector_queries,
            sector_query_stuck_corr=sector_query_stuck_corr,
        )
        new_text = _splice_observed(text, body)
        changed = new_text != text
        if changed and not args.dry_run:
            path.write_text(new_text, encoding="utf-8")
        reports.append({
            "name": meta.get("name", path.stem),
            "path": str(path.relative_to(REPO_ROOT)),
            "sectors": sectors,
            "changed": changed,
            "stable_total": sum(
                len(a.get("stable_producers", [])) for a in analyses.values()
            ),
            "stuck_total": sum(
                len(a.get("stuck_producers", [])) for a in analyses.values()
            ),
        })
        logging.info(
            "%s %s — stable:%d stuck:%d",
            "WOULD UPDATE" if (changed and args.dry_run) else (
                "UPDATED" if changed else "unchanged"
            ),
            reports[-1]["name"],
            reports[-1]["stable_total"],
            reports[-1]["stuck_total"],
        )

    print(json.dumps({
        "ok": True,
        "days": args.days,
        "stable_floor": args.stable_floor,
        "dry_run": args.dry_run,
        "sessions_loaded": len(sessions),
        "skills_processed": len(reports),
        "skills_changed": sum(1 for r in reports if r.get("changed")),
        "reports": reports,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
