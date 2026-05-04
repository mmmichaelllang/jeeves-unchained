"""Gmail SMTP send (smtp.gmail.com:465) for Phase 3 write delivery."""

from __future__ import annotations

import logging
import os
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

# Backoff schedule for transient SMTP failures. Gmail occasionally returns 421
# "Try again later" or socket errors; the daily briefing is the product output
# so we retry generously rather than lose it.
_SMTP_RETRY_SLEEPS = (30, 60, 120)
# Permanent SMTP errors (auth, rejection by recipient, malformed message) start
# at 5xx and should NOT be retried.
_PERMANENT_SMTP_CODES = frozenset({535, 550, 551, 552, 553, 554})


class SMTPConfigError(RuntimeError):
    pass


def send_html(
    *,
    to: str,
    sender: str,
    subject: str,
    html: str,
    app_password: str | None = None,
    max_attempts: int = 4,
) -> None:
    """Send an HTML email via Gmail SMTP_SSL using an app password.

    Retries up to ``max_attempts`` times on transient failures (timeout,
    connection reset, 421 Try Again Later, socket errors). Permanent errors
    (535 auth, 550-554 rejection) are NOT retried — re-trying an auth failure
    just gets the account temporarily locked.

    Args:
        to: recipient address.
        sender: authenticated Gmail address (usually the same as `to`).
        subject: email subject line.
        html: complete HTML body.
        app_password: 16-character Gmail app password. If None, reads
            GMAIL_APP_PASSWORD from the environment.
        max_attempts: total attempts including the initial try (default 4 →
            initial + 3 retries with 30/60/120s backoff).

    Raises:
        SMTPConfigError: if no app password is available.
        smtplib.SMTPException: if all retries exhaust on a transient failure
            OR a permanent failure occurs.
    """

    pw = app_password or os.environ.get("GMAIL_APP_PASSWORD", "")
    if not pw:
        raise SMTPConfigError("GMAIL_APP_PASSWORD is required to send the briefing.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
                s.login(sender, pw)
                s.send_message(msg)
            log.info(
                "briefing sent to %s (%d bytes HTML)%s",
                to, len(html),
                f" — succeeded on attempt {attempt}" if attempt > 1 else "",
            )
            return
        except smtplib.SMTPResponseException as exc:
            # Permanent failure → don't retry.
            if exc.smtp_code in _PERMANENT_SMTP_CODES:
                log.error(
                    "SMTP permanent failure %s: %s — not retrying",
                    exc.smtp_code, exc.smtp_error,
                )
                raise
            last_exc = exc
            log.warning(
                "SMTP transient failure (attempt %d/%d) code=%s: %s",
                attempt, max_attempts, exc.smtp_code, exc.smtp_error,
            )
        except (smtplib.SMTPException, OSError, TimeoutError) as exc:
            last_exc = exc
            log.warning(
                "SMTP send failed (attempt %d/%d): %s: %s",
                attempt, max_attempts, type(exc).__name__, exc,
            )
        # Sleep before next attempt (skip after final attempt).
        if attempt < max_attempts:
            sleep_s = _SMTP_RETRY_SLEEPS[min(attempt - 1, len(_SMTP_RETRY_SLEEPS) - 1)]
            log.info("SMTP retry sleeping %ds before attempt %d", sleep_s, attempt + 1)
            time.sleep(sleep_s)

    # All attempts exhausted.
    log.error("SMTP send exhausted all %d attempts; last error: %s", max_attempts, last_exc)
    if last_exc is not None:
        raise last_exc
    raise smtplib.SMTPException("SMTP send failed after all retries")
