"""Phase 4 — Correspondence pipeline.

Flow:
  1. Gmail sweep (OAuth via google-api-python-client) → list of MessagePreview
  2. Kimi K2.5 classifies each message into one of six buckets → list of ClassifiedMessage
  3. Groq Llama 3.3 70B renders the classified inbox in Jeeves voice → HTML
  4. Persist:
     - `sessions/correspondence-<date>.json` (compact summary for research phase handoff)
     - `sessions/correspondence-<date>.html` (email body archive)
  5. Send via SMTP.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .gmail import MessagePreview

log = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CONTACTS_PATH = Path(__file__).resolve().parent / "priority_contacts.json"

CLASSIFICATIONS = [
    "reply_needed",
    "decision_required",
    "scheduling",
    "follow_up",
    "escalation",
    "no_action",
]


@dataclass
class ClassifiedMessage:
    id: str
    classification: str
    priority_contact: bool
    priority_contact_label: str | None
    summary: str
    suggested_action: str
    # echoes from the original preview
    sender: str = ""
    subject: str = ""
    date: str = ""


@dataclass
class CorrespondenceResult:
    html: str
    handoff: dict[str, Any]
    classified: list[ClassifiedMessage]
    word_count: int
    profane_aside_count: int
    banned_word_hits: list[str] = field(default_factory=list)
    banned_transition_hits: list[str] = field(default_factory=list)


def load_priority_contacts() -> dict[str, Any]:
    return json.loads(CONTACTS_PATH.read_text(encoding="utf-8"))


def classify_with_kimi(
    cfg: Config,
    previews: list[MessagePreview],
    contacts: dict[str, Any],
) -> list[ClassifiedMessage]:
    """Single Kimi call that returns a JSON array of classifications."""

    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .gmail import previews_to_classifier_input
    from .llm import build_kimi_llm

    system = (PROMPTS_DIR / "correspondence_classify.md").read_text(encoding="utf-8")
    user_json = {
        "messages": previews_to_classifier_input(previews),
        "contacts": contacts,
    }
    user = json.dumps(user_json, ensure_ascii=False)

    llm = build_kimi_llm(cfg, temperature=0.1, max_tokens=4096)
    resp = llm.chat(
        [
            ChatMessage(role=MessageRole.SYSTEM, content=system),
            ChatMessage(role=MessageRole.USER, content=user),
        ]
    )
    raw = str(resp.message.content or "").strip()
    rows = _parse_json_array(raw)

    by_id = {p.message_id: p for p in previews}
    out: list[ClassifiedMessage] = []
    for row in rows:
        mid = row.get("id", "")
        preview = by_id.get(mid)
        cls = row.get("classification", "no_action")
        if cls not in CLASSIFICATIONS:
            cls = "no_action"
        out.append(
            ClassifiedMessage(
                id=mid,
                classification=cls,
                priority_contact=bool(row.get("priority_contact")),
                priority_contact_label=row.get("priority_contact_label"),
                summary=str(row.get("summary", "")),
                suggested_action=str(row.get("suggested_action", "")),
                sender=preview.sender if preview else "",
                subject=preview.subject if preview else "",
                date=preview.date if preview else "",
            )
        )
    return out


def render_with_groq(
    cfg: Config,
    classified: list[ClassifiedMessage],
    contacts: dict[str, Any],
    *,
    run_date_iso: str,
    max_tokens: int = 4096,
) -> str:
    """Single Groq call that returns the full HTML briefing."""

    from llama_index.core.base.llms.types import ChatMessage, MessageRole

    from .llm import build_groq_llm

    system = (PROMPTS_DIR / "correspondence_write.md").read_text(encoding="utf-8")
    user_payload = {
        "date": run_date_iso,
        "contacts": contacts,
        "classified": [c.__dict__ for c in classified],
    }
    user = (
        "Here is today's classified inbox plus the priority-contacts block. "
        "Render the correspondence briefing now in Jeeves voice following every "
        "rule in the system prompt. Output HTML only, starting with <!DOCTYPE html>.\n\n"
        "```json\n"
        + json.dumps(user_payload, ensure_ascii=False, indent=2)
        + "\n```"
    )

    llm = build_groq_llm(cfg, temperature=0.65, max_tokens=max_tokens)
    resp = llm.chat(
        [
            ChatMessage(role=MessageRole.SYSTEM, content=system),
            ChatMessage(role=MessageRole.USER, content=user),
        ]
    )
    return str(resp.message.content or "")


def build_handoff_text(classified: list[ClassifiedMessage]) -> str:
    """Compact plain-text digest that the research phase will embed into
    session.correspondence.text. Capped to ~1500 chars to match schema limits."""

    if not classified:
        return ""

    order = {cls: i for i, cls in enumerate(
        ["escalation", "reply_needed", "decision_required", "scheduling", "follow_up", "no_action"]
    )}
    ordered = sorted(classified, key=lambda c: order.get(c.classification, 99))

    lines: list[str] = []
    for c in ordered:
        label = c.priority_contact_label or c.sender.split("<")[0].strip() or "unknown"
        tag = c.classification.replace("_", " ")
        lines.append(f"- [{tag}] {label}: {c.summary}")
        if len("\n".join(lines)) > 1200:
            break
    return "\n".join(lines)


def build_handoff_json(
    classified: list[ClassifiedMessage],
    fallback_used: bool,
) -> dict[str, Any]:
    """The `{found, fallback_used, text}` shape research reads at startup."""

    text = build_handoff_text(classified)
    return {
        "found": bool(classified),
        "fallback_used": fallback_used,
        "text": text,
        "counts": _counts_by_classification(classified),
    }


def _counts_by_classification(classified: list[ClassifiedMessage]) -> dict[str, int]:
    counts: dict[str, int] = {c: 0 for c in CLASSIFICATIONS}
    for item in classified:
        counts[item.classification] = counts.get(item.classification, 0) + 1
    return counts


# ---- HTML post-processing (mirrors jeeves.write structure) ----


BANNED_WORDS = ["in a vacuum", "tapestry"]
BANNED_TRANSITIONS = ["Moving on,", "Next,", "Turning to,", "In other news,"]
PROFANE_FRAGMENTS = [
    "clusterfuck", "shitshow", "fuckfest", "horse-shit", "fucked", "goddamn",
    "fuck-ton", "thundercunt", "shittery", "omnishambles", "shit-storm",
    "fucking", "cock-womble", "disaster-class", "godforsaken", "dog-shit",
    "balls-up", "train-wreck", "bollocks", "cluster-fuck", "piss-take",
    "shit-weasels", "fuck-knuckles", "horse-piss", "dog-fuckery", "shit-heap",
    "fuck-sticks", "ass-backward", "goat-fuck", "fuck-bucket", "cock-waffle",
    "shit-sandwich", "fuck-wits", "shit-show", "ass-wipe", "thundercunts",
    "fuck-parade", "shit-fountain", "fuck-trumpets", "wank-puffin",
    "fuck-pantry", "shit-tornado", "shit-cake", "knob-rot", "cock-up",
]


def postprocess_html(raw: str) -> tuple[str, int, int, list[str], list[str]]:
    """Clean model output; return (html, word_count, profane_count, banned_words, banned_transitions)."""

    html = _strip_markdown_fences(raw.strip())
    html = _ensure_doctype(html)
    body_text = _strip_tags(html)
    word_count = len(body_text.split())
    profane_count = sum(body_text.lower().count(frag) for frag in PROFANE_FRAGMENTS)
    banned_words = [w for w in BANNED_WORDS if w.lower() in body_text.lower()]
    banned_transitions = [t for t in BANNED_TRANSITIONS if t.lower() in body_text.lower()]
    return html, word_count, profane_count, banned_words, banned_transitions


def _strip_markdown_fences(s: str) -> str:
    m = re.match(r"^```(?:html)?\s*\n(.*?)\n```\s*$", s, re.DOTALL)
    return m.group(1).strip() if m else s


def _ensure_doctype(html: str) -> str:
    if html.lstrip().startswith("<!DOCTYPE"):
        return html
    m = re.search(r"<!DOCTYPE html", html, re.IGNORECASE)
    if m:
        return html[m.start():]
    return (
        "<!DOCTYPE html><html><head><meta charset=\"UTF-8\"></head><body>"
        + html
        + "</body></html>"
    )


def _strip_tags(html: str) -> str:
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL)
    no_comments = re.sub(r"<!--.*?-->", " ", no_scripts, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", no_comments)
    return re.sub(r"\s+", " ", text).strip()


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    """Parse a JSON array from model output, tolerating surrounding prose."""

    s = _strip_markdown_fences(raw)
    # Find the outer [...] span.
    start = s.find("[")
    end = s.rfind("]")
    if start < 0 or end <= start:
        log.warning("classifier output has no JSON array; returning empty.")
        return []
    try:
        parsed = json.loads(s[start : end + 1])
    except json.JSONDecodeError as e:
        log.warning("classifier JSON parse failed: %s", e)
        return []
    if not isinstance(parsed, list):
        return []
    return [row for row in parsed if isinstance(row, dict)]


# ---- Dry-run fixtures ----


def fixture_previews() -> list[MessagePreview]:
    """Canned inbox used by `scripts/correspondence.py --dry-run`."""

    return [
        MessagePreview(
            thread_id="t1", message_id="m1",
            sender="Sarah Lang <sarah@example.com>",
            to="lang.mc@gmail.com",
            subject="Tonight's dinner + Piper's storytime",
            date="Thu, 23 Apr 2026 09:00:00 -0700",
            snippet="Can you grab milk on the way home? Also storytime is at 10:30 tomorrow, I'll take her.",
            body_text="Can you grab milk on the way home? Also storytime is at 10:30 tomorrow, I'll take her.",
            unread=True,
            labels=["INBOX", "UNREAD"],
        ),
        MessagePreview(
            thread_id="t2", message_id="m2",
            sender="Northshore SD HR <hr@nsd.org>",
            to="lang.mc@gmail.com",
            subject="Interview availability — HS English position",
            date="Wed, 22 Apr 2026 16:30:00 -0700",
            snippet="We'd like to schedule a first-round interview. Please send three 45-min windows next week.",
            body_text="We'd like to schedule a first-round interview. Please send three 45-min windows next week.",
            unread=True,
            labels=["INBOX", "UNREAD"],
        ),
        MessagePreview(
            thread_id="t3", message_id="m3",
            sender="Andy Lang <andy@example.com>",
            to="lang.mc@gmail.com",
            subject="Gentle Change — Week 17",
            date="Wed, 22 Apr 2026 07:00:00 -0700",
            snippet="This week's Gentle Change newsletter: five small habits worth building.",
            body_text="This week's Gentle Change newsletter: five small habits worth building.",
            unread=False,
            labels=["INBOX"],
        ),
        MessagePreview(
            thread_id="t4", message_id="m4",
            sender="Seattle Choral Co <info@seattlechoral.org>",
            to="lang.mc@gmail.com",
            subject="Spring auditions — Sarah's slot confirmation",
            date="Tue, 21 Apr 2026 11:15:00 -0700",
            snippet="Please confirm Sarah's audition slot on May 3 at 2pm.",
            body_text="Please confirm Sarah's audition slot on May 3 at 2pm.",
            unread=False,
            labels=["INBOX"],
        ),
        MessagePreview(
            thread_id="t5", message_id="m5",
            sender="GitHub <noreply@github.com>",
            to="lang.mc@gmail.com",
            subject="[jeeves-unchained] Workflow run queued",
            date="Wed, 22 Apr 2026 13:42:00 -0700",
            snippet="Your workflow run has been queued.",
            body_text="Your workflow run has been queued.",
            unread=False,
            labels=["INBOX"],
        ),
    ]


def fixture_classified() -> list[ClassifiedMessage]:
    return [
        ClassifiedMessage(
            id="m1", classification="escalation", priority_contact=True,
            priority_contact_label="Mrs. Lang",
            summary="Sarah asks you to pick up milk and confirms Piper's storytime tomorrow at 10:30.",
            suggested_action="Reply confirming both; add storytime to calendar.",
            sender="Sarah Lang <sarah@example.com>",
            subject="Tonight's dinner + Piper's storytime",
            date="Thu, 23 Apr 2026 09:00:00 -0700",
        ),
        ClassifiedMessage(
            id="m2", classification="scheduling", priority_contact=False,
            priority_contact_label=None,
            summary="Northshore SD wants to schedule a first-round interview for the HS English role.",
            suggested_action="Send three 45-minute availability windows for next week.",
            sender="Northshore SD HR <hr@nsd.org>",
            subject="Interview availability — HS English position",
            date="Wed, 22 Apr 2026 16:30:00 -0700",
        ),
        ClassifiedMessage(
            id="m3", classification="escalation", priority_contact=True,
            priority_contact_label="Andy",
            summary="Andy sent this week's Gentle Change newsletter.",
            suggested_action="Read at leisure; reply if inclined.",
            sender="Andy Lang <andy@example.com>",
            subject="Gentle Change — Week 17",
            date="Wed, 22 Apr 2026 07:00:00 -0700",
        ),
        ClassifiedMessage(
            id="m4", classification="scheduling", priority_contact=False,
            priority_contact_label=None,
            summary="Seattle Choral Co needs confirmation of Sarah's May 3, 2pm audition slot.",
            suggested_action="Forward to Sarah, then reply to confirm.",
            sender="Seattle Choral Co <info@seattlechoral.org>",
            subject="Spring auditions — Sarah's slot confirmation",
            date="Tue, 21 Apr 2026 11:15:00 -0700",
        ),
        ClassifiedMessage(
            id="m5", classification="no_action", priority_contact=False,
            priority_contact_label=None,
            summary="GitHub workflow notification — informational.",
            suggested_action="",
            sender="GitHub <noreply@github.com>",
            subject="[jeeves-unchained] Workflow run queued",
            date="Wed, 22 Apr 2026 13:42:00 -0700",
        ),
    ]


def render_mock_correspondence(run_date_iso: str, classified: list[ClassifiedMessage]) -> str:
    """Dry-run placeholder HTML that exercises post-processing."""

    items_html = "".join(
        f"<li><strong>{c.classification}</strong> — {c.sender}: {c.summary}</li>"
        for c in classified
    )
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><style>body {{font-family:Georgia,serif;max-width:720px;margin:0 auto;padding:20px;background:#faf9f6;color:#1a1a1a;}}</style></head>
<body>
<h1>📫 Correspondence — {run_date_iso} (DRY RUN)</h1>
<h2>Today's Action Summary</h2>
<p>A clusterfuck of biblical proportions, Sir — I do beg your pardon, a busy morning. A total and utter shitshow of priorities. An absolute fuckfest of incompetence at the district office, pardon me. Do excuse the language; it has been a steaming pile of horse-shit of a commute.</p>
<h2>Priority Correspondence</h2>
<p>A note from your dear wife, Sir. Your brother has written. A colossal goddamn mess, pardon me. Fucking disaster-class, I'm afraid — ahem.</p>
<h2>Electronic Mail (Gmail)</h2>
<ul>{items_html}</ul>
<p class="closing">Your reluctantly faithful Butler,<br>Jeeves</p>
</body>
</html>"""
