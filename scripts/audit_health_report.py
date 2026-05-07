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

# Match the audit-fix-<date>.json fix log written by scripts/audit_fix.py.
#
# Naming-overlap warning for future maintainers: there are THREE related
# files in sessions/, with similar prefixes — DO NOT widen this regex:
#
#   audit-<date>.json           = pre-fix detection from audit.py (NOT matched
#                                 here — different prefix `audit-` vs `audit-fix-`)
#   audit-<date>.post-fix.json  = post-fix detection from F-009 gate's shell
#                                 dance in daily.yml (NOT matched here — same
#                                 `audit-` prefix as pre-fix)
#   audit-fix-<date>.json       = the fix log this script consumes (matched
#                                 by the anchor `^audit-fix-`)
#
# `^` and `$` anchors are load-bearing — without them the trailing `.post-fix`
# variant could leak into the match if naming ever changes.
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
    # Number of commits available in the local history (from `git rev-list
    # --count HEAD`). Compared to `days` to detect shallow checkouts that
    # may miss old auditor-revert commits. None when git is unavailable.
    git_history_depth: int | None = None
    # True when `git_history_depth` looks like it covers the requested
    # window comfortably (heuristic: at least 1 commit per day in window
    # plus a small buffer). False when shallow checkout may have truncated
    # the revert-commit scan; surfaces as a sentinel warning.
    history_covers_window: bool = True


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


