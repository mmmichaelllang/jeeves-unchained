#!/usr/bin/env python3
"""Weekly quality-warning telemetry report.

Walks sessions/run-manifest-*.json for the last N days and aggregates the
``quality_warnings`` field across runs. Surfaces chronic patterns that
indicate a prompt or pipeline issue rather than a one-off model wobble:

  - PART7 fallback frequency  — if part7_uap_fallback_injected fires daily,
    the prompt itself needs surgery, not just rescuing.
  - NIM refine failures       — sustained refine misses suggest NIM tier
    pressure or model drift.
  - Banner / signoff repairs  — drift indicators.

Output:
  - Markdown report to reports/quality-telemetry-<utc-date>.md
  - Optional email via GMAIL_APP_PASSWORD when --email is passed.

Exit codes:
  0 — report written (and emailed if requested)
  1 — no manifests found in window
  2 — script error

Usage:
  python scripts/quality_telemetry_report.py --days 7
  python scripts/quality_telemetry_report.py --days 14 --email lang.mc@gmail.com
  python scripts/quality_telemetry_report.py --days 7 --no-write    # stdout only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Warnings whose frequency, by itself, is a signal worth flagging in the
# report. The thresholds are deliberately conservative: a single hit in 7
# days is normal; >2 is a pattern; >5 means the underlying issue is daily.
# Tune these as the pipeline matures.
CHRONIC_THRESHOLDS: dict[str, int] = {
    "part7_uap_fallback_injected": 3,
    "part7_literary_fallback_injected": 3,
    "part7_route_b_literary_suppressed": 3,
    "part7_route_b_uap_dropped": 2,
    "part7_route_a_literary_dropped": 2,
    "part9_tott_scaffolding_injected": 3,
    "banner stripped": 1,
    "nim_refine_failed": 5,  # prefix-matched
}


def _log() -> logging.Logger:
    return logging.getLogger("jeeves.quality_telemetry")


def _utc_today() -> date:
    return datetime.now(tz=timezone.utc).date()


def load_manifests(sessions_dir: Path, days: int) -> list[dict]:
    """Return list of manifest dicts for the last `days` UTC days, newest-first.

    Skips files that don't parse as JSON. Logs the count for forensic visibility.
    """
    log = _log()
    out: list[dict] = []
    today = _utc_today()
    for delta in range(days):
        d = today - timedelta(days=delta)
        path = sessions_dir / f"run-manifest-{d.isoformat()}.json"
        if not path.exists():
            log.debug("no manifest for %s", d)
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Stamp the parsed date for cross-reference even if the
                # manifest's own `date` field has drifted.
                data.setdefault("_path_date", d.isoformat())
                out.append(data)
        except json.JSONDecodeError as exc:
            log.warning("manifest %s did not parse: %s", path, exc)
    return out


def aggregate_warnings(manifests: list[dict]) -> Counter:
    """Count quality_warning occurrences across all manifests.

    Warnings are normalized: the prefix before the first colon is the bucket
    name, so ``nim_refine_failed:part4:APITimeoutError;...`` and
    ``nim_refine_failed:part6:RuntimeError;...`` both contribute to the
    ``nim_refine_failed`` count.
    """
    counter: Counter = Counter()
    for m in manifests:
        warnings = m.get("quality_warnings") or []
        if not isinstance(warnings, list):
            continue
        for w in warnings:
            if not isinstance(w, str):
                continue
            bucket = w.split(":", 1)[0]
            counter[bucket] += 1
    return counter


def aggregate_scores(manifests: list[dict]) -> tuple[float, int, int]:
    """Mean / min / max quality_score across manifests, or (0, 0, 0) if empty."""
    scores = [
        int(m.get("quality_score", 0))
        for m in manifests
        if isinstance(m.get("quality_score"), (int, float))
    ]
    if not scores:
        return 0.0, 0, 0
    return sum(scores) / len(scores), min(scores), max(scores)


def detect_chronic(counter: Counter) -> list[tuple[str, int, int]]:
    """Return [(warning, count, threshold), ...] for warnings >= threshold."""
    out: list[tuple[str, int, int]] = []
    for warning, threshold in CHRONIC_THRESHOLDS.items():
        count = counter.get(warning, 0)
        if count >= threshold:
            out.append((warning, count, threshold))
    out.sort(key=lambda t: -t[1])
    return out


def build_markdown_report(
    *,
    manifests: list[dict],
    counter: Counter,
    score_mean: float,
    score_min: int,
    score_max: int,
    chronic: list[tuple[str, int, int]],
    days: int,
) -> str:
    """Render the telemetry report as Markdown."""
    today = _utc_today().isoformat()
    lines: list[str] = []
    lines.append(f"# Jeeves — Quality Telemetry Report ({today})")
    lines.append("")
    lines.append(f"Window: last **{days}** days  •  Manifests: **{len(manifests)}**")
    lines.append("")

    # Score summary
    lines.append("## Quality scores")
    lines.append("")
    lines.append(f"- Mean score: **{score_mean:.1f}** / 100")
    lines.append(f"- Range: {score_min} – {score_max}")
    lines.append("")

    # Chronic warnings (the actionable section)
    lines.append("## Chronic warnings")
    lines.append("")
    if chronic:
        lines.append("Warnings firing at or above their telemetry threshold:")
        lines.append("")
        lines.append("| Warning | Count | Threshold |")
        lines.append("|---|---:|---:|")
        for w, c, t in chronic:
            lines.append(f"| `{w}` | {c} | {t} |")
        lines.append("")
        lines.append(
            "Chronic firings indicate the prompt or pipeline (not just the "
            "model) needs surgery — investigate the highest-count entry."
        )
    else:
        lines.append("None — all monitored warnings below their threshold.")
    lines.append("")

    # Full warning frequency
    lines.append("## All warning buckets (full frequency)")
    lines.append("")
    if counter:
        lines.append("| Bucket | Count |")
        lines.append("|---|---:|")
        for bucket, count in counter.most_common():
            lines.append(f"| `{bucket}` | {count} |")
    else:
        lines.append("No warnings recorded in window.")
    lines.append("")

    # Per-day quick reference
    lines.append("## Per-day manifests")
    lines.append("")
    if manifests:
        lines.append("| Date | Score | Words | Warnings |")
        lines.append("|---|---:|---:|---:|")
        for m in manifests:
            d = m.get("date") or m.get("_path_date") or "?"
            s = m.get("quality_score", "?")
            w = m.get("briefing_word_count", "?")
            warns = m.get("quality_warnings") or []
            n = len(warns) if isinstance(warns, list) else "?"
            lines.append(f"| {d} | {s} | {w} | {n} |")
    else:
        lines.append("No manifests in window.")
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(report_md: str, *, today: date | None = None) -> Path:
    """Persist the report to reports/quality-telemetry-<UTC date>.md."""
    target = today or _utc_today()
    out_dir = REPO_ROOT / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"quality-telemetry-{target.isoformat()}.md"
    out_path.write_text(report_md, encoding="utf-8")
    return out_path


def maybe_send_email(report_md: str, *, recipient: str) -> bool:
    """Best-effort email send via jeeves.alert. Returns True on success."""
    try:
        from jeeves.alert import send_failure_alert  # reuse the alert plumbing
    except ImportError:
        _log().warning("jeeves.alert unavailable; skipping email")
        return False
    # Reuse alert.py's HTML rendering by funnelling the markdown into the
    # `details` block. Subject signals telemetry-not-incident so it sorts
    # cleanly in the recipient's inbox.
    return send_failure_alert(
        subject="Weekly quality telemetry",
        reason="Weekly quality-warning telemetry summary attached.",
        details=report_md,
        remediation="(no action required unless 'Chronic warnings' is non-empty)",
        recipient=recipient,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly quality telemetry report.")
    ap.add_argument("--days", type=int, default=7,
                    help="Window size in days (default 7).")
    ap.add_argument("--email", default="",
                    help="Recipient email. Empty = skip send.")
    ap.add_argument("--no-write", action="store_true",
                    help="Print report to stdout, do not persist.")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = _log()

    sessions_dir = REPO_ROOT / "sessions"
    if not sessions_dir.is_dir():
        log.error("sessions/ directory missing at %s", sessions_dir)
        return 2

    manifests = load_manifests(sessions_dir, args.days)
    if not manifests:
        log.warning("no run-manifest-*.json found in last %d days", args.days)
        return 1

    counter = aggregate_warnings(manifests)
    score_mean, score_min, score_max = aggregate_scores(manifests)
    chronic = detect_chronic(counter)
    report = build_markdown_report(
        manifests=manifests,
        counter=counter,
        score_mean=score_mean,
        score_min=score_min,
        score_max=score_max,
        chronic=chronic,
        days=args.days,
    )

    if args.no_write:
        print(report)
    else:
        path = write_report(report)
        log.info("report written to %s", path)
        print(report)

    if args.email:
        sent = maybe_send_email(report, recipient=args.email)
        log.info("email %s", "sent" if sent else "NOT sent")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
