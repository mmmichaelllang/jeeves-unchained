#!/usr/bin/env python3
"""Audit health report — weekly digest of validator + gate activity.

Walks ``sessions/audit-fix-<date>.json`` files for the last N days
(default 7) and produces a summary of:

- Total fix actions per status (applied / skipped / failed)
- Fix actions per type (rerender_empty_with_data, rerender_greeting,
  strip_hallucinated_url, etc.)
- Failed actions broken down by reason: validator-rejected, LLM-call-failed, other
- Auditor-regression count from git log (commits with the
  "(reverted — auditor regressed)" suffix that the F-009 gate writes)
- Days with no audit-fix run (operator visibility into pipeline gaps)

Output:
- stdout (plain text + simple HTML stitched together)
- optional ``--email`` flag sends via ``jeeves.email.send_html`` using
  the same SMTP config as the daily briefing

Recommended cadence: weekly via ``.github/workflows/audit_health.yml``.

Why: F-001 + F-007 + F-009 together close the May-6 failure modes, but
operator can't tell from the daily commit log whether the validator is
firing too often (sign that reasoning models are drifting), or whether
the gate is reverting silently (sign that audit_fix is making things
worse). This report surfaces both signals in one place.

Usage:
    python scripts/audit_health_report.py
    python scripts/audit_health_report.py --days 14
    python scripts/audit_health_report.py --sessions-dir sessions/
    python scripts/audit_health_report.py --email lang.mc@gmail.com
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("audit_health")

# Match audit-fix file names. Excludes audit-fix-<date>.post-fix.json variants
# (those are the pre/post-fix audit JSONs from F-009; audit-fix-* is the
# fix log written by audit_fix.py).
_AUDIT_FIX_DATE_RE = re.compile(r"^audit-fix-(\d{4}-\d{2}-\d{2})\.json$")

# F-009 commit-message marker for auditor regressions.
_REVERT_MARKER = "(reverted — auditor regressed)"


@dataclass
class DayReport:
    """One day's worth of audit_fix activity."""
    date: str
    file: Path
    total_actions: int = 0
    applied: int = 0
    skipped: int = 0
    failed: int = 0
    failed_validator_rejected: int = 0
    failed_llm_call: int = 0
    failed_other: int = 0
    actions_by_type: Counter = field(default_factory=Counter)
    audit_model_used: str | None = None


@dataclass
class WeekReport:
    days: list[DayReport] = field(default_factory=list)
    missing_days: list[str] = field(default_factory=list)  # dates with no audit-fix file
    revert_commits: list[tuple[str, str]] = field(default_factory=list)  # (sha, subject)
    window_start: date | None = None
    window_end: date | None = None


def _parse_one_audit_fix(path: Path) -> DayReport | None:
    """Parse a single audit-fix-<date>.json file. Returns None on parse error."""
    m = _AUDIT_FIX_DATE_RE.match(path.name)
    if not m:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("failed to read %s: %s", path, exc)
        return None
    rep = DayReport(date=m.group(1), file=path)
    rep.audit_model_used = data.get("audit_model_used")
    actions = data.get("actions") or []
    rep.total_actions = len(actions)
    for a in actions:
        if not isinstance(a, dict):
            continue
        status = (a.get("status") or "").lower()
        atype = a.get("type") or ""
        detail = (a.get("detail") or "").lower()
        if status == "applied":
            rep.applied += 1
        elif status == "skipped":
            rep.skipped += 1
        elif status == "failed":
            rep.failed += 1
            if "validator rejected" in detail:
                rep.failed_validator_rejected += 1
            elif "llm call failed" in detail:
                rep.failed_llm_call += 1
            else:
                rep.failed_other += 1
        if atype:
            rep.actions_by_type[atype] += 1
    return rep


def _enumerate_window_dates(today: date, days: int) -> list[str]:
    """Return list of YYYY-MM-DD strings covering the window (oldest to newest)."""
    start = today - timedelta(days=days - 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(days)]


