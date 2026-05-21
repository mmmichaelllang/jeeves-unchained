#!/usr/bin/env python3
"""Tier 1 monitor for jeeves-unchained adaptive-loop progress.

Runs every 30 min via GHA. Deterministic checks against LOOP_STATE.md + git.
On ALERT: opens a GitHub issue + sends email via existing GMAIL_APP_PASSWORD.

Exit codes:
  0 — OK
  2 — ALERT (one or more checks failed)

Acknowledgments: write `.loop-watch-ack.json` with {alert_key: iso_timestamp}
to suppress a known-handled alert. Removed alerts re-fire on the next anomaly.

Tier 2 (Opus reasoning via Cowork scheduled task) escalates beyond what this
script can catch: design rationalization, scope drift, narrative red flags.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import smtplib
import subprocess
import sys
import urllib.error
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
LOOP_STATE = ROOT / "LOOP_STATE.md"
ROADMAP = ROOT / "ROADMAP.md"
ACK_FILE = ROOT / ".loop-watch-ack.json"
DECISIONS = ROOT / "decisions"


# ----- Parsing --------------------------------------------------------------

def parse_loop_state() -> dict:
    """Extract structured fields from LOOP_STATE.md."""
    text = LOOP_STATE.read_text(encoding="utf-8")

    def grab(label: str, multiline: bool = False) -> Optional[str]:
        # `\n+` after the capture lets the section end on either blank-then-##
        # or immediately-##. Without it we miss any section that ends with a
        # blank line before the next ##.
        pattern = rf"## {re.escape(label)}\n(.+?)(?=\n+##|\Z)"
        flags = re.DOTALL if multiline else 0
        m = re.search(pattern, text, flags)
        return m.group(1).strip() if m else None

    return {
        "last_updated": grab("Last Updated"),
        "iteration_raw": grab("Iteration") or "",
        "iteration": _extract_int(grab("Iteration") or ""),
        "last_milestone": grab("Last Milestone"),
        "last_outcome": grab("Last Outcome"),
        "same_blocker_count": _extract_int(grab("Same Blocker Count") or ""),
        "refined_done_when": grab("Refined DONE WHEN", multiline=True),
        "active_branch": grab("Active Branch"),
        "last_blocker": grab("Last Blocker", multiline=True),
    }


def _extract_int(s: str) -> int:
    m = re.match(r"^(\d+)", s.strip())
    return int(m.group(1)) if m else 0


def run_git(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git"] + args, cwd=ROOT, capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except Exception:
        return ""


def latest_commit_on_branch(branch: str) -> Optional[dict]:
    out = run_git(["log", "-1", "--format=%H|%ct|%s", branch])
    if not out or "|" not in out:
        return None
    try:
        sha, ct, msg = out.split("|", 2)
        age_h = (datetime.datetime.utcnow().timestamp() - int(ct)) / 3600.0
        return {"sha": sha, "age_hours": age_h, "message": msg}
    except Exception:
        return None


def get_ack() -> dict:
    if not ACK_FILE.exists():
        return {}
    try:
        return json.loads(ACK_FILE.read_text())
    except Exception:
        return {}


def is_acked(alert_key: str) -> bool:
    return alert_key in get_ack()


def latest_decision_doc() -> Optional[Path]:
    if not DECISIONS.exists():
        return None
    files = sorted(DECISIONS.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


# ----- Checks ---------------------------------------------------------------

def check_blocker_count(state: dict) -> Optional[str]:
    n = state["same_blocker_count"]
    if n >= 3:
        return f"same_blocker_count={n} (≥3 — stuck pattern, manual intervention required)"
    return None


def check_iteration_cap(state: dict) -> Optional[str]:
    i = state["iteration"]
    if i > 25:
        return f"iteration={i} (>25 — soft cap exceeded; check ROADMAP for stalled milestone)"
    return None


def check_stop_outcome(state: dict) -> Optional[str]:
    outcome = (state["last_outcome"] or "").upper()
    if outcome.startswith("STOP"):
        alert_key = f"stop:{state['iteration']}:{state['last_milestone']}"
        if is_acked(alert_key):
            return None
        return (
            f"last_outcome=STOP at iteration={state['iteration']} on milestone "
            f"{state['last_milestone']!r} (unacked)"
        )
    return None


def check_stalled_branch(state: dict) -> Optional[str]:
    branch_raw = (state["active_branch"] or "").strip()
    branch = branch_raw.split()[0] if branch_raw else ""
    if not branch or branch in ("main", "master"):
        return None
    if not branch.startswith("feat/M"):
        return None
    info = latest_commit_on_branch(branch)
    if info is None:
        return f"active_branch={branch} declared in LOOP_STATE but not found in git"
    if info["age_hours"] > 6:
        return (
            f"active_branch={branch} stalled — last commit was "
            f"{info['age_hours']:.1f}h ago (>6h while iteration active)"
        )
    return None


def check_revise_no_progress(state: dict) -> Optional[str]:
    """REVISE decision in latest doc + no commits in 12h → loop ignored direction."""
    doc = latest_decision_doc()
    if doc is None:
        return None
    try:
        text = doc.read_text(encoding="utf-8")
    except Exception:
        return None
    if "DECISION: REVISE" not in text:
        return None
    out = run_git(["log", "-1", "--all", "--format=%ct"])
    if not out.isdigit():
        return None
    age_h = (datetime.datetime.utcnow().timestamp() - int(out)) / 3600.0
    if age_h > 12:
        return (
            f"DECISION: REVISE present in {doc.name} but no commits in {age_h:.1f}h"
        )
    return None


def check_branch_field_match(state: dict) -> Optional[str]:
    branch_raw = (state["active_branch"] or "").strip()
    expected = branch_raw.split()[0] if branch_raw else ""
    if not expected or expected in ("main", "master"):
        return None
    if not expected.startswith("feat/"):
        return None
    current = run_git(["branch", "--show-current"])
    if current.startswith("feat/") and current != expected:
        return f"LOOP_STATE active_branch={expected} but git HEAD={current}"
    return None


def check_repeated_milestone(state: dict) -> Optional[str]:
    """Same milestone attempted ≥3 times in history → design likely wrong."""
    text = LOOP_STATE.read_text(encoding="utf-8")
    target = state["last_milestone"] or ""
    if not target:
        return None
    # Extract milestone code (e.g. "M0", "M1") from string like "M0 (Probe...) — second attempt"
    m = re.search(r"\b(M\d+(?:\.\d+)?)\b", target)
    if not m:
        return None
    code = m.group(1)
    history_attempts = len(re.findall(rf"\|\s*\d+[a-z]?\s*\|\s*{re.escape(code)}\b", text))
    if history_attempts >= 3:
        return f"{code} attempted {history_attempts} times in history — design likely wrong"
    return None


def check_recent_commit_activity(state: dict) -> Optional[str]:
    """Loop active (iteration>0) but no commits across all branches in last 4h."""
    if state["iteration"] < 1:
        return None
    out = run_git(["log", "-1", "--all", "--format=%ct"])
    if not out.isdigit():
        return None
    age_h = (datetime.datetime.utcnow().timestamp() - int(out)) / 3600.0
    if age_h > 4:
        return f"no commits across any branch in {age_h:.1f}h while loop iteration={state['iteration']}"
    return None


# ----- Reporting ------------------------------------------------------------

def write_intervention(text: str, source: str = "Tier 1 monitor") -> None:
    """Inject a next_priority into LOOP_STATE.md so the loop self-corrects.

    Loop reads LOOP_STATE.md at STEP 1 and honors a non-empty next_priority
    at STEP 3 (overrides all standard rules). After honoring, the loop clears
    the field. We append timestamp + source so emergency rollback can audit.

    Format intervention text so the loop knows: HALT / PIVOT / INVESTIGATE.
    """
    if not LOOP_STATE.exists():
        return
    text_content = LOOP_STATE.read_text(encoding="utf-8")
    timestamp = datetime.datetime.utcnow().isoformat() + "Z"
    block = (
        f"INTERVENTION [{timestamp}] [{source}]:\n"
        f"{text}\n"
        f"(Loop honors this verbatim per loop.md STEP 3; clears after execution.)\n"
    )
    # Find ## Next Priority section, replace contents up to next ##.
    pattern = r"(## Next Priority\n)(.*?)(?=\n+##|\Z)"
    new_content, count = re.subn(
        pattern,
        lambda m: m.group(1) + block,
        text_content,
        count=1,
        flags=re.DOTALL,
    )
    if count == 0:
        # No Next Priority section; append one
        new_content = text_content.rstrip() + f"\n\n## Next Priority\n{block}"
    LOOP_STATE.write_text(new_content, encoding="utf-8")
    print(f"[monitor] wrote intervention to LOOP_STATE.md next_priority")


def has_active_intervention() -> bool:
    """True if next_priority already contains a fresh intervention block.

    Prevents stacking duplicate interventions every 30 min before the loop
    honors the existing one.
    """
    if not LOOP_STATE.exists():
        return False
    text = LOOP_STATE.read_text(encoding="utf-8")
    m = re.search(r"## Next Priority\n(.*?)(?=\n+##|\Z)", text, re.DOTALL)
    if not m:
        return False
    body = m.group(1).strip()
    return body.startswith("INTERVENTION [")


def alert(messages: list[str], state: dict, dry_run: bool = False) -> None:
    body_lines = [
        "## Tier 1 monitor — anomaly detected in jeeves-unchained adaptive-loop",
        "",
        *[f"- 🔴 {m}" for m in messages],
        "",
        "## Loop state snapshot",
        f"- iteration: {state['iteration']}",
        f"- last_milestone: {state['last_milestone']}",
        f"- last_outcome: {state['last_outcome']}",
        f"- same_blocker_count: {state['same_blocker_count']}",
        f"- active_branch: {state['active_branch']}",
        f"- last_updated: {state['last_updated']}",
        "",
        "## Acknowledge to suppress",
        "Add the alert key to `.loop-watch-ack.json` to silence this alert:",
        "```json",
        "{",
        f'  "stop:{state["iteration"]}:{state["last_milestone"]}": "{datetime.datetime.utcnow().isoformat()}Z"',
        "}",
        "```",
        "",
        "Source: LOOP_STATE.md + git state in repo.",
    ]
    body = "\n".join(body_lines)
    title = f"[loop-monitor] {len(messages)} alert(s): {messages[0][:60]}"

    if dry_run:
        print("=== DRY RUN ===")
        print(f"TITLE: {title}")
        print(body)
        return

    # Auto-intervention: if any STOP-rule trip detected, write next_priority
    # so the loop self-halts on next iteration. Skip if intervention already
    # active (loop hasn't yet honored prior one).
    stop_triggers = [
        m for m in messages
        if "stuck pattern" in m
        or "STOP at iteration" in m
        or "soft cap exceeded" in m
        or "attempted" in m
    ]
    if stop_triggers and not has_active_intervention():
        intervention_text = (
            "HALT. Tier 1 monitor detected stop-rule trip:\n"
            + "\n".join(f"  - {m}" for m in stop_triggers)
            + "\n\nDO NOT continue the current milestone. Set last_outcome=STOP "
            + "in LOOP_STATE.md and emit USER ACTION REQUIRED. Wait for user "
            + "ack in `.loop-watch-ack.json` or further instructions in "
            + "next_priority before resuming."
        )
        write_intervention(intervention_text, source="Tier 1 monitor")

    issue_url = create_github_issue(title, body)
    suffix = f"\n\nGitHub issue: {issue_url}" if issue_url else ""
    send_email(title, body + suffix)


def create_github_issue(title: str, body: str) -> Optional[str]:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "mmmichaelllang/jeeves-unchained")
    if not token:
        print("[monitor] no GITHUB_TOKEN, skipping issue creation")
        return None

    url = f"https://api.github.com/repos/{repo}/issues"
    data = json.dumps({"title": title, "body": body, "labels": ["loop-monitor"]}).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp)
            print(f"[monitor] opened issue: {payload.get('html_url')}")
            return payload.get("html_url")
    except Exception as e:
        print(f"[monitor] issue creation failed: {e}")
        return None


def send_email(subject: str, body: str) -> None:
    user = os.environ.get("GMAIL_USER", "lang.mc@gmail.com")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    recipient = os.environ.get("JEEVES_RECIPIENT_EMAIL", "lang.mc@gmail.com")
    if not password:
        print("[monitor] no GMAIL_APP_PASSWORD, skipping email")
        return

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        print(f"[monitor] email sent to {recipient}")
    except Exception as e:
        print(f"[monitor] email failed: {e}")


# ----- Main -----------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Tier 1 adaptive-loop monitor.")
    parser.add_argument("--dry-run", action="store_true", help="Print alerts; do not send.")
    args = parser.parse_args()

    if not LOOP_STATE.exists():
        print(f"[monitor] {LOOP_STATE} not found, skipping")
        return 0

    state = parse_loop_state()

    checks = [
        check_blocker_count,
        check_iteration_cap,
        check_stop_outcome,
        check_stalled_branch,
        check_revise_no_progress,
        check_branch_field_match,
        check_repeated_milestone,
        check_recent_commit_activity,
    ]

    alerts: list[str] = []
    for check in checks:
        try:
            result = check(state)
            if result:
                alerts.append(result)
        except Exception as e:
            print(f"[monitor] check {check.__name__} crashed: {e}")

    if not alerts:
        print(
            f"[monitor] OK — iter={state['iteration']} "
            f"milestone={state['last_milestone']!r} "
            f"outcome={state['last_outcome']!r}"
        )
        return 0

    print(f"[monitor] {len(alerts)} alert(s):")
    for a in alerts:
        print(f"  - {a}")

    alert(alerts, state, dry_run=args.dry_run)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
