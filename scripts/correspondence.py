#!/usr/bin/env python3
"""Phase 4 — Correspondence driver.

Sweeps Gmail via OAuth, classifies threads with Kimi K2.5, renders a Jeeves-voice
brief via Groq Llama 3.3 70B, persists a handoff JSON for the research phase,
and emails the brief via Gmail SMTP.

Usage:
  python scripts/correspondence.py --date 2026-04-23
  python scripts/correspondence.py --dry-run                  # no Gmail, no Kimi, no Groq, no SMTP
  python scripts/correspondence.py --skip-send                # real Gmail + Kimi + Groq, no SMTP
  python scripts/correspondence.py --use-fixture --skip-send  # fixture inbox, real Kimi + Groq, no SMTP
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.correspondence import (  # noqa: E402
    CorrespondenceResult,
    build_handoff_json,
    classify_with_kimi,
    fixture_classified,
    fixture_previews,
    load_priority_contacts,
    postprocess_html,
    render_mock_correspondence,
    render_with_groq,
)
from jeeves.email import SMTPConfigError, send_html  # noqa: E402

log = logging.getLogger("jeeves.correspondence")

SUBJECT_TEMPLATE = "📫 Correspondence — {full_date}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jeeves correspondence phase (Phase 4).")
    p.add_argument("--date", default=None, help="Run date (YYYY-MM-DD). Defaults to today UTC.")
    p.add_argument("--dry-run", action="store_true", help="Fixture HTML only; no Gmail, no model calls, no SMTP.")
    p.add_argument("--skip-send", action="store_true", help="Real Gmail + models; no SMTP.")
    p.add_argument("--use-fixture", action="store_true", help="Skip Gmail and use the canned fixture inbox.")
    p.add_argument("--days", type=int, default=60, help="Gmail sweep window in days (default 60).")
    p.add_argument("--max-messages", type=int, default=50, help="Max unread messages to fetch (default 50, newest first).")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args(argv)


def _run(cfg: Config, args: argparse.Namespace) -> CorrespondenceResult:
    contacts = load_priority_contacts()
    fallback_used = False

    if args.dry_run:
        log.info("DRY RUN — fixture inbox + fixture classification + mock HTML.")
        classified = fixture_classified()
        html_raw = render_mock_correspondence(cfg.run_date.isoformat(), classified)
    elif args.use_fixture:
        log.info("--use-fixture: canned inbox, real Kimi + Groq.")
        previews = fixture_previews()
        classified = classify_with_kimi(cfg, previews, contacts)
        html_raw = render_with_groq(cfg, classified, contacts, run_date_iso=cfg.run_date.isoformat())
    else:
        from jeeves.gmail import build_gmail_service, sweep_recent

        service = build_gmail_service(cfg.gmail_oauth_token_json)
        previews = sweep_recent(service, days=args.days, max_results=args.max_messages)
        if not previews:
            log.warning("gmail sweep returned 0 messages — marking fallback_used=True.")
            fallback_used = True
        classified = classify_with_kimi(cfg, previews, contacts)
        html_raw = render_with_groq(cfg, classified, contacts, run_date_iso=cfg.run_date.isoformat())

    html, word_count, profane_count, banned_words, banned_transitions = postprocess_html(html_raw)
    handoff = build_handoff_json(classified, fallback_used=fallback_used)

    return CorrespondenceResult(
        html=html,
        handoff=handoff,
        classified=classified,
        word_count=word_count,
        profane_aside_count=profane_count,
        banned_word_hits=banned_words,
        banned_transition_hits=banned_transitions,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    loosen_env = args.dry_run or args.skip_send or args.use_fixture
    try:
        cfg = Config.from_env(
            phase="correspondence",
            dry_run=loosen_env,
            run_date=args.date,
            verbose=args.verbose,
        )
    except MissingSecret as e:
        log.error(str(e))
        return 2

    # When running real models (skip-send or use-fixture), enforce NIM + Groq keys.
    if (args.skip_send or args.use_fixture) and not args.dry_run:
        missing = [
            name for name, val in [
                ("NVIDIA_API_KEY", cfg.nvidia_api_key),
                ("GROQ_API_KEY", cfg.groq_api_key),
            ] if not val
        ]
        if missing:
            log.error("Missing required env vars for this mode: %s", ", ".join(missing))
            return 2

    result = _run(cfg, args)

    log.info(
        "correspondence: %d classified, %d words, handoff_found=%s",
        len(result.classified), result.word_count, result.handoff["found"],
    )
    if result.banned_word_hits:
        log.warning("BANNED WORD HITS: %s", result.banned_word_hits)
    if result.banned_transition_hits:
        log.warning("BANNED TRANSITION HITS: %s", result.banned_transition_hits)
    if result.word_count < 1500 and not args.dry_run:
        log.warning("briefing is under 1500 words (%d).", result.word_count)

    # Persist artifacts: handoff JSON + HTML body.
    json_path = cfg.correspondence_json_path()
    html_path = cfg.correspondence_html_path()
    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(
        json.dumps(result.handoff, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    html_path.write_text(result.html, encoding="utf-8")
    log.info("handoff JSON -> %s, briefing HTML -> %s", json_path, html_path)

    if args.dry_run or args.skip_send:
        log.info("[skip-send] correspondence briefing not delivered.")
        return 0

    full_date = datetime.strptime(cfg.run_date.isoformat(), "%Y-%m-%d").strftime(
        "%A, %B %-d, %Y"
    )
    subject = SUBJECT_TEMPLATE.format(full_date=full_date)
    try:
        send_html(
            to=cfg.recipient_email,
            sender=cfg.recipient_email,
            subject=subject,
            html=result.html,
            app_password=cfg.gmail_app_password,
        )
    except SMTPConfigError as e:
        log.error(str(e))
        return 4

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
