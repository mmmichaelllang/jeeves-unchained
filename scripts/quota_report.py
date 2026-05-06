"""Quota usage report — print rolling spend per provider.

Reads `.quota-state.json` (current month/day counters) and the trailing
N days of `sessions/shadow-tinyfish-*.jsonl` to estimate $/day. Used as
both a manual digest tool and the data source for the daily Slack/email
cost-monitoring step planned in the sprint-18 rollout.

Usage::

    uv run python scripts/quota_report.py             # default: trailing 7 days
    uv run python scripts/quota_report.py --since 30d
    uv run python scripts/quota_report.py --json      # machine-readable
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Per-call costs (USD) — kept in sync with scripts/eval_extractors.py.
_PER_CALL_USD = {
    "tinyfish": 0.012,
    "firecrawl": 0.005,
    "playwright": 3.0 / 60.0 * 0.008,  # ~3s of CI runner time per call
    "tavily": 8.00 / 1000,
    "exa": 5.00 / 1000,
    "serper": 0.30 / 1000,
    "gemini": 35.0 / 1000,
}


def _parse_since(spec: str) -> int:
    """Parse '7d' / '30d' / '24h' into days (rounded up)."""
    spec = spec.strip().lower()
    if spec.endswith("d"):
        return max(1, int(spec[:-1]))
    if spec.endswith("h"):
        hours = int(spec[:-1])
        return max(1, (hours + 23) // 24)
    return max(1, int(spec))


def _shadow_counts(repo_root: Path, days: int) -> dict[str, int]:
    """Count shadow-captured TinyFish calls in the last `days` days."""
    counts = {"tinyfish_shadow": 0}
    sessions_dir = repo_root / "sessions"
    if not sessions_dir.exists():
        return counts
    cutoff = datetime.now(timezone.utc).date() - timedelta(days=days)
    for path in sessions_dir.glob("shadow-tinyfish-*.jsonl"):
        try:
            stamp = path.stem.replace("shadow-tinyfish-", "")
            day = datetime.strptime(stamp, "%Y-%m-%d").date()
        except Exception:
            continue
        if day < cutoff:
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                counts["tinyfish_shadow"] += sum(1 for _ in fh)
        except Exception:
            continue
    return counts


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", type=str, default="7d",
                   help="Window for shadow counts (e.g. '7d', '30d', '24h').")
    p.add_argument("--state", type=Path, default=Path(".quota-state.json"))
    p.add_argument("--json", action="store_true", dest="as_json")
    args = p.parse_args(argv)

    repo_root = Path.cwd()
    days = _parse_since(args.since)

    if not args.state.exists():
        print(f"no quota file at {args.state}", file=sys.stderr)
        return 1

    state = json.loads(args.state.read_text(encoding="utf-8"))
    providers = state.get("providers") or {}
    daily = state.get("daily") or {}

    rows: list[dict] = []
    for name, p_state in providers.items():
        used = int(p_state.get("used") or 0)
        per_call = _PER_CALL_USD.get(name, 0.0)
        rows.append({
            "provider": name,
            "scope": "month",
            "used": used,
            "free_cap": int(p_state.get("free_cap") or 0),
            "spend_usd": round(used * per_call, 4),
        })
    for name, used in daily.items():
        if name == "date":
            continue
        per_call = _PER_CALL_USD.get(name, 0.0)
        rows.append({
            "provider": name,
            "scope": f"day({daily.get('date', '?')})",
            "used": int(used),
            "free_cap": 0,
            "spend_usd": round(int(used) * per_call, 4),
        })

    shadow = _shadow_counts(repo_root, days)
    rows.append({
        "provider": "tinyfish_shadow",
        "scope": f"last_{days}d",
        "used": shadow["tinyfish_shadow"],
        "free_cap": 0,
        "spend_usd": round(shadow["tinyfish_shadow"] * _PER_CALL_USD["tinyfish"], 4),
    })

    payload = {
        "month": state.get("month"),
        "today": daily.get("date"),
        "window_days": days,
        "rows": rows,
    }

    if args.as_json:
        print(json.dumps(payload, indent=2))
        return 0

    print(f"quota report — month={payload['month']}, today={payload['today']}, window={days}d")
    print()
    print(f"{'provider':<22} {'scope':<14} {'used':>7} {'cap':>7} {'spend_usd':>10}")
    print("-" * 64)
    for r in rows:
        print(f"{r['provider']:<22} {r['scope']:<14} {r['used']:>7} "
              f"{r['free_cap']:>7} {r['spend_usd']:>10.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
