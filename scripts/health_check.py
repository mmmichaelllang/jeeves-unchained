#!/usr/bin/env python3
"""Jeeves richness health check — M6 acceptance criterion enforcer.

Created 2026-05-22 to fill the gap discovered while auditing the loop:
LOOP_STATE.md's DONE-WHEN referenced `scripts/health_check.py` but no
such file existed. The Tier 2 monitor (an Opus reasoning session) was
implicitly counting validation.yml's exit-0 dispatches as M6 "success"
when in fact those exit codes say nothing about whether the resulting
daily.yml run produced a rich briefing.

This script reads recent ``sessions/session-*.json`` files and reports
objective metrics tied to the M6 acceptance criteria. Exit code 0 iff
all M6 criteria are met for the window.

Usage:
    python scripts/health_check.py                  # default window=5 days
    python scripts/health_check.py --window 7       # last 7 days
    python scripts/health_check.py --source validation  # filter to sprint window
    python scripts/health_check.py --json           # machine-readable output

M6 criteria (from ROADMAP.md and LOOP_STATE.md):
    1. ≥9 of <window> sessions produce non-empty briefings.
       A session is "non-empty" if ≥3 agent sectors carry substantive
       content (>=200 chars across `findings`-like fields).
    2. Zero KILL_SWITCH deployments across the window.
       Detected by scanning `JEEVES_REFACTOR_KILL_SWITCH` references in
       recent git commits (best-effort — pre-existing flag references are
       benign; only NEW deployment commits trigger).
    3. Average ≥10 of 13 populated sectors per non-empty session.

Exit codes:
    0 — all M6 criteria met
    1 — at least one criterion failed
    2 — script error (e.g., sessions dir missing)
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

# Allow running from repo root or scripts/
ROOT = Path(__file__).resolve().parent.parent
SESSIONS_DIR = ROOT / "sessions"

# 13 agent-using sectors (newyorker excluded — direct-fetch fast path,
# always populates and would skew avg_sectors upward if counted).
AGENT_SECTORS: tuple[str, ...] = (
    "weather",
    "local_news",
    "career",
    "english_lesson_plans",
    "family",
    "global_news",
    "intellectual_journals",
    "wearable_ai",
    "triadic_ontology",
    "ai_systems",
    "uap",
    "literary_pick",
    "enriched_articles",
)
N_AGENT_SECTORS = len(AGENT_SECTORS)

# M6 thresholds — match LOOP_STATE.md's DONE-WHEN.
DEFAULT_WINDOW_DAYS = 5
M6_MIN_NON_EMPTY = 4        # ≥4 of <window> (scaled from 9/12 → 4/5)
M6_MIN_AVG_SECTORS = 10.0   # ≥10 of 13
M6_MIN_SECTOR_CHARS = 200   # chars of substantive content to count as populated
M6_MIN_RICH_SECTORS_FOR_NON_EMPTY = 3  # session is non-empty iff ≥3 rich sectors


def _sector_chars(value) -> int:
    """Count substantive prose chars across any sector shape.

    Mirrors scripts/research.py:_sector_total_chars (kept in sync — change
    here if the canonical helper changes). Counts findings/summary/text/dek/
    insight/choir/toddler/notes; ignores urls/category/source structural keys.
    """
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.strip())
    if isinstance(value, list):
        total = 0
        for item in value:
            if isinstance(item, dict):
                for k in ("findings", "summary", "text", "dek", "insight"):
                    v = item.get(k)
                    if isinstance(v, str):
                        total += len(v.strip())
            elif isinstance(item, str):
                total += len(item.strip())
        return total
    if isinstance(value, dict):
        total = 0
        for k in (
            "findings", "summary", "text", "dek", "insight",
            "choir", "toddler", "notes",
        ):
            v = value.get(k)
            if isinstance(v, str):
                total += len(v.strip())
        for k in ("openings", "classroom_ready", "pedagogy_pieces"):
            v = value.get(k)
            if isinstance(v, list):
                total += _sector_chars(v)
        return total
    return 0


def evaluate_session(path: Path) -> dict:
    """Return per-session stats: chars per sector, populated count, is_non_empty."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"path": str(path), "error": f"parse_failed: {exc}"}

    per_sector_chars = {
        name: _sector_chars(data.get(name)) for name in AGENT_SECTORS
    }
    populated = sum(1 for c in per_sector_chars.values() if c >= M6_MIN_SECTOR_CHARS)
    is_non_empty = populated >= M6_MIN_RICH_SECTORS_FOR_NON_EMPTY
    return {
        "path": str(path),
        "date": data.get("date", path.stem.replace("session-", "")),
        "populated_sectors": populated,
        "total_chars": sum(per_sector_chars.values()),
        "is_non_empty": is_non_empty,
        "per_sector_chars": per_sector_chars,
    }


def collect_sessions(window_days: int) -> list[Path]:
    """Find session-*.json files within the rolling window, newest-first."""
    if not SESSIONS_DIR.exists():
        return []
    today = date.today()
    # window_days=N means today plus the (N-1) prior days = N distinct days.
    # Using days=window_days would include N+1 entries (off-by-one).
    cutoff = today - timedelta(days=max(0, window_days - 1))
    out = []
    for path in sorted(SESSIONS_DIR.glob("session-*.json"), reverse=True):
        try:
            # Strip "session-" prefix then take only the YYYY-MM-DD portion so
            # tagged manual runs (e.g. session-2026-05-25-manual1.json) are
            # counted under the correct calendar date.
            d_str = "-".join(path.stem.replace("session-", "").split("-")[:3])
            d = date.fromisoformat(d_str)
        except ValueError:
            continue
        if d >= cutoff:
            out.append(path)
    return out