def _git_log_revert_commits(since: date, sessions_dir: Path) -> list[tuple[str, str]]:
    """Return list of (short-sha, subject) for commits in the last window
    whose subject contains the F-009 revert marker.
    """
    cmd = [
        "git", "-C", str(sessions_dir.parent.resolve()),
        "log", f"--since={since.isoformat()}",
        "--oneline", "--no-decorate",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        log.warning("git log failed: %s", exc)
        return []
    commits = []
    for line in out.splitlines():
        if _REVERT_MARKER not in line:
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2:
            commits.append((parts[0], parts[1]))
    return commits


def collect_week_report(
    sessions_dir: Path,
    *,
    days: int = 7,
    today: date | None = None,
) -> WeekReport:
    """Walk sessions/audit-fix-*.json for the last `days` days. Build report."""
    if today is None:
        today = date.today()
    window_dates = _enumerate_window_dates(today, days)
    window_set = set(window_dates)

    rep = WeekReport(window_start=date.fromisoformat(window_dates[0]), window_end=today)
    seen_dates: set[str] = set()
    for path in sorted(sessions_dir.glob("audit-fix-*.json")):
        day = _parse_one_audit_fix(path)
        if not day:
            continue
        if day.date not in window_set:
            continue
        rep.days.append(day)
        seen_dates.add(day.date)

    rep.missing_days = [d for d in window_dates if d not in seen_dates]
    rep.revert_commits = _git_log_revert_commits(rep.window_start, sessions_dir)
    return rep


def render_text(rep: WeekReport) -> str:
    """Plain-text summary suitable for stdout / email body."""
    lines = []
    win = (
        f"window: {rep.window_start.isoformat()} to {rep.window_end.isoformat()} "
        f"({(rep.window_end - rep.window_start).days + 1} days)"
        if rep.window_start and rep.window_end else "window: ?"
    )
    lines.append(f"=== Jeeves Audit Health — {win} ===")
    lines.append("")

    if not rep.days:
        lines.append("No audit-fix runs in window. Auditor may be disabled "
                     "(JEEVES_AUDITOR_AUTO_FIX=0) or the daily pipeline is failing "
                     "before reaching the auditor step.")
        return "\n".join(lines) + "\n"

    total_applied = sum(d.applied for d in rep.days)
    total_skipped = sum(d.skipped for d in rep.days)
    total_failed = sum(d.failed for d in rep.days)
    total_validator = sum(d.failed_validator_rejected for d in rep.days)
    total_llm = sum(d.failed_llm_call for d in rep.days)
    total_other = sum(d.failed_other for d in rep.days)

    lines.append("Totals:")
    lines.append(f"  applied:                  {total_applied}")
    lines.append(f"  skipped:                  {total_skipped}")
    lines.append(f"  failed:                   {total_failed}")
    lines.append(f"    - validator rejected:   {total_validator}")
    lines.append(f"    - LLM call failed:      {total_llm}")
    lines.append(f"    - other:                {total_other}")
    lines.append("")

    type_counter = Counter()
    for d in rep.days:
        type_counter.update(d.actions_by_type)
    if type_counter:
        lines.append("By type:")
        for t, n in sorted(type_counter.items(), key=lambda kv: -kv[1]):
            lines.append(f"  {t:32s} {n}")
        lines.append("")

    lines.append("Per-day:")
    lines.append("  date          appl  skip  fail  (val/llm/other)  model")
    for d in rep.days:
        model = (d.audit_model_used or "-")[:30]
        lines.append(
            f"  {d.date}    {d.applied:>3}   {d.skipped:>3}   {d.failed:>3}   "
            f"({d.failed_validator_rejected}/{d.failed_llm_call}/{d.failed_other})            "
            f"{model}"
        )
    lines.append("")

    if rep.missing_days:
        lines.append(f"Missing days (no audit-fix-<date>.json): {len(rep.missing_days)}")
        for md in rep.missing_days:
            lines.append(f"  {md}")
        lines.append("")

    if rep.revert_commits:
        lines.append(f"Auditor reverts in window: {len(rep.revert_commits)}")
        for sha, subj in rep.revert_commits:
            lines.append(f"  {sha}  {subj}")
        lines.append("")
    else:
        lines.append("Auditor reverts in window: 0  (gate did not fire)")
        lines.append("")

    # Operator-actionable signals.
    sentinel = []
    if total_validator > 0:
        per_day = total_validator / max(len(rep.days), 1)
        if per_day > 2:
            sentinel.append(
                f"⚠ Validator rejection rate is {per_day:.1f}/day "
                f"(threshold: >2). Reasoning model may be drifting — "
                f"inspect rejected outputs in audit-fix-*.json `evidence.preview` "
                f"or tune validator thresholds."
            )
    if rep.revert_commits:
        sentinel.append(
            f"⚠ {len(rep.revert_commits)} auditor revert(s) — investigate the "
            f"specific defect type that audit_fix introduced before relying on "
            f"the gate alone."
        )
    if not rep.days and rep.missing_days:
        sentinel.append(
            "⚠ No audit-fix runs detected. Either AUTO_FIX=0 or the pipeline "
            "is failing upstream of the auditor."
        )
    if sentinel:
        lines.append("Sentinel:")
        for s in sentinel:
            lines.append(f"  {s}")
    else:
        lines.append("Sentinel: green (no thresholds exceeded).")

    return "\n".join(lines) + "\n"


def render_html(rep: WeekReport) -> str:
    """HTML version of the same content for email."""
    text = render_text(rep)
    # Minimal HTML — preserve whitespace via <pre>, sentinel as colored bar.
    return (
        "<!DOCTYPE html><html><body style=\"font-family: ui-monospace, monospace;"
        " background: #fafafa; padding: 1.5em;\">\n"
        f"<pre style=\"white-space: pre-wrap;\">{_escape_html(text)}</pre>\n"
        "</body></html>\n"
    )


def _escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def main() -> int:
    parser = argparse.ArgumentParser(description="Weekly audit health digest.")
    parser.add_argument("--days", type=int, default=7,
                        help="Window size in days (default 7)")
    parser.add_argument("--sessions-dir", default="sessions",
                        help="Path to sessions/ (default ./sessions)")
    parser.add_argument("--email", default="",
                        help="Send HTML report to this address via jeeves.email.send_html")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sessions_dir = Path(args.sessions_dir).resolve()
    if not sessions_dir.is_dir():
        log.error("sessions dir not found: %s", sessions_dir)
        return 1

    rep = collect_week_report(sessions_dir, days=args.days)
    text = render_text(rep)
    print(text)

    if args.email:
        try:
            from jeeves.email import send_html  # noqa: PLC0415
        except ImportError as exc:
            log.error("cannot import jeeves.email.send_html: %s", exc)
            return 2
        subject = (
            f"Jeeves audit health — {rep.window_start} to {rep.window_end} "
            f"(applied={sum(d.applied for d in rep.days)}, "
            f"reverts={len(rep.revert_commits)})"
        )
        html = render_html(rep)
        try:
            send_html(args.email, subject, html)
            log.info("emailed audit health report to %s", args.email)
        except Exception as exc:  # noqa: BLE001 — report-only path
            log.error("email send failed: %s", exc)
            return 3

    return 0


if __name__ == "__main__":
    sys.exit(main())
