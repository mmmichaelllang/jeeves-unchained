#!/usr/bin/env python3
"""Preflight Gmail OAuth check.

Validates the OAuth refresh token in ``GMAIL_OAUTH_TOKEN_JSON`` BEFORE the
correspondence pipeline starts. On a permanent failure (``invalid_grant`` —
revoked or expired refresh token), exits with code 2 and sends an out-of-band
alert email via ``GMAIL_APP_PASSWORD`` (a separate credential that does not
depend on OAuth).

Why this exists: Google OAuth refresh tokens issued under "Testing" consent
status expire after 7 days. Until 2026-05-08 the failure mode was silent — the
correspondence step crashed in 1s, research/write skipped, no newsletter, no
visible alert. This script makes the failure loud and actionable.

Exit codes:
    0  refresh token works (access token minted successfully)
    2  invalid_grant — refresh token revoked or expired (PERMANENT)
    3  other auth error (TRANSIENT — Google API down, network, etc.)
    4  missing env vars (treat as broken-ish; don't retry blindly)

Usage:
    python scripts/check_gmail_oauth.py                # exit 0/2/3/4
    python scripts/check_gmail_oauth.py --no-alert     # skip alert email
    python scripts/check_gmail_oauth.py --quiet        # exit codes only
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

EXIT_OK = 0
EXIT_INVALID_GRANT = 2
EXIT_OTHER_AUTH = 3
EXIT_MISSING_ENV = 4

REMEDIATION_INVALID_GRANT = """\
1. Mint a fresh OAuth token locally:
       python scripts/gmail_auth.py --credentials ~/Downloads/credentials.json
2. Copy contents of token.json into the GitHub secret GMAIL_OAUTH_TOKEN_JSON.
3. Verify the OAuth consent screen is set to "In production" in Google Cloud
   Console (APIs & Services -> OAuth consent screen). "Testing" mode expires
   refresh tokens after 7 days.
4. Trigger daily.yml manually with workflow_dispatch."""

REMEDIATION_OTHER = """\
This may be a transient Google API outage. Auto-retry will fire shortly.
If it persists for >30 minutes, mint a fresh token via scripts/gmail_auth.py
and update GMAIL_OAUTH_TOKEN_JSON."""


def _log() -> logging.Logger:
    return logging.getLogger("jeeves.preflight.gmail")


def _classify_refresh_error(exc: Exception) -> int:
    """Map a refresh-failure exception to one of our exit codes.

    Google's library raises ``google.auth.exceptions.RefreshError`` whose
    message starts with ``invalid_grant: ...`` for permanent failures. Network
    errors raise ``TransportError`` or stdlib ``ConnectionError``.
    """
    msg = str(exc)
    if "invalid_grant" in msg:
        return EXIT_INVALID_GRANT
    return EXIT_OTHER_AUTH


def check_token(token_json: str) -> tuple[int, str]:
    """Try a refresh. Returns (exit_code, human_message)."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        return (
            EXIT_MISSING_ENV,
            f"google-auth not installed in this env: {exc}",
        )

    try:
        info = json.loads(token_json)
    except json.JSONDecodeError as exc:
        return (
            EXIT_MISSING_ENV,
            f"GMAIL_OAUTH_TOKEN_JSON is not valid JSON: {exc}",
        )

    scopes = info.get("scopes") or ["https://www.googleapis.com/auth/gmail.readonly"]
    try:
        creds = Credentials.from_authorized_user_info(info, scopes=scopes)
    except Exception as exc:
        return (
            EXIT_MISSING_ENV,
            f"Could not construct Credentials from token JSON: "
            f"{type(exc).__name__}: {exc}",
        )

    # If the access token is still valid, no refresh attempt is made — but we
    # WANT to exercise the refresh path to catch a revoked refresh-token early.
    # Force refresh by clearing the access token.
    creds.token = None
    try:
        creds.refresh(Request())
    except Exception as exc:
        code = _classify_refresh_error(exc)
        return code, f"{type(exc).__name__}: {exc}"

    if not creds.token:
        return EXIT_OTHER_AUTH, "refresh succeeded but no access token returned"
    return EXIT_OK, "refresh ok"


def _send_alert(*, exit_code: int, message: str) -> bool:
    try:
        from jeeves.alert import send_failure_alert
    except ImportError as exc:
        _log().error("could not import jeeves.alert: %s", exc)
        return False

    if exit_code == EXIT_INVALID_GRANT:
        return send_failure_alert(
            subject="Gmail OAuth refresh token revoked",
            reason="Gmail OAuth refresh token is invalid_grant — pipeline cannot run.",
            details=message,
            remediation=REMEDIATION_INVALID_GRANT,
        )
    if exit_code == EXIT_OTHER_AUTH:
        return send_failure_alert(
            subject="Gmail OAuth refresh failed (transient)",
            reason="Gmail OAuth refresh failed for a non-permanent reason.",
            details=message,
            remediation=REMEDIATION_OTHER,
        )
    if exit_code == EXIT_MISSING_ENV:
        return send_failure_alert(
            subject="Gmail OAuth preflight misconfigured",
            reason="Could not even attempt a refresh — env or token JSON broken.",
            details=message,
            remediation="Check GMAIL_OAUTH_TOKEN_JSON secret is set and valid JSON.",
        )
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Preflight Gmail OAuth check.")
    ap.add_argument("--no-alert", action="store_true",
                    help="Do not send an alert email even on failure.")
    ap.add_argument("--quiet", action="store_true",
                    help="Suppress non-error logs.")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    log = _log()

    token_json = os.environ.get("GMAIL_OAUTH_TOKEN_JSON", "")
    if not token_json:
        log.error("GMAIL_OAUTH_TOKEN_JSON is not set in the environment")
        if not args.no_alert:
            _send_alert(
                exit_code=EXIT_MISSING_ENV,
                message="GMAIL_OAUTH_TOKEN_JSON env var is empty.",
            )
        return EXIT_MISSING_ENV

    code, msg = check_token(token_json)
    if code == EXIT_OK:
        log.info("gmail oauth preflight ok")
        return 0

    log.error("gmail oauth preflight failed (exit=%d): %s", code, msg)
    if not args.no_alert:
        sent = _send_alert(exit_code=code, message=msg)
        log.info("alert email %s", "sent" if sent else "NOT sent")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
