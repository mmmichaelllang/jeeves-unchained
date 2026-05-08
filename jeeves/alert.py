"""Out-of-band alert email — used when the main pipeline cannot run.

The daily briefing flows through Gmail OAuth (read) + Gmail SMTP app password
(send). When OAuth dies (refresh-token revoked or expired in OAuth-consent
"Testing" mode), the pipeline crashes silently in CI — the user just sees no
newsletter. App-password SMTP is a SEPARATE credential that does not depend on
OAuth, so we use it as the fallback alert channel.

Use this module from preflight checks (``scripts/check_gmail_oauth.py``) and
from the auto-retry classifier when a failure is permanent (auth, import,
secret-missing) and retrying will not help.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from jeeves.email import SMTPConfigError, send_html

log = logging.getLogger(__name__)


def _utc_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def send_failure_alert(
    *,
    subject: str,
    reason: str,
    details: str = "",
    remediation: str = "",
    recipient: str | None = None,
    app_password: str | None = None,
) -> bool:
    """Send a plain-text-as-HTML alert about a pipeline-level failure.

    Returns True on success, False on send failure or missing app password —
    callers should not crash if the alert itself cannot be sent (this is the
    fallback channel, so it must fail-soft).

    Args:
        subject: short subject line. ``[jeeves alert]`` prefix is added.
        reason: one-line cause (e.g. "Gmail OAuth refresh token revoked").
        details: multi-line context (stack trace, log excerpt, etc.).
        remediation: short numbered list of recovery steps.
        recipient: defaults to ``JEEVES_RECIPIENT_EMAIL`` env or
            ``lang.mc@gmail.com``.
        app_password: defaults to ``GMAIL_APP_PASSWORD`` env.
    """

    pw = app_password or os.environ.get("GMAIL_APP_PASSWORD", "")
    if not pw:
        log.error("alert: no GMAIL_APP_PASSWORD — cannot send out-of-band alert")
        return False

    to = recipient or os.environ.get("JEEVES_RECIPIENT_EMAIL", "lang.mc@gmail.com")
    full_subject = f"[jeeves alert] {subject}"
    body = _render_alert_html(
        reason=reason,
        details=details,
        remediation=remediation,
    )
    try:
        send_html(
            to=to,
            sender=to,
            subject=full_subject,
            html=body,
            app_password=pw,
            max_attempts=2,  # alerts are best-effort; do not stall CI on retries
        )
        log.info("alert: out-of-band failure email sent to %s", to)
        return True
    except Exception as exc:
        log.error("alert: failed to send out-of-band email: %s: %s",
                  type(exc).__name__, exc)
        return False


def _render_alert_html(*, reason: str, details: str, remediation: str) -> str:
    stamp = _utc_stamp()
    detail_block = (
        f"<pre style=\"background:#f4f4f4;padding:10px;border-left:3px solid #c00;"
        f"font-family:monospace;font-size:12px;white-space:pre-wrap;\">{_escape(details)}</pre>"
        if details else ""
    )
    remediation_block = (
        f"<h3>Remediation</h3><pre style=\"font-family:monospace;font-size:13px;"
        f"white-space:pre-wrap;\">{_escape(remediation)}</pre>"
        if remediation else ""
    )
    return (
        "<html><body style=\"font-family:system-ui,sans-serif;max-width:680px;\">"
        f"<h2 style=\"color:#c00;\">Jeeves pipeline alert</h2>"
        f"<p><strong>{_escape(reason)}</strong></p>"
        f"<p style=\"color:#666;font-size:13px;\">Detected: {stamp}</p>"
        f"{detail_block}{remediation_block}"
        "</body></html>"
    )


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