def _git_history_depth(sessions_dir: Path) -> int | None:
    """Return number of commits reachable from HEAD via `git rev-list --count`.

    Returns None when git is unavailable or sessions_dir.parent is not a repo.
    Used to detect shallow checkouts that may truncate the revert-commit scan
    in `_git_log_revert_commits`.
    """
    cmd = [
        "git", "-C", str(sessions_dir.parent.resolve()),
        "rev-list", "--count", "HEAD",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    out = out.strip()
    return int(out) if out.isdigit() else None


def _history_covers_window(history_depth: int | None, days: int) -> bool:
    """Heuristic: does the local git history reach back far enough to cover
    the requested window without missing revert-commits?

    Production daily-pipeline cadence is ~5 commits/day (correspondence,
    research, write, audit, audit-fix). Plus the occasional fix PR. So a
    ~7-day window needs roughly 35-50 commits of history. We require at
    least `days * 6` to give 20% buffer over the average. Heuristic only —
    when False we surface a sentinel WARN, not a hard fail.
    """
    if history_depth is None:
        return True  # unknown — don't false-warn the operator
    return history_depth >= days * 6


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
    rep.git_history_depth = _git_history_depth(sessions_dir)
    rep.history_covers_window = _history_covers_window(rep.git_history_depth, days)
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
    if not rep.history_covers_window and rep.git_history_depth is not None:
        sentinel.append(
            f"⚠ Git history depth ({rep.git_history_depth} commits) may not "
            f"cover the {(rep.window_end - rep.window_start).days + 1}-day window "
            f"(rule of thumb: ~6 commits/day = "
            f"{((rep.window_end - rep.window_start).days + 1) * 6} expected). "
            f"Auditor-revert scan may have missed older commits. Bump "
            f"`fetch-depth` in the workflow if you widen the window beyond "
            f"~5 days."
        )
    if sentinel:
        lines.append("Sentinel:")
        for s in sentinel:
            lines.append(f"  {s}")
    else:
        lines.append("Sentinel: green (no thresholds exceeded).")

    return "\n".join(lines) + "\n"


def _escape_html(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# Email-client compatibility: inline styles only (Gmail/Outlook strip <style>
# blocks unpredictably). Use <table> for layout (broadest compat). Keep a
# single max-width: 600px container so mobile clients render readably.
_EMAIL_BODY_BG = "#fafafa"
_CARD_BG = "#ffffff"
_CARD_BORDER = "1px solid #e1e4e8"
_TEXT_COLOR = "#1f2328"
_MUTED_COLOR = "#6e7781"
_OK_COLOR = "#1a7f37"      # green — sentinel green
_WARN_COLOR = "#bf8700"    # amber — sentinel warn
_ALERT_COLOR = "#cf222e"   # red — sentinel alert (revert events)


def _chip(label: str, value: str, color: str) -> str:
    """Color-coded summary chip for the totals row."""
    return (
        f'<td style="padding: 8px 12px; background: {_CARD_BG}; '
        f'border: {_CARD_BORDER}; border-radius: 4px; min-width: 80px;">'
        f'<div style="color: {_MUTED_COLOR}; font-size: 11px; '
        f'text-transform: uppercase; letter-spacing: 0.04em;">{_escape_html(label)}</div>'
        f'<div style="color: {color}; font-size: 22px; font-weight: 600; '
        f'line-height: 1.2;">{_escape_html(value)}</div>'
        f"</td>"
    )


def _sentinel_chip(message: str, color: str) -> str:
    """Single sentinel row — color bar + message."""
    return (
        f'<tr><td style="padding: 10px 12px; background: {_CARD_BG}; '
        f'border-left: 4px solid {color}; border-top: 1px solid #e1e4e8;">'
        f'<span style="color: {color}; font-weight: 600;">●</span> '
        f'<span style="color: {_TEXT_COLOR};">{_escape_html(message)}</span>'
        f"</td></tr>"
    )


def _per_day_table(rep: WeekReport) -> str:
    """Per-day table; mobile-readable via min-width: 0 + word-break."""
    if not rep.days:
        return ""
    rows = [
        '<tr style="background: #f6f8fa; color: ' + _MUTED_COLOR
        + '; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;">'
        '<th style="padding: 6px 8px; text-align: left;">Date</th>'
        '<th style="padding: 6px 8px; text-align: right;">Appl</th>'
        '<th style="padding: 6px 8px; text-align: right;">Skip</th>'
        '<th style="padding: 6px 8px; text-align: right;">Fail</th>'
        '<th style="padding: 6px 8px; text-align: right;">Val/LLM/Other</th>'
        '<th style="padding: 6px 8px; text-align: left;">Model</th>'
        "</tr>"
    ]
    for d in rep.days:
        model = (d.audit_model_used or "—")
        if len(model) > 30:
            model = model[:27] + "…"
        # Color failed cell red if any failures.
        fail_color = _ALERT_COLOR if d.failed > 0 else _TEXT_COLOR
        rows.append(
            f'<tr style="border-top: 1px solid #e1e4e8;">'
            f'<td style="padding: 6px 8px; font-family: ui-monospace, monospace; '
            f'font-size: 12px; color: {_TEXT_COLOR};">{d.date}</td>'
            f'<td style="padding: 6px 8px; text-align: right; color: {_OK_COLOR}; font-weight: 600;">{d.applied}</td>'
            f'<td style="padding: 6px 8px; text-align: right; color: {_MUTED_COLOR};">{d.skipped}</td>'
            f'<td style="padding: 6px 8px; text-align: right; color: {fail_color}; font-weight: 600;">{d.failed}</td>'
            f'<td style="padding: 6px 8px; text-align: right; font-family: ui-monospace, monospace; '
            f'font-size: 11px; color: {_MUTED_COLOR};">'
            f'{d.failed_validator_rejected}/{d.failed_llm_call}/{d.failed_other}</td>'
            f'<td style="padding: 6px 8px; font-family: ui-monospace, monospace; '
            f'font-size: 11px; color: {_MUTED_COLOR};">{_escape_html(model)}</td>'
            f"</tr>"
        )
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        f'style="width: 100%; border-collapse: collapse; background: {_CARD_BG}; '
        f'border: {_CARD_BORDER}; border-radius: 4px; margin: 16px 0;">'
        + "".join(rows) +
        "</table>"
    )


def _classify_sentinels(rep: WeekReport) -> list[tuple[str, str]]:
    """Return list of (color, message) pairs. Pure function — same logic as
    render_text's sentinel block, but typed for the HTML renderer.
    """
    out: list[tuple[str, str]] = []
    total_validator = sum(d.failed_validator_rejected for d in rep.days)
    if total_validator > 0:
        per_day = total_validator / max(len(rep.days), 1)
        if per_day > 2:
            out.append((
                _WARN_COLOR,
                f"Validator rejection rate is {per_day:.1f}/day "
                f"(threshold: >2). Reasoning model may be drifting — inspect "
                f"rejected outputs in audit-fix-*.json `evidence.preview` or "
                f"tune validator thresholds.",
            ))
    if rep.revert_commits:
        out.append((
            _ALERT_COLOR,
            f"{len(rep.revert_commits)} auditor revert(s) — investigate the "
            f"specific defect type that audit_fix introduced before relying "
            f"on the gate alone.",
        ))
    if not rep.days and rep.missing_days:
        out.append((
            _WARN_COLOR,
            "No audit-fix runs detected. Either AUTO_FIX=0 or the pipeline "
            "is failing upstream of the auditor.",
        ))
    if not rep.history_covers_window and rep.git_history_depth is not None:
        days = (rep.window_end - rep.window_start).days + 1
        out.append((
            _WARN_COLOR,
            f"Git history depth ({rep.git_history_depth} commits) may not "
            f"cover the {days}-day window — auditor-revert scan may have "
            f"missed older commits. Bump fetch-depth in the workflow if you "
            f"widen the window beyond ~5 days.",
        ))
    return out


def render_html(rep: WeekReport) -> str:
    """Mobile-friendly HTML email body.

    Single 600px-max-width container. Inline styles only (email clients
    strip <style> tags unpredictably). Tables for layout (broad compat).

    Sections (in order):
      1. Header — title + window
      2. Summary chips — applied / skipped / failed (color-coded)
      3. Sentinel block — colored chips when thresholds exceeded
      4. Per-day table
      5. Reverts section (if any)
      6. Missing-days section (if any)
      7. Footer — link to AUTO_FIX-re-enable-protocol.md
    """
    days = (rep.window_end - rep.window_start).days + 1 if rep.window_start and rep.window_end else 0
    win_label = (
        f"{rep.window_start.isoformat()} → {rep.window_end.isoformat()} ({days} days)"
        if rep.window_start and rep.window_end else "?"
    )

    # ── empty-window short-circuit ────────────────────────────────────────
    if not rep.days:
        return (
            "<!DOCTYPE html>"
            "<html><body style=\"margin: 0; padding: 24px; "
            f"background: {_EMAIL_BODY_BG}; font-family: -apple-system, "
            "BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, "
            f"sans-serif; color: {_TEXT_COLOR};\">"
            "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
            f"style=\"max-width: 600px; margin: 0 auto; background: {_CARD_BG}; "
            f"border: {_CARD_BORDER}; border-radius: 6px; padding: 20px;\">"
            f"<tr><td><h1 style=\"margin: 0 0 8px; font-size: 18px;\">Jeeves Audit Health</h1>"
            f"<p style=\"margin: 0 0 16px; color: {_MUTED_COLOR}; font-size: 13px;\">"
            f"{_escape_html(win_label)}</p>"
            f"<div style=\"padding: 12px; border-left: 4px solid {_WARN_COLOR}; "
            f"background: #fff8c5;\"><strong>No audit-fix runs in window.</strong> "
            f"Either <code>JEEVES_AUDITOR_AUTO_FIX=0</code> or the daily pipeline "
            f"is failing before reaching the auditor step.</div></td></tr></table>"
            "</body></html>"
        )

    total_applied = sum(d.applied for d in rep.days)
    total_skipped = sum(d.skipped for d in rep.days)
    total_failed = sum(d.failed for d in rep.days)
    total_validator = sum(d.failed_validator_rejected for d in rep.days)
    total_llm = sum(d.failed_llm_call for d in rep.days)
    total_other = sum(d.failed_other for d in rep.days)

    # ── summary chips row ──────────────────────────────────────────────────
    applied_color = _OK_COLOR if total_applied > 0 else _MUTED_COLOR
    failed_color = _ALERT_COLOR if total_failed > 0 else _MUTED_COLOR
    revert_color = _ALERT_COLOR if rep.revert_commits else _MUTED_COLOR
    chips_row = (
        '<table role="presentation" cellpadding="0" cellspacing="6" border="0" '
        'style="margin: 16px 0; width: 100%;">'
        "<tr>"
        + _chip("applied", str(total_applied), applied_color)
        + _chip("skipped", str(total_skipped), _MUTED_COLOR)
        + _chip("failed", str(total_failed), failed_color)
        + _chip("reverts", str(len(rep.revert_commits)), revert_color)
        + "</tr></table>"
    )

    # ── failure breakdown line (no chip — sub-summary) ─────────────────────
    failure_breakdown = ""
    if total_failed > 0:
        failure_breakdown = (
            f'<p style="margin: 0 0 12px; color: {_MUTED_COLOR}; font-size: 13px;">'
            f'Failed breakdown — validator: <strong>{total_validator}</strong> · '
            f'LLM call: <strong>{total_llm}</strong> · '
            f'other: <strong>{total_other}</strong></p>'
        )

    # ── sentinel block ─────────────────────────────────────────────────────
    sentinels = _classify_sentinels(rep)
    sentinel_block = ""
    if sentinels:
        sentinel_rows = "".join(_sentinel_chip(msg, color) for color, msg in sentinels)
        sentinel_block = (
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            'style="width: 100%; border-collapse: separate; '
            'border-spacing: 0 6px; margin: 16px 0;">'
            + sentinel_rows
            + "</table>"
        )
    else:
        sentinel_block = (
            f'<div style="margin: 16px 0; padding: 10px 12px; background: {_CARD_BG}; '
            f'border-left: 4px solid {_OK_COLOR}; border-top: 1px solid #e1e4e8;">'
            f'<span style="color: {_OK_COLOR}; font-weight: 600;">●</span> '
            f'<span style="color: {_TEXT_COLOR};">Sentinel: green — no thresholds exceeded.</span>'
            "</div>"
        )

    # ── reverts list (if any) ──────────────────────────────────────────────
    revert_block = ""
    if rep.revert_commits:
        revert_rows = "".join(
            f'<tr><td style="padding: 4px 8px; font-family: ui-monospace, monospace; '
            f'font-size: 11px; color: {_MUTED_COLOR};">{_escape_html(sha)}</td>'
            f'<td style="padding: 4px 8px; font-size: 12px; color: {_TEXT_COLOR};">'
            f'{_escape_html(subj)}</td></tr>'
            for sha, subj in rep.revert_commits
        )
        revert_block = (
            f'<h3 style="margin: 16px 0 4px; font-size: 14px; color: {_ALERT_COLOR};">'
            f'Auditor reverts in window ({len(rep.revert_commits)})</h3>'
            '<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'style="width: 100%; background: {_CARD_BG}; border: {_CARD_BORDER}; '
            'border-radius: 4px;">'
            + revert_rows
            + "</table>"
        )

    # ── missing days (if any) ──────────────────────────────────────────────
    missing_block = ""
    if rep.missing_days:
        missing_block = (
            f'<p style="margin: 12px 0 0; color: {_MUTED_COLOR}; font-size: 12px;">'
            f'Missing days ({len(rep.missing_days)}): '
            f'{", ".join(rep.missing_days)}</p>'
        )

    return (
        "<!DOCTYPE html>"
        "<html><body style=\"margin: 0; padding: 24px; "
        f"background: {_EMAIL_BODY_BG}; font-family: -apple-system, "
        "BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, "
        f"sans-serif; color: {_TEXT_COLOR}; line-height: 1.4;\">"
        "<table role=\"presentation\" cellpadding=\"0\" cellspacing=\"0\" "
        f"style=\"max-width: 600px; margin: 0 auto; background: {_CARD_BG}; "
        f"border: {_CARD_BORDER}; border-radius: 6px; padding: 20px;\">"
        f"<tr><td>"
        # header
        f"<h1 style=\"margin: 0 0 4px; font-size: 18px;\">Jeeves Audit Health</h1>"
        f"<p style=\"margin: 0 0 12px; color: {_MUTED_COLOR}; font-size: 13px; "
        f"font-family: ui-monospace, monospace;\">{_escape_html(win_label)}</p>"
        # summary
        + chips_row
        + failure_breakdown
        # sentinel
        + sentinel_block
        # per-day table
        + f"<h3 style=\"margin: 16px 0 4px; font-size: 14px;\">Per day</h3>"
        + _per_day_table(rep)
        # reverts
        + revert_block
        # missing days
        + missing_block
        # footer
        + f"<p style=\"margin: 20px 0 0; padding-top: 12px; border-top: 1px solid #e1e4e8; "
        f"color: {_MUTED_COLOR}; font-size: 11px;\">"
        f"Generated by <code>scripts/audit_health_report.py</code>. "
        f"Re-enable protocol: <code>.claude/plans/AUTO_FIX-re-enable-protocol.md</code>.</p>"
        f"</td></tr></table>"
        "</body></html>"
    )


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