def check_kill_switch(window_days: int) -> dict:
    """Best-effort check: did any commit in the window deploy KILL_SWITCH=1?

    Scans `git log --grep` for KILL_SWITCH commit messages in the window.
    Pre-existing flag references in code are NOT counted — only commits
    whose subject mentions deploying it.
    """
    try:
        since = (date.today() - timedelta(days=window_days)).isoformat()
        result = subprocess.run(
            ["git", "log", "--since", since, "--grep=KILL_SWITCH",
             "--grep=kill switch", "--pretty=oneline"],
            cwd=ROOT, capture_output=True, text=True, timeout=10,
        )
        hits = [line for line in result.stdout.strip().splitlines() if line]
        # Filter to deploy-like phrasing — references in test names or
        # documentation commits don't count.
        deploy_hits = [
            l for l in hits
            if any(k in l.lower() for k in ("deploy", "trip", "fire", "activate", "set kill"))
        ]
        return {
            "total_kill_switch_mentions": len(hits),
            "deploy_hits": deploy_hits,
            "deploy_count": len(deploy_hits),
        }
    except Exception as exc:
        return {"error": f"git_log_failed: {exc}", "deploy_count": 0}


def run_check(window_days: int, min_non_empty: int | None = None) -> dict:
    """Full M6 acceptance check. Returns structured result.

    ``min_non_empty`` overrides the default ``M6_MIN_NON_EMPTY`` threshold
    when supplied. Required for M9 (90-day) gate where the constant 4/5
    threshold no longer corresponds to the ROADMAP criterion (85/90).
    """
    sessions = collect_sessions(window_days)
    if not sessions:
        return {
            "window_days": window_days,
            "sessions_found": 0,
            "error": "no_sessions_in_window",
            "m6_pass": False,
        }

    per_session = [evaluate_session(p) for p in sessions]
    non_empty = [s for s in per_session if s.get("is_non_empty")]
    kill_switch_info = check_kill_switch(window_days)

    avg_populated = (
        sum(s["populated_sectors"] for s in non_empty) / len(non_empty)
        if non_empty else 0.0
    )

    # M6 thresholds. min_non_empty override lets the same script gate
    # both the legacy M6 12-day window (default 4) and the M9 90-day
    # window (caller passes 85) without forking the script.
    threshold = (
        int(min_non_empty) if min_non_empty is not None else M6_MIN_NON_EMPTY
    )
    crit_1 = len(non_empty) >= threshold
    crit_2 = kill_switch_info.get("deploy_count", 0) == 0
    crit_3 = avg_populated >= M6_MIN_AVG_SECTORS

    return {
        "window_days": window_days,
        "sessions_found": len(per_session),
        "non_empty_count": len(non_empty),
        "non_empty_threshold": threshold,
        "avg_populated_sectors": round(avg_populated, 2),
        "avg_sectors_threshold": M6_MIN_AVG_SECTORS,
        "max_sectors": N_AGENT_SECTORS,
        "kill_switch": kill_switch_info,
        "m6_criterion_1_non_empty_count": crit_1,
        "m6_criterion_2_zero_kill_switch": crit_2,
        "m6_criterion_3_avg_sectors": crit_3,
        "m6_pass": crit_1 and crit_2 and crit_3,
        "per_session": per_session,
    }


def render_text(result: dict) -> str:
    """Human-readable single-line summary the LOOP_STATE.md VERIFY grep matches."""
    if "error" in result and "non_empty_count" not in result:
        return f"non_empty=0/0 KILL_SWITCH=? avg_sectors=0.0 m6_pass=False error={result['error']}"
    lines = [
        f"non_empty={result['non_empty_count']}/{result['sessions_found']} "
        f"(threshold ≥{result['non_empty_threshold']})",
        f"KILL_SWITCH={result['kill_switch'].get('deploy_count', '?')} "
        f"(threshold 0)",
        f"avg_sectors={result['avg_populated_sectors']}/{result['max_sectors']} "
        f"(threshold ≥{result['avg_sectors_threshold']})",
        f"m6_pass={result['m6_pass']}",
    ]
    out = " ".join(lines)
    if result.get("per_session"):
        out += "\n\nper-session:"
        for s in result["per_session"]:
            if "error" in s:
                out += f"\n  {s.get('path', '?')}: ERROR {s['error']}"
            else:
                marker = "OK" if s["is_non_empty"] else "THIN"
                out += (
                    f"\n  {s['date']} [{marker}] "
                    f"populated={s['populated_sectors']}/{N_AGENT_SECTORS} "
                    f"chars={s['total_chars']}"
                )
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Jeeves M6 richness health check.")
    p.add_argument("--window", type=int, default=DEFAULT_WINDOW_DAYS,
                   help=f"Day window (default {DEFAULT_WINDOW_DAYS}).")
    p.add_argument("--source", default="",
                   help="Filter label (e.g. 'validation'). Currently advisory.")
    p.add_argument("--json", action="store_true",
                   help="Emit machine-readable JSON.")
    p.add_argument("--min-non-empty", type=int, default=None,
                   help=(
                       f"Override the non-empty threshold (default "
                       f"{M6_MIN_NON_EMPTY}). Required for the M9 90-day "
                       "gate — pass 85 to match ROADMAP M9's '>=85/90' "
                       "criterion."
                   ))
    args = p.parse_args(argv)

    try:
        result = run_check(args.window, min_non_empty=args.min_non_empty)
    except Exception as exc:
        print(f"health_check error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_text(result))

    return 0 if result.get("m6_pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
