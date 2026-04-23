#!/usr/bin/env python3
"""Phase 3 — Write script.

Loads a session JSON, generates a Jeeves-voice HTML briefing via Groq
Llama 3.3 70B, and sends it to the recipient via Gmail SMTP.

Usage:
  python scripts/write.py --date 2026-04-23
  python scripts/write.py --dry-run                  # fixture HTML, no SMTP
  python scripts/write.py --skip-send                # real Groq, no SMTP
  python scripts/write.py --plan-only                # summary only, no model call
  python scripts/write.py --use-fixture --skip-send  # smoke test (real Groq, canned session)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from jeeves.config import Config, MissingSecret  # noqa: E402
from jeeves.email import SMTPConfigError, send_html  # noqa: E402
from jeeves.session_io import load_session_by_date  # noqa: E402
from jeeves.write import (  # noqa: E402
    generate_briefing,
    postprocess_html,
    render_mock_briefing,
)

log = logging.getLogger("jeeves.write")

SUBJECT_TEMPLATE = "📜 Daily Intelligence from Jeeves — {full_date}"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Jeeves write phase (Phase 3).")
    p.add_argument("--date", default=None, help="Session date (YYYY-MM-DD). Defaults to today UTC.")
    p.add_argument("--dry-run", action="store_true", help="Fixture HTML only; no model call or SMTP.")
    p.add_argument("--skip-send", action="store_true", help="Generate HTML but do not send email.")
    p.add_argument("--plan-only", action="store_true", help="Print session sector summary and exit.")
    p.add_argument(
        "--use-fixture",
        action="store_true",
        help="Skip loading a real session; use the canned mock payload from jeeves.testing.mocks. "
             "Useful for smoke-testing the Groq + SMTP path without running research first.",
    )
    p.add_argument(
        "--max-tokens",
        type=int,
        default=8192,
        help="Groq max_completion_tokens (default 8192 — model cap).",
    )
    p.add_argument("--verbose", action="store_true")
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
    print(f"Weather: {(session.weather or '')[:80]}")
    print(f"New Yorker available: {session.newyorker.available}")
    print(f"Vault insight available: {session.vault_insight.available}")


def _commit_coverage(cfg: Config, briefing_path: Path) -> None:
    """Commit the rendered briefing HTML back to GitHub so it's archived.

    Uses `git` CLI because the runner already has the checkout and GITHUB_TOKEN
    wired. Safe to skip silently if git isn't available or auth fails —
    the email has still been delivered.
    """

    try:
        import os

        env = os.environ.copy()
        subprocess.run(
            ["git", "config", "user.name", "jeeves-bot"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "jeeves-bot@users.noreply.github.com"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "add", str(briefing_path.relative_to(cfg.repo_root))],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", f"briefing {cfg.run_date.isoformat()}"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "push"],
            cwd=cfg.repo_root, env=env, check=True, capture_output=True,
        )
        log.info("briefing archived to %s", briefing_path)
    except subprocess.CalledProcessError as e:
        log.warning("failed to archive briefing (non-fatal): %s", e.stderr.decode(errors="replace")[:300])


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    # `dry_run` loosens env validation — treat --skip-send, --plan-only, and
    # --use-fixture the same way. Real Groq is still called for --skip-send and
    # --use-fixture, so GROQ_API_KEY must be present for those (enforced below).
    loosen_env = args.dry_run or args.skip_send or args.plan_only or args.use_fixture
    try:
        cfg = Config.from_env(
            phase="write",
            dry_run=loosen_env,
            run_date=args.date,
            verbose=args.verbose,
        )
    except MissingSecret as e:
        log.error(str(e))
        return 2

    # --skip-send and --use-fixture both invoke Groq; ensure GROQ_API_KEY is set.
    if (args.skip_send or args.use_fixture) and not args.dry_run and not cfg.groq_api_key:
        log.error("GROQ_API_KEY is required unless --dry-run is set.")
        return 2

    if args.use_fixture:
        from jeeves.schema import SessionModel
        from jeeves.testing.mocks import canned_session

        log.info("--use-fixture: loading canned mock session (no real session JSON needed).")
        session = SessionModel.model_validate(canned_session(cfg.run_date))
    else:
        try:
            session = load_session_by_date(cfg, cfg.run_date)
        except FileNotFoundError:
            log.error("No session file found for %s", cfg.run_date.isoformat())
            return 3

    if args.plan_only:
        _plan_only(session)
        return 0

    if args.dry_run:
        raw_html = render_mock_briefing(session)
        log.info("dry-run fixture briefing assembled (%d chars)", len(raw_html))
    else:
        raw_html = generate_briefing(cfg, session, max_tokens=args.max_tokens)

    result = postprocess_html(raw_html, session)
    log.info(
        "briefing: %d words, %d profane asides, %d coverage entries",
        result.word_count, result.profane_aside_count, len(result.coverage_log),
    )
    if result.banned_word_hits:
        log.warning("BANNED WORD HITS: %s", result.banned_word_hits)
    if result.banned_transition_hits:
        log.warning("BANNED TRANSITION HITS: %s", result.banned_transition_hits)
    if result.word_count < 5000 and not args.dry_run:
        log.warning("briefing is under 5000 words (%d) — model undershot.", result.word_count)
    if result.profane_aside_count < 5 and not args.dry_run:
        log.warning("fewer than 5 profane asides detected (%d).", result.profane_aside_count)

    out_path = cfg.briefing_html_path()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.html, encoding="utf-8")
    log.info("briefing written to %s", out_path)

    if args.dry_run or args.skip_send:
        log.info("[skip-send] briefing not delivered.")
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

    _commit_coverage(cfg, out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
