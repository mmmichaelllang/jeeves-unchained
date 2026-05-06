"""Mine a search-eval golden set from past sessions/.

Sprint-19 slice E. Reads ``sessions/session-*.json``, harvests the URLs that
each sector actually surfaced (and that survived dedup downstream), and
emits a YAML fixture the ``eval_search.py`` harness consumes.

Why mine instead of hand-curate?
    Hand-curated golden sets go stale fast (today's "obvious" Edmonds news
    URL is gone in 4 weeks). Past *real* runs are the closest thing to
    ground truth for "what should serper/jina/tinyfish/playwright surface
    given this query?". Each session is a recall snapshot.

Output schema (``tests/fixtures/search_eval_set.yaml``)::

    version: 1
    mined_at: 2026-05-05
    cases:
      - id: 2026-05-04_local_news_municipal
        category: local_news
        sector: local_news
        # The query the harness should send. Synthesised from the sector
        # category + a date-anchored hint when present.
        query: "Edmonds WA municipal news 2026-05-04"
        # URLs that appeared in this sector that day. Recall@N is computed
        # by counting how many of these appear in a provider's top-N set.
        golden_urls:
          - https://edmondswa.gov/...
          - ...

Usage::

    python scripts/mine_golden_set.py \\
        --sessions-dir sessions \\
        --days 7 \\
        --out tests/fixtures/search_eval_set.yaml

Deterministic — same input directory + same ``--days`` always produces the
same YAML (sort by case id). Safe to re-run; existing fixture is overwritten.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("mine_golden_set")

# Sectors worth mining. Skipped: weather (synthesized text, no URLs),
# uap/ai_systems/triadic_ontology (too narrow / non-deterministic), newyorker
# (single source), correspondence (private mail).
_MINEABLE_SECTORS: tuple[str, ...] = (
    "local_news",
    "global_news",
    "intellectual_journals",
    "career",
    "family",
    "wearable_ai",
)

# Heuristic per-sector query template. {date} becomes the session date so
# providers see a time-anchored query. Templates are intentionally simple —
# the eval is about which provider returns relevant URLs *given this query*,
# not query-rewriting tricks.
_QUERY_TEMPLATES: dict[str, str] = {
    "local_news": "Edmonds Washington local news {date}",
    "global_news": "world news {date} top stories",
    "intellectual_journals": "essays NYRB OR LRB OR n+1 long-form {date}",
    "career": "AI engineering hiring news {date}",
    "family": "family parenting culture longform {date}",
    "wearable_ai": "wearable AI device news {date}",
}


def _extract_urls(value: Any) -> list[str]:
    """Pull urls out of any sector value shape (string/list/dict)."""
    out: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                u = item.get("url") or item.get("link")
                if u:
                    out.append(str(u))
                # also harvest urls list inside each item
                urls = item.get("urls")
                if isinstance(urls, list):
                    out.extend(str(u) for u in urls if u)
            elif isinstance(item, str) and item.startswith("http"):
                out.append(item)
    elif isinstance(value, dict):
        urls = value.get("urls")
        if isinstance(urls, list):
            out.extend(str(u) for u in urls if u)
    # de-dup, preserve order
    seen: set[str] = set()
    deduped: list[str] = []
    for u in out:
        if u not in seen and u.startswith("http"):
            seen.add(u)
            deduped.append(u)
    return deduped


def _session_date(path: Path) -> str | None:
    m = re.search(r"session-(\d{4}-\d{2}-\d{2})\.json$", path.name)
    return m.group(1) if m else None


def _within_window(session_date: str, days: int) -> bool:
    try:
        d = datetime.strptime(session_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - d) <= timedelta(days=days)


def mine_sessions(sessions_dir: Path, days: int) -> list[dict[str, Any]]:
    """Walk ``sessions_dir`` and return one case dict per (date, sector) tuple
    that has at least 2 URLs. Returns sorted list of dicts ready for YAML
    serialisation."""
    cases: list[dict[str, Any]] = []
    for path in sorted(sessions_dir.glob("session-*.json")):
        date = _session_date(path)
        if not date or not _within_window(date, days):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("skip %s: %s", path, exc)
            continue
        for sector in _MINEABLE_SECTORS:
            if sector not in data:
                continue
            urls = _extract_urls(data[sector])
            if len(urls) < 2:
                continue
            template = _QUERY_TEMPLATES.get(sector, "{date} {sector}")
            query = template.format(date=date, sector=sector)
            cases.append(
                {
                    "id": f"{date}_{sector}",
                    "category": sector,
                    "sector": sector,
                    "session_date": date,
                    "query": query,
                    "golden_urls": urls[:15],
                }
            )
    cases.sort(key=lambda c: c["id"])
    return cases


def write_yaml(cases: list[dict[str, Any]], out_path: Path) -> None:
    """Hand-rolled YAML emitter — pyyaml is optional in this repo."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines: list[str] = [
        "version: 1",
        f"mined_at: {today}",
        f"count: {len(cases)}",
        "cases:",
    ]
    for c in cases:
        lines.append(f"  - id: {c['id']}")
        lines.append(f"    category: {c['category']}")
        lines.append(f"    sector: {c['sector']}")
        lines.append(f"    session_date: {c['session_date']}")
        # Quote the query — it contains spaces and date colons.
        q = c["query"].replace('"', '\\"')
        lines.append(f'    query: "{q}"')
        lines.append("    golden_urls:")
        for u in c["golden_urls"]:
            # YAML scalar safe: URLs have no special chars our reader cares about.
            lines.append(f"      - {u}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Mine a search-eval golden set from past sessions/.")
    p.add_argument("--sessions-dir", default="sessions", type=Path)
    p.add_argument("--days", default=7, type=int, help="Window of recent sessions to mine (default 7).")
    p.add_argument(
        "--out",
        default=Path("tests/fixtures/search_eval_set.yaml"),
        type=Path,
    )
    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.sessions_dir.is_dir():
        log.error("sessions dir not found: %s", args.sessions_dir)
        return 2

    cases = mine_sessions(args.sessions_dir, args.days)
    write_yaml(cases, args.out)
    log.info("wrote %d cases to %s", len(cases), args.out)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
