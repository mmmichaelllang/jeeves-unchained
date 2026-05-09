#!/usr/bin/env python3
"""Tokens-per-call rollup report.

Walks ``sessions/telemetry-*.jsonl`` for the last N days and aggregates:

  - LLM-call counts per provider × model × label
  - Token totals (prompt / completion / total) per provider when available
  - Latency mean / p95 per provider
  - Tool-call counts (the existing ``tool_call`` event type) per provider
  - Anomalies: errors per provider, token-cap-approaching warnings

Output:
  - Markdown report → ``reports/tokens-per-call-<utc-date>.md`` (committed
    via the weekly audit_health.yml workflow → durable history in the repo)
  - Optional email via the existing alert plumbing when ``--email`` is set

Schema this reads
-----------------
The ``llm_call`` event is emitted by ``jeeves.tools.telemetry.emit_llm_call``.
Until every LLM call site is instrumented, token totals will be partial —
the rollup gracefully degrades to call-count + latency for sites that
don't yet emit usage data.

Usage
-----
    python scripts/tokens_per_call_report.py --days 7
    python scripts/tokens_per_call_report.py --days 14 --email lang.mc@gmail.com
    python scripts/tokens_per_call_report.py --no-write          # stdout only
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _utc_today() -> date:
    return datetime.now(tz=timezone.utc).date()


def _walk_telemetry(sessions_dir: Path, days: int) -> list[dict]:
    """Return a flat list of telemetry events from the last `days` files."""
    out: list[dict] = []
    today = _utc_today()
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
                        event = json.loads(raw)
                        if isinstance(event, dict):
                            event["_source_date"] = d.isoformat()
                            out.append(event)
                    except json.JSONDecodeError:
                        continue
        except OSError as exc:
            logging.warning("telemetry %s read failed: %s", path, exc)
    return out


def _percentile(values: list[float], pct: float) -> float:
    """Simple percentile — no numpy dep. pct in 0..100."""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (pct / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[lo]
    weight = rank - lo
    return s[lo] * (1 - weight) + s[hi] * weight


def _aggregate(events: list[dict]) -> dict:
    """Group events by provider × model and roll up call/token/latency."""
    llm_groups: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "calls": 0,
        "errors": 0,
        "prompt_tokens": [],
        "completion_tokens": [],
        "total_tokens": [],
        "latency_ms": [],
        "labels": defaultdict(int),
        "sectors": defaultdict(int),
    })
    tool_groups: dict[str, dict] = defaultdict(lambda: {
        "calls": 0,
        "errors": 0,
        "latency_ms": [],
    })

    for ev in events:
        kind = ev.get("event") or ""
        if kind == "llm_call":
            key = (str(ev.get("provider", "?")), str(ev.get("model", "")))
            g = llm_groups[key]
            g["calls"] += 1
            if not ev.get("ok", True):
                g["errors"] += 1
            for tk in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if isinstance(ev.get(tk), (int, float)):
                    g[tk].append(int(ev[tk]))
            if isinstance(ev.get("latency_ms"), (int, float)):
                g["latency_ms"].append(float(ev["latency_ms"]))
            label = ev.get("label") or ""
            if label:
                g["labels"][label] += 1
            sector = ev.get("sector") or ""
            if sector:
                g["sectors"][sector] += 1
        elif kind == "tool_call":
            provider = str(ev.get("provider", "?"))
            t = tool_groups[provider]
            t["calls"] += 1
            if not ev.get("ok", True):
                t["errors"] += 1
            if isinstance(ev.get("latency_ms"), (int, float)):
                t["latency_ms"].append(float(ev["latency_ms"]))

    return {"llm": dict(llm_groups), "tool": dict(tool_groups)}


def _render_markdown(*, agg: dict, events: list[dict], days: int) -> str:
    today = _utc_today().isoformat()
    lines: list[str] = []
    lines.append(f"# Jeeves — Tokens-per-Call Telemetry ({today})")
    lines.append("")
    lines.append(
        f"Window: last **{days}** days  •  Total events: **{len(events)}**  "
        f"•  LLM calls: **{sum(g['calls'] for g in agg['llm'].values())}**  "
        f"•  Tool calls: **{sum(g['calls'] for g in agg['tool'].values())}**"
    )
    lines.append("")

    # ---- LLM calls
    lines.append("## LLM calls")
    lines.append("")
    if not agg["llm"]:
        lines.append(
            "_No `llm_call` events in window. Each LLM call site that should "
            "appear here MUST emit via_ `jeeves.tools.telemetry.emit_llm_call` "
            "_with `JEEVES_TELEMETRY=1`. See follow-up note below._"
        )
        lines.append("")
    else:
        lines.append("| Provider | Model | Calls | Err | Σprompt | Σcompl | Σtotal | mean lat ms | p95 lat ms |")
        lines.append("|---|---|---:|---:|---:|---:|---:|---:|---:|")
        for (provider, model), g in sorted(agg["llm"].items()):
            sum_prompt = sum(g["prompt_tokens"]) or "—"
            sum_compl = sum(g["completion_tokens"]) or "—"
            sum_total = sum(g["total_tokens"]) or "—"
            mean_lat = (
                round(sum(g["latency_ms"]) / len(g["latency_ms"]), 1)
                if g["latency_ms"] else "—"
            )
            p95 = (
                round(_percentile(g["latency_ms"], 95), 1)
                if g["latency_ms"] else "—"
            )
            lines.append(
                f"| {provider} | {model or '—'} | {g['calls']} | "
                f"{g['errors']} | {sum_prompt} | {sum_compl} | "
                f"{sum_total} | {mean_lat} | {p95} |"
            )
        lines.append("")

        # Per-sector breakdown for the largest provider
        biggest = max(agg["llm"].items(), key=lambda t: t[1]["calls"])
        provider, model = biggest[0]
        if biggest[1]["sectors"]:
            lines.append(f"### `{provider}` × `{model or '—'}` — calls by sector")
            lines.append("")
            for sector, count in sorted(
                biggest[1]["sectors"].items(), key=lambda t: -t[1],
            ):
                lines.append(f"- `{sector}` — {count}")
            lines.append("")

    # ---- Tool calls
    lines.append("## Tool calls (search/extract providers)")
    lines.append("")
    if not agg["tool"]:
        lines.append("_No `tool_call` events in window._")
        lines.append("")
    else:
        lines.append("| Provider | Calls | Err | mean lat ms | p95 lat ms |")
        lines.append("|---|---:|---:|---:|---:|")
        for provider, g in sorted(agg["tool"].items()):
            mean_lat = (
                round(sum(g["latency_ms"]) / len(g["latency_ms"]), 1)
                if g["latency_ms"] else "—"
            )
            p95 = (
                round(_percentile(g["latency_ms"], 95), 1)
                if g["latency_ms"] else "—"
            )
            lines.append(
                f"| {provider} | {g['calls']} | {g['errors']} | "
                f"{mean_lat} | {p95} |"
            )
        lines.append("")

    # ---- Coverage notes
    lines.append("## Instrumentation coverage")
    lines.append("")
    lines.append(
        "LLM call sites are instrumented progressively. Sites NOT yet "
        "emitting `llm_call` events as of this report do not appear in the "
        "tables above — their token counts are still invisible to telemetry. "
        "See `jeeves/tools/telemetry.py:emit_llm_call` for the helper; the "
        "instrumentation backlog lives in the repo's project memory."
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(report_md: str, *, today: date | None = None) -> Path:
    target = today or _utc_today()
    out_dir = REPO_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"tokens-per-call-{target.isoformat()}.md"
    out_path.write_text(report_md, encoding="utf-8")
    return out_path


def maybe_send_email(report_md: str, *, recipient: str) -> bool:
    try:
        from jeeves.alert import send_failure_alert
    except ImportError:
        logging.warning("jeeves.alert unavailable; skipping email")
        return False
    return send_failure_alert(
        subject="Weekly tokens-per-call telemetry",
        reason="Weekly LLM/tool-call rollup attached.",
        details=report_md,
        remediation="(no action required unless errors/latency cells trend up)",
        recipient=recipient,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tokens-per-call rollup.")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--email", default="")
    ap.add_argument("--no-write", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    sessions_dir = REPO_ROOT / "sessions"
    if not sessions_dir.is_dir():
        logging.error("sessions/ missing at %s", sessions_dir)
        return 2

    events = _walk_telemetry(sessions_dir, args.days)
    logging.info("loaded %d events from telemetry-*.jsonl", len(events))

    agg = _aggregate(events)
    report = _render_markdown(agg=agg, events=events, days=args.days)

    if args.no_write:
        print(report)
    else:
        path = write_report(report)
        logging.info("report written to %s", path)
        print(report)

    if args.email:
        sent = maybe_send_email(report, recipient=args.email)
        logging.info("email %s", "sent" if sent else "NOT sent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
