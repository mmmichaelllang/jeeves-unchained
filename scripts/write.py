#!/usr/bin/env python3
"""Phase 3 — Write script (STUB).

Loads the session JSON for a given date and prints the sector summaries a
full Phase 3 implementation would render. Proves the Phase 2 contract is
stable before Phase 3 is built.

Usage:
  python scripts/write.py --date 2026-04-23 --plan-only
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.session_io import load_session_by_date  # noqa: E402

log = logging.getLogger("jeeves.write")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jeeves write phase (Phase 3 — stub).")
    p.add_argument("--date", required=False, help="Session date (YYYY-MM-DD). Defaults to today UTC.")
    p.add_argument(
        "--plan-only",
        action="store_true",
        help="Load + summarize the session JSON; do not generate HTML or send email.",
    )
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args(argv)


def _plan_only(session) -> None:
    print(f"Session date: {session.date}")
    print(f"Status: {session.status}")
    print(f"Dedup URLs: {len(session.dedup.covered_urls)}")
    print(f"Local news entries: {len(session.local_news)}")
    print(f"Global news entries: {len(session.global_news)}")
    print(f"Intellectual journal entries: {len(session.intellectual_journals)}")
    print(f"Wearable AI entries: {len(session.wearable_ai)}")
    print(f"Enriched articles: {len(session.enriched_articles)}")
    print(f"Weather: {session.weather[:80]}")
    print(f"New Yorker available: {session.newyorker.available}")
    print(f"Vault insight available: {session.vault_insight.available}")
    print("\nTop career findings:")
    print("  " + str(session.career)[:200])
    print("\nThis is a plan-only stub. Phase 3 will:")
    print("  1) Load Jeeves voice prompt (ported from cloud-write-prompt.md).")
    print("  2) Render HTML via Groq Llama 3.3 70B.")
    print("  3) Send via SMTP using GMAIL_APP_PASSWORD.")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        cfg = Config.from_env(dry_run=True, run_date=args.date)
    except MissingSecret as e:
        log.error(str(e))
        return 2

    try:
        session = load_session_by_date(cfg, cfg.run_date)
    except FileNotFoundError:
        log.error("No session file found for %s", cfg.run_date.isoformat())
        return 3

    if args.plan_only:
        _plan_only(session)
        return 0

    log.warning("Phase 3 is a stub — nothing to render yet. Use --plan-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
