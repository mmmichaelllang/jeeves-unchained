"""Gmail API client — OAuth credential refresh + thread sweep.

Gmail OAuth is bootstrapped ONCE, locally, via `scripts/gmail_auth.py`.
That script runs the InstalledAppFlow, writes `token.json` with a
refresh token, and prints instructions to paste its contents into the
GitHub secret `GMAIL_OAUTH_TOKEN_JSON`. At runtime, every invocation
refreshes the access token from the stored refresh token — no human
interaction required.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


@dataclass
class MessagePreview:
    """Lightweight message shape used for classification + the briefing."""

    thread_id: str
    message_id: str
    sender: str
    to: str
    subject: str
    date: str
    snippet: str
    body_text: str
    unread: bool
    labels: list[str] = field(default_factory=list)


def build_gmail_service(token_json: str):
    """Construct a Gmail v1 service from a serialized OAuth token payload.

    The payload is produced by `scripts/gmail_auth.py` and must include
    `refresh_token`, `token_uri`, `client_id`, `client_secret`, `scopes`.
    """

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    info = json.loads(token_json)
    creds = Credentials.from_authorized_user_info(info, scopes=GMAIL_SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            raise RuntimeError(
                "Gmail OAuth token is invalid and cannot be refreshed. "
                "Re-run scripts/gmail_auth.py locally to mint a new token."
            )
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def list_message_ids(service, query: str, max_results: int = 150) -> list[dict[str, str]]:
    """Return [{id, threadId}] for messages matching `query` (Gmail search syntax)."""

    out: list[dict[str, str]] = []
    next_page: str | None = None
    fetched = 0
    while fetched < max_results:
        req = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(100, max_results - fetched),
            pageToken=next_page,
        )
        resp = req.execute()
        batch = resp.get("messages", []) or []
        out.extend(batch)
        fetched += len(batch)
        next_page = resp.get("nextPageToken")
        if not next_page:
            break
    return out


def fetch_message(service, message_id: str) -> MessagePreview:
    """Fetch a single message and normalize into a MessagePreview."""

    msg = service.users().messages().get(
        userId="me", id=message_id, format="full",
    ).execute()

    headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = _extract_body_text(msg.get("payload", {}))
    labels = msg.get("labelIds", []) or []

    return MessagePreview(
        thread_id=msg.get("threadId", ""),
        message_id=msg.get("id", ""),
        sender=headers.get("from", ""),
        to=headers.get("to", ""),
        subject=headers.get("subject", "(no subject)"),
        date=headers.get("date", ""),
        snippet=msg.get("snippet", "") or "",
        body_text=body[:3000],
        unread="UNREAD" in labels,
        labels=labels,
    )


def _extract_body_text(payload: dict[str, Any]) -> str:
    """Walk MIME parts, prefer text/plain, fall back to text/html (stripped)."""

    if not payload:
        return ""
    mime = payload.get("mimeType", "")
    body = payload.get("body") or {}
    data = body.get("data")

    if data and mime == "text/plain":
        return _decode(data)
    if data and mime == "text/html":
        return _strip_tags(_decode(data))

    # multipart — recurse
    for part in payload.get("parts", []) or []:
        text = _extract_body_text(part)
        if text:
            return text
    if data:
        return _decode(data)
    return ""


def _decode(b64url: str) -> str:
    pad = "=" * (-len(b64url) % 4)
    try:
        return base64.urlsafe_b64decode(b64url + pad).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _strip_tags(html: str) -> str:
    import re

    no_script = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", no_script)
    return re.sub(r"\s+", " ", text).strip()


def sweep_recent(
    service,
    *,
    days: int = 60,
    max_results: int = 50,
) -> list[MessagePreview]:
    """Sweep unread messages from the last `days` days, excluding spam/promotions.

    The briefing is a daily triage of what's new — already-read mail has been
    handled. Using `is:unread` also keeps the Kimi classify payload and the
    Groq render payload bounded on busy accounts.
    """

    query = f"is:unread newer_than:{days}d -label:spam -label:promotions"
    ids = list_message_ids(service, query, max_results=max_results)

    seen: set[str] = set()
    previews: list[MessagePreview] = []
    for m in ids:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        try:
            previews.append(fetch_message(service, mid))
        except Exception as e:
            log.warning("failed to fetch message %s: %s", mid, e)
    log.info("gmail sweep (unread-only): %d messages", len(previews))
    return previews


def previews_to_classifier_input(previews: list[MessagePreview]) -> list[dict[str, Any]]:
    """Compact JSON shape passed to Kimi for classification."""

    return [
        {
            "id": p.message_id,
            "thread_id": p.thread_id,
            "sender": p.sender,
            "subject": p.subject,
            "date": p.date,
            "unread": p.unread,
            "snippet": (p.snippet or p.body_text)[:600],
        }
        for p in previews
    ]


