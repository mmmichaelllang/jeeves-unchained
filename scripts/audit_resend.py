#!/usr/bin/env python3
"""Re-send a revised briefing email after the auditor applied fixes.

Reads sessions/audit-fix-<date>.json — only sends if applied actions > 0.
Subject is prefixed with [REVISED]. Used by the auditor job in daily.yml
when JEEVES_AUDITOR_RESEND=1 and JEEVES_AUDITOR_AUTO_FIX=1 are both set.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("audit_resend")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    parser.add_argument("--sessions-dir", default="sessions")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sessions_dir = Path(args.sessions_dir).resolve()
    fix_log = sessions_dir / f"audit-fix-{args.date}.json"
    briefing = sessions_dir / f"briefing-{args.date}.html"

    if not fix_log.exists():
        log.info("no fix log — nothing to re-send")
        return 0
    if not briefing.exists():
        log.error("revised briefing missing: %s", briefing)
        return 1

    data = json.loads(fix_log.read_text(encoding="utf-8"))
    applied = sum(1 for a in (data.get("actions") or [])
                  if a.get("status") == "applied")
    if applied == 0:
        log.info("no fixes applied — skipping re-send")
        return 0

    log.info("re-sending revised briefing (%d fixes applied)", applied)
    try:
        from jeeves.email import send_html
        from jeeves.config import Config
    except Exception as exc:
        log.error("import failed: %s", exc)
        return 1

    # 2026-05-09: was a dead-code import (`send_email` didn't exist). The
    # auditor was committing post-fix briefings but the [REVISED] email
    # was never actually leaving the runner. Today's user-visible double
    # email came from retry-failed.yml re-firing the whole Daily Pipeline,
    # not from this resend.
    try:
        from datetime import datetime
        cfg = Config.from_env(phase="write", dry_run=False, run_date=args.date)
        html = briefing.read_text(encoding="utf-8")
        full_date = datetime.strptime(args.date, "%Y-%m-%d").strftime(
            "%A, %B %-d, %Y"
        )
        subject = f"[REVISED] 📜 Daily Intelligence from Jeeves — {full_date}"
        send_html(
            to=cfg.recipient_email,
            sender=cfg.recipient_email,
            subject=subject,
            html=html,
            app_password=cfg.gmail_app_password,
        )
        log.info("re-send complete")
        return 0
    except Exception as exc:
        log.error("re-send failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
