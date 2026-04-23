"""Gmail SMTP send (smtp.gmail.com:465) for Phase 3 write delivery."""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger(__name__)

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class SMTPConfigError(RuntimeError):
    pass


def send_html(
    *,
    to: str,
    sender: str,
    subject: str,
    html: str,
    app_password: str | None = None,
) -> None:
    """Send an HTML email via Gmail SMTP_SSL using an app password.

    Args:
        to: recipient address.
        sender: authenticated Gmail address (usually the same as `to`).
        subject: email subject line.
        html: complete HTML body.
        app_password: 16-character Gmail app password. If None, reads
            GMAIL_APP_PASSWORD from the environment.

    Raises:
        SMTPConfigError: if no app password is available.
    """

    pw = app_password or os.environ.get("GMAIL_APP_PASSWORD", "")
    if not pw:
        raise SMTPConfigError("GMAIL_APP_PASSWORD is required to send the briefing.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.attach(MIMEText(html, "html", "utf-8"))

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as s:
        s.login(sender, pw)
        s.send_message(msg)
    log.info("briefing sent to %s (%d bytes HTML)", to, len(html))
