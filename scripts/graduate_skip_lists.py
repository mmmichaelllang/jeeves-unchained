#!/usr/bin/env python3
"""Skill skip-list graduator.

Walks `sessions/session-*.json` for the last N days, identifies items that
have shipped on >= MIN_CONSECUTIVE consecutive days for a sector, and
auto-updates the matching skill markdown's skip-list section. Idempotent —
running twice produces the same output.

Why this exists
---------------
The Autobrowse-pattern site skills (jeeves/site_skills/registry/*.md) carry
a hand-curated skip-list of items the research agent should NOT re-ship.
Hand maintenance drifts: as truly-new items become old news, the skip-list
goes stale. This script automates the maintenance from session-*.json
ground truth.

Skip-list section contract
--------------------------
Each skill markdown contains a section starting with the literal heading::

    ## Skip-list — auto-graduated

This script REPLACES the body of that section with a freshly-rendered list.
If the section is missing, it is inserted just before the "## Empty-feed
protocol" heading (or, lacking that, at the end of the file).

Cadence
-------
Run weekly via .github/workflows/audit_health.yml. Idempotent + commits
its own changes via the workflow.

Usage
-----
    python scripts/graduate_skip_lists.py
    python scripts/graduate_skip_lists.py --days 14
    python scripts/graduate_skip_lists.py --min-consecutive 3
    python scripts/graduate_skip_lists.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SESSIONS_DIR = REPO_ROOT / "sessions"
REGISTRY_DIR = REPO_ROOT / "jeeves" / "site_skills" / "registry"

# Auto-graduated section heading + sentinel tags. The script writes between
# BEGIN and END markers so subsequent runs replace cleanly without touching
# any other part of the skill body.
SECTION_HEADING = "## Skip-list — auto-graduated"
BEGIN_MARKER = "<!-- skip-list:begin -->"
END_MARKER = "<!-- skip-list:end -->"


# ---------------------------------------------------------------------------
# session JSON walking
# ---------------------------------------------------------------------------

def _utc_today() -> date:
    return datetime.now(tz=timezone.utc).date()


def _strip_url(u: str) -> str:
    """Canonical form for cross-day URL match.

    Drops trailing slash, strips arxiv ``vN`` versions, normalises
    ``abs/`` ↔ ``pdf/`` so the same paper at both URL shapes counts once.
    """
    u = (u or "").strip().rstrip("/")
    # arxiv canonicalisation — pdf/ID and abs/ID and abs/IDvN all → abs/ID;
    # also collapse www.arxiv.org → arxiv.org so cross-day match works.
    m = re.match(r"^https?://(?:www\.)?arxiv\.org/(?:abs|pdf)/([0-9.]+)(?:v\d+)?(?:\.pdf)?$", u, re.I)
    if m:
        return f"https://arxiv.org/abs/{m.group(1)}"
    return u


def _collect_sector_urls(session: dict, sector: str) -> list[str]:
    """Pull URLs from a single session's sector.

    Handles the two sector shapes jeeves uses:
      - ``list[Finding]`` — local_news, global_news, intellectual_journals,
        wearable_ai, enriched_articles. Each item has ``urls: list[str]``.
      - ``DeepResearch`` — triadic_ontology, ai_systems, uap. Block has
        ``urls: list[str]`` directly.
    """
    val = session.get(sector)
    if val is None:
        return []
    out: list[str] = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict):
                for u in item.get("urls", []) or []:
                    if isinstance(u, str):
                        out.append(_strip_url(u))
    elif isinstance(val, dict):
        for u in val.get("urls", []) or []:
            if isinstance(u, str):
                out.append(_strip_url(u))
    return out


def _collect_sector_headlines(session: dict, sector: str) -> list[str]:
    """Pull human-readable item titles for a sector.

    For findings-shaped sectors this is the first sentence of `findings`
    (mirrors `collect_headlines_from_sector` in research_sectors.py — kept
    independent here so the graduator can run without importing the
    pipeline's full dep stack).
    """
    val = session.get(sector)
    if val is None:
        return []
    out: list[str] = []
    if isinstance(val, list):
        for item in val:
            if isinstance(item, dict):
                title = item.get("title") or item.get("headline")
                if title:
                    out.append(str(title).strip())
                    continue
                findings = item.get("findings") or ""
                first = findings.split(".", 1)[0].strip()
                if first and len(first) > 12:
                    out.append(first[:140])
    elif isinstance(val, dict):
        findings = val.get("findings") or ""
        for chunk in re.split(r"(?<=[.!?])\s+", findings):
            chunk = chunk.strip()
            if chunk and len(chunk) > 12:
                out.append(chunk[:140])
    return out


def _walk_sessions(days: int) -> list[tuple[date, dict]]:
    """Return [(session_date, session_dict)] for the last `days` days."""
    out: list[tuple[date, dict]] = []
    today = _utc_today()
    for delta in range(days):
        d = today - timedelta(days=delta)
        path = SESSIONS_DIR / f"session-{d.isoformat()}.json"
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logging.warning("session %s did not parse: %s", path, exc)
            continue
        if isinstance(data, dict):
            out.append((d, data))
    return sorted(out, key=lambda t: t[0])


# ---------------------------------------------------------------------------
# durable-item detection
# ---------------------------------------------------------------------------

def _items_with_consecutive_streaks(
    sessions: list[tuple[date, dict]],
    sector: str,
    *,
    min_consecutive: int,
    item_extractor,
) -> dict[str, int]:
    """For each item produced by ``item_extractor(session, sector)``,
    return its longest consecutive-day streak across the session series.

    Output: ``{item: longest_streak}`` filtered to items whose longest
    streak meets or exceeds ``min_consecutive``.
    """
    if not sessions:
        return {}
    # date_index[d] = set of items shipped that day
    date_to_items: dict[date, set[str]] = {}
    all_items: set[str] = set()
    for d, sess in sessions:
        items = set(item_extractor(sess, sector))
        date_to_items[d] = items
        all_items |= items

    longest: dict[str, int] = defaultdict(int)
    sorted_dates = sorted(date_to_items.keys())
    for item in all_items:
        streak = 0
        cur = 0
        prev_d: date | None = None
        for d in sorted_dates:
            if item in date_to_items[d]:
                if prev_d is None or (d - prev_d).days == 1:
                    cur += 1
                else:
                    cur = 1
                streak = max(streak, cur)
                prev_d = d
            else:
                cur = 0
                prev_d = None
        if streak >= min_consecutive:
            longest[item] = streak
    return dict(longest)


# ---------------------------------------------------------------------------
# skill markdown rewriter
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n.*?\n---\s*\n", re.DOTALL)


def _parse_skill_metadata(text: str) -> dict:
    """Parse name, sectors, hosts from the skill's frontmatter."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm_lines = m.group(0).split("\n")
    out: dict = {}
    for line in fm_lines:
        line = line.strip()
        if not line or line == "---":
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if value.startswith("[") and value.endswith("]"):
            out[key] = [
                s.strip().strip('"').strip("'")
                for s in value[1:-1].split(",") if s.strip()
            ]
        else:
            out[key] = value
    return out


def _render_skip_list_section(
    *,
    urls: dict[str, int],
    headlines: dict[str, int],
    days: int,
    min_consecutive: int,
) -> str:
    """Render the auto-graduated skip-list as a markdown block ready to
    drop between BEGIN and END markers."""
    today = _utc_today().isoformat()
    parts = [
        f"_Last graduated_: {today}  •  _Window_: {days}-day  •  "
        f"_Min consecutive ship streak_: {min_consecutive}",
        "",
    ]
    if not urls and not headlines:
        parts.append("_No items have crossed the streak threshold in this window._")
        return "\n".join(parts) + "\n"

    if urls:
        parts.append("**URLs already over-shipped (skip on next run):**")
        parts.append("")
        for u, streak in sorted(urls.items(), key=lambda t: (-t[1], t[0])):
            parts.append(f"- `{u}`  _(shipped {streak} consecutive days)_")
        parts.append("")
    if headlines:
        parts.append("**Headlines already over-shipped (find a different angle):**")
        parts.append("")
        for h, streak in sorted(headlines.items(), key=lambda t: (-t[1], t[0])):
            short = h if len(h) <= 120 else h[:117] + "…"
            parts.append(f"- {short}  _(shipped {streak} consecutive days)_")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def _splice_skip_list(skill_text: str, body: str) -> str:
    """Insert/replace the skip-list section in the skill text.

    Returns the new skill text. Idempotent: running twice produces the same
    output. The section is bracketed by BEGIN/END markers so subsequent
    runs find their own previous output and replace it cleanly.
    """
    section = (
        f"{SECTION_HEADING}\n\n"
        f"{BEGIN_MARKER}\n"
        f"{body.rstrip()}\n"
        f"{END_MARKER}\n"
    )
    if BEGIN_MARKER in skill_text and END_MARKER in skill_text:
        # Replace existing block (and its heading) atomically.
        pattern = re.compile(
            re.escape(SECTION_HEADING)
            + r".*?"
            + re.escape(END_MARKER)
            + r"\n?",
            re.DOTALL,
        )
        return pattern.sub(section, skill_text, count=1)
    # Insert before "## Empty-feed protocol" if present, else append.
    insertion_anchor = "## Empty-feed protocol"
    if insertion_anchor in skill_text:
        return skill_text.replace(
            insertion_anchor, section + "\n" + insertion_anchor, 1,
        )
    return skill_text.rstrip() + "\n\n" + section


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def _process_skill(
    skill_path: Path,
    sessions: list[tuple[date, dict]],
    *,
    min_consecutive: int,
    days: int,
    dry_run: bool,
) -> tuple[bool, dict]:
    """Process one skill. Returns (changed, report-dict)."""
    text = skill_path.read_text(encoding="utf-8")
    meta = _parse_skill_metadata(text)
    name = meta.get("name", skill_path.stem)
    sectors = meta.get("sectors") or []
    if not isinstance(sectors, list):
        sectors = [sectors]

    if not sectors:
        return False, {"name": name, "skipped": "no sectors in frontmatter"}

    aggregated_urls: dict[str, int] = {}
    aggregated_headlines: dict[str, int] = {}
    for sector in sectors:
        urls = _items_with_consecutive_streaks(
            sessions, sector,
            min_consecutive=min_consecutive,
            item_extractor=_collect_sector_urls,
        )
        for k, v in urls.items():
            aggregated_urls[k] = max(aggregated_urls.get(k, 0), v)
        headlines = _items_with_consecutive_streaks(
            sessions, sector,
            min_consecutive=min_consecutive,
            item_extractor=_collect_sector_headlines,
        )
        for k, v in headlines.items():
            aggregated_headlines[k] = max(aggregated_headlines.get(k, 0), v)

    body = _render_skip_list_section(
        urls=aggregated_urls,
        headlines=aggregated_headlines,
        days=days,
        min_consecutive=min_consecutive,
    )
    new_text = _splice_skip_list(text, body)
    if new_text == text:
        return False, {
            "name": name,
            "sectors": sectors,
            "url_hits": len(aggregated_urls),
            "headline_hits": len(aggregated_headlines),
            "changed": False,
        }
    if not dry_run:
        skill_path.write_text(new_text, encoding="utf-8")
    return True, {
        "name": name,
        "sectors": sectors,
        "url_hits": len(aggregated_urls),
        "headline_hits": len(aggregated_headlines),
        "changed": True,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-graduate site-skill skip-lists.")
    ap.add_argument("--days", type=int, default=14,
                    help="Look-back window (days). Default 14.")
    ap.add_argument("--min-consecutive", type=int, default=3,
                    help="Minimum consecutive-day streak to flag as over-shipped. Default 3.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not modify files; just print the report.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not REGISTRY_DIR.is_dir():
        logging.error("registry not found at %s", REGISTRY_DIR)
        return 2
    if not SESSIONS_DIR.is_dir():
        logging.error("sessions/ not found at %s", SESSIONS_DIR)
        return 2

    sessions = _walk_sessions(args.days)
    logging.info("loaded %d sessions in %d-day window", len(sessions), args.days)

    reports: list[dict] = []
    for path in sorted(REGISTRY_DIR.glob("*.md")):
        changed, report = _process_skill(
            path, sessions,
            min_consecutive=args.min_consecutive,
            days=args.days,
            dry_run=args.dry_run,
        )
        report["path"] = str(path.relative_to(REPO_ROOT))
        reports.append(report)
        verb = "WOULD UPDATE" if (changed and args.dry_run) else (
            "UPDATED" if changed else "unchanged"
        )
        logging.info(
            "%s %s — urls:%d headlines:%d sectors:%s",
            verb, report["name"],
            report.get("url_hits", 0),
            report.get("headline_hits", 0),
            report.get("sectors", []),
        )

    print(json.dumps({
        "ok": True,
        "days": args.days,
        "min_consecutive": args.min_consecutive,
        "dry_run": args.dry_run,
        "sessions_loaded": len(sessions),
        "skills_processed": len(reports),
        "skills_changed": sum(1 for r in reports if r.get("changed")),
        "reports": reports,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
