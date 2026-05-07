#!/usr/bin/env python3
"""Phase 5 — Auditor (PR2: fix actions).

Reads sessions/audit-<date>.json + sessions/briefing-<date>.html, applies
per-defect fixes, writes the corrected briefing to the same path. Logs a
fix record at sessions/audit-fix-<date>.json.

Fixes (priority order):

    F1 strip_hallucinated_url      strip <a> tag, keep prose
    F2 reorder_sections            rebuild canonical order
    F3 dedup_within_run            strip duplicate URLs across sections
    F4 strip_repeated_asides       keep first occurrence, drop the rest
    F5 inject_empty_feed           h3 + canonical empty-feed paragraph
    F6 rerender_empty_with_data    LLM-rewrite the part with explicit data
    F7 rerender_greeting           LLM-rewrite Part 1 with weather + correspondence + date
    F8 rewrite_low_quality_section LLM-rewrite when D9 score < 7
    F9 polish_narrative_flow       LLM-edit when D7 score < 7

LLM-backed fixes (F6-F9) use ``jeeves.audit_models.resolve_audit_models``
— reasoning-first free-tier picker. Fail-soft: any LLM failure leaves the
section untouched and logs a fix_skipped record.

Usage:
    python scripts/audit_fix.py --date 2026-05-06
    python scripts/audit_fix.py --date 2026-05-06 --no-llm    # only F1-F5
    python scripts/audit_fix.py --date 2026-05-06 --dry-run   # don't write
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

log = logging.getLogger("audit_fix")


# Re-use canonical structure from audit.py.
from audit import (  # noqa: E402
    EXPECTED_H3_ORDER,
    aggregate_session_urls,
    extract_h3_sequence,
    extract_section_blocks,
)

_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_HREF_TAG_RE = re.compile(
    r"<a[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Fix log
# ---------------------------------------------------------------------------


@dataclass
class FixAction:
    type: str
    section: str | None
    detail: str
    status: str  # "applied" | "skipped" | "failed"
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class FixReport:
    date: str
    pre_fix_chars: int
    post_fix_chars: int
    actions: list[FixAction]
    pre_fix_defect_count: int
    post_fix_defect_count: int | None = None
    audit_model_used: str | None = None


# ---------------------------------------------------------------------------
# F1 — strip hallucinated URLs
# ---------------------------------------------------------------------------


def fix_hallucinated_urls(html: str, defects: list[dict],
                          actions: list[FixAction]) -> str:
    """Replace <a href="bad">text</a> with just `text` for each hallucinated URL."""
    bad_urls = {
        d["evidence"]["url"]
        for d in defects
        if d["type"] == "hallucinated_url" and d.get("evidence", {}).get("url")
    }
    if not bad_urls:
        return html

    def _replace(m: re.Match) -> str:
        href = m.group(1).strip()
        inner = m.group(2)
        if href in bad_urls:
            actions.append(FixAction(
                type="strip_hallucinated_url",
                section=None,
                detail=f"stripped <a href={href!r}>",
                status="applied",
                evidence={"url": href},
            ))
            return inner
        return m.group(0)

    return _HREF_TAG_RE.sub(_replace, html)


# ---------------------------------------------------------------------------
# F2 — reorder sections to canonical PART_PLAN order
# ---------------------------------------------------------------------------


def fix_section_order(html: str, defects: list[dict],
                      actions: list[FixAction]) -> str:
    has_order_defect = any(d["type"] == "section_order" for d in defects)
    if not has_order_defect:
        return html

    actual = extract_h3_sequence(html)
    canonical_in_doc = [h for h in EXPECTED_H3_ORDER if h in actual]
    if [h for h in actual if h in EXPECTED_H3_ORDER] == canonical_in_doc:
        return html

    # Find the prelude (everything before first canonical h3).
    matches = list(_H3_RE.finditer(html))
    canonical_match_starts = [
        m.start() for m in matches
        if _TAG_RE.sub("", m.group(1)).strip() in EXPECTED_H3_ORDER
    ]
    if not canonical_match_starts:
        return html
    prelude_end = canonical_match_starts[0]
    prelude = html[:prelude_end]

    # Collect each canonical h3 block in document order.
    blocks: dict[str, str] = {}
    for i, m in enumerate(matches):
        h3 = _TAG_RE.sub("", m.group(1)).strip()
        if h3 not in EXPECTED_H3_ORDER:
            continue
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        # Stop before a final closing </body> or </html>.
        block = html[m.start():end]
        # Strip trailing </body></html> from the very last block (we re-add).
        block = re.sub(r"\s*</body\s*>\s*$", "", block, flags=re.IGNORECASE)
        block = re.sub(r"\s*</html\s*>\s*$", "", block, flags=re.IGNORECASE)
        blocks[h3] = block

    # Reassemble in canonical order. Drop closing tags from prelude, re-add at end.
    prelude = re.sub(r"\s*</body\s*>\s*$", "", prelude, flags=re.IGNORECASE)
    prelude = re.sub(r"\s*</html\s*>\s*$", "", prelude, flags=re.IGNORECASE)
    rebuilt = prelude
    for h3 in EXPECTED_H3_ORDER:
        if h3 in blocks:
            rebuilt += "\n" + blocks[h3]
    # Pull any post-h3 trailers (signoff div, COVERAGE_LOG comment, </body>).
    last_canonical_end = max(
        (m.end() for m in matches if _TAG_RE.sub("", m.group(1)).strip() in EXPECTED_H3_ORDER),
        default=0,
    )
    # Keep everything after the last canonical block that's a comment/signoff.
    tail = ""
    if last_canonical_end:
        # Find the last } closer in original html that ends an EXPECTED_H3_ORDER block
        # — already absorbed; just append signoff + close tags.
        signoff = re.search(
            r'<div[^>]*class="signoff"[^>]*>.*?</div>',
            html, re.IGNORECASE | re.DOTALL,
        )
        if signoff:
            tail += "\n" + signoff.group(0)
        coverage = re.search(r"<!--\s*COVERAGE_LOG.*?-->", html, re.DOTALL)
        if coverage:
            tail += "\n" + coverage.group(0)
    rebuilt += tail + "\n</body>\n</html>"
    actions.append(FixAction(
        type="reorder_sections",
        section=None,
        detail=f"rebuilt h3 order: actual={actual} → canonical={list(blocks.keys())}",
        status="applied",
        evidence={"actual": actual, "rebuilt": list(blocks.keys())},
    ))
    return rebuilt


# ---------------------------------------------------------------------------
# F3 — dedup within-run cross-section URLs
# ---------------------------------------------------------------------------


def fix_dedup_within_run(html: str, defects: list[dict],
                         actions: list[FixAction]) -> str:
    cross_defects = [d for d in defects if d["type"] == "dedup_url_cross_section"]
    if not cross_defects:
        return html
    seen: set[str] = set()

    def _replace(m: re.Match) -> str:
        href = m.group(1).strip()
        inner = m.group(2)
        if href in seen:
            actions.append(FixAction(
                type="dedup_within_run_strip",
                section=None,
                detail=f"stripped duplicate <a href={href!r}>",
                status="applied",
                evidence={"url": href},
            ))
            return inner
        seen.add(href)
        return m.group(0)

    return _HREF_TAG_RE.sub(_replace, html)


# ---------------------------------------------------------------------------
# F4 — strip repeated aside templates
# ---------------------------------------------------------------------------


_ASIDE_RE = re.compile(
    r"(?i)\ba\s+(?:proper|right|total|absolute|utter|complete)[^.]{0,80}"
    r"(?:shambles|shitshow|shit-show|clusterfuck|shit-tornado|"
    r"shit-storm|fuckup|fuck-up|fucking\s+\w+|deep-fried[^.]{0,30}|"
    r"gold-plated[^.]{0,30}|bespoke[^.]{0,30}|hand-crafted[^.]{0,30})"
    r"[^.]*?\."
)


def fix_aside_repetition(html: str, defects: list[dict],
                         actions: list[FixAction]) -> str:
    has_aside_defect = any(
        d["type"] in ("aside_repetition", "aside_overuse") for d in defects
    )
    if not has_aside_defect:
        return html

    # Track templates seen; strip any 2nd+ occurrence of an identical template.
    seen: dict[str, int] = {}
    out_parts: list[str] = []
    last_end = 0
    for m in _ASIDE_RE.finditer(html):
        template = m.group(0).strip().lower()
        # Normalize template by collapsing whitespace.
        norm = re.sub(r"\s+", " ", template)
        seen[norm] = seen.get(norm, 0) + 1
        if seen[norm] >= 2:
            # Strip — just keep text BEFORE this aside, including a trailing space.
            out_parts.append(html[last_end:m.start()])
            actions.append(FixAction(
                type="strip_repeated_aside",
                section=None,
                detail=f"stripped repeat #{seen[norm]} of aside",
                status="applied",
                evidence={"template": norm[:120]},
            ))
            last_end = m.end()
    out_parts.append(html[last_end:])
    return "".join(out_parts)


# ---------------------------------------------------------------------------
# F5 — inject empty-feed paragraph for missing sections
# ---------------------------------------------------------------------------


_EMPTY_FEED_DEFAULT = "<p>The wires are quiet on this front this morning, Sir.</p>"

_SECTION_EMPTY_FEED = {
    "The Domestic Sphere": (
        "<p>The local feed is quiet this morning, Sir — nothing within "
        "the geofence that rises to the level of a briefing item.</p>"
    ),
    "Beyond the Geofence": (
        "<p>The global wires are quiet this morning, Sir — nothing of "
        "sufficient substance to detain us.</p>"
    ),
    "The Reading Room": (
        "<p>The intellectual journals offer nothing fresh this morning, "
        "Sir. Tomorrow we shall resume.</p>"
    ),
    "The Specific Enquiries": (
        "<p>No movement on triadic ontology, AI systems, or UAP "
        "disclosure overnight, Sir.</p>"
    ),
    "The Commercial Ledger": (
        "<p>The commercial AI ledger is quiet this morning, Sir.</p>"
    ),
    "The Library Stacks": _EMPTY_FEED_DEFAULT,
    "Talk of the Town": _EMPTY_FEED_DEFAULT,
}


def fix_missing_sections(html: str, defects: list[dict], session: dict,
                         actions: list[FixAction]) -> str:
    missing = [d for d in defects if d["type"] == "missing_section"
               and d.get("section")]
    if not missing:
        return html
    # Inject in canonical position. We re-walk: for each canonical h3 not in
    # the doc, splice in the h3 + empty-feed paragraph at the right slot.
    h3_seq = extract_h3_sequence(html)
    for d in missing:
        target = d["section"]
        if target in h3_seq:
            continue
        # Find the closest previous canonical h3 that IS in the doc.
        try:
            target_idx = EXPECTED_H3_ORDER.index(target)
        except ValueError:
            continue
        anchor: str | None = None
        for prev in EXPECTED_H3_ORDER[:target_idx][::-1]:
            if prev in h3_seq:
                anchor = prev
                break
        block = f"\n<h3>{target}</h3>\n{_SECTION_EMPTY_FEED.get(target, _EMPTY_FEED_DEFAULT)}\n"
        if anchor:
            # Splice after the end of the anchor block (just before next h3 or EOF).
            anchor_match = re.search(
                rf"<h3[^>]*>{re.escape(anchor)}</h3>",
                html, re.IGNORECASE,
            )
            if anchor_match:
                # Find the next h3 after anchor.
                next_h3 = _H3_RE.search(html, anchor_match.end())
                splice_at = next_h3.start() if next_h3 else html.find("</body>")
                if splice_at == -1:
                    splice_at = len(html)
                html = html[:splice_at] + block + html[splice_at:]
                actions.append(FixAction(
                    type="inject_missing_section",
                    section=target,
                    detail=f"injected empty-feed for {target}",
                    status="applied",
                    evidence={"anchor": anchor},
                ))
                h3_seq = extract_h3_sequence(html)
        else:
            # No anchor — splice at the very top after first h1/p.
            first_h3 = _H3_RE.search(html)
            splice_at = first_h3.start() if first_h3 else html.find("</body>")
            if splice_at == -1:
                splice_at = len(html)
            html = html[:splice_at] + block + html[splice_at:]
            actions.append(FixAction(
                type="inject_missing_section",
                section=target,
                detail=f"injected empty-feed for {target} at top",
                status="applied",
                evidence={"anchor": None},
            ))
            h3_seq = extract_h3_sequence(html)
    return html


# ---------------------------------------------------------------------------
# LLM-backed fixes (F6 - F9). Optional — gated by --no-llm.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F-001 — output validator for LLM-backed fixes (F6-F9).
# Reasoning models (nemotron, deepseek-r1, openai-o1) emit chain-of-thought
# on a non-trivial percentage of calls. fix_empty_with_data previously
# spliced ``text`` verbatim with only a falsy guard, leading to the
# 2026-05-06 briefing leaking nemotron's planning prose into Talk of the
# Town. This validator gates every fix path that splices model output.
# ---------------------------------------------------------------------------

# First non-whitespace token must be a block-level HTML opener.
_AUDIT_OUT_FIRST_TAG_RE = re.compile(r"^\s*<(p|div|h[1-6]|section|article)\b", re.IGNORECASE)

# Strip HTML tags for word counting. Crude but sufficient — we only need
# to bracket the count, not parse semantically.
_AUDIT_OUT_TAG_STRIP_RE = re.compile(r"<[^>]+>")

# CoT markers — phrases reasoning models emit when narrating their plan.
# Case-insensitive substring match. Defense-in-depth: catches HTML-then-CoT
# (case 3 in the F-001 plan), which the first-tag check misses.
_AUDIT_OUT_COT_MARKERS = (
    "we need to produce",
    "word count:",
    "word count target",
    "let me start",
    "let me think",
    "let me check",
    "let me adjust",
    "let's count",
    "step 1:",
    "step 2:",
    "first, i'll",
    "first i'll",
    "i'll write",
    "i'll draft",
    "i need to",
    "the user wants",
    "the user is asking",
    "okay, so",
    "okay so the",
    "<think>",
    "</think>",
)


def _validate_audit_model_output(
    text: str,
    *,
    expect_html_paragraph: bool = True,
    min_words: int = 30,
    max_words: int = 400,
) -> tuple[bool, str]:
    """Reject chain-of-thought leaks, fragments, and oversized output.

    Returns (ok, reason). ok=True means the text is safe to splice into
    the briefing. ok=False means the caller should append a failed
    FixAction and skip the splice.

    Rules (in order):

    1. Strip whitespace; reject empty.
    2. If expect_html_paragraph: first non-whitespace token must open a
       block-level HTML tag (<p>, <div>, <h2>-<h6>, <section>, <article>).
    3. Word count (post tag-strip) must be in [min_words, max_words].
    4. Defense-in-depth: scan full text against curated CoT markers.
    """
    if not text or not text.strip():
        return (False, "empty after strip")

    stripped = text.strip()

    if expect_html_paragraph and not _AUDIT_OUT_FIRST_TAG_RE.match(stripped):
        preview = stripped[:60].replace("\n", " ")
        return (False, f"non-html prefix: {preview!r}")

    plain = _AUDIT_OUT_TAG_STRIP_RE.sub(" ", stripped)
    words = [w for w in plain.split() if w.strip()]
    n = len(words)
    if n < min_words:
        return (False, f"word count: {n} below floor {min_words}")
    if n > max_words:
        return (False, f"word count: {n} above ceiling {max_words}")

    lower = stripped.lower()
    for marker in _AUDIT_OUT_COT_MARKERS:
        if marker in lower:
            return (False, f"cot marker: {marker!r}")

    return (True, "ok")


def _call_audit_model(prompt: str, system: str = "",
                      max_tokens: int = 2048) -> tuple[str, str | None]:
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return ("", None)
    try:
        from openai import OpenAI
    except ImportError:
        return ("", None)
    try:
        from jeeves.audit_models import resolve_audit_models
    except Exception:
        return ("", None)

    client = OpenAI(
        api_key=api_key, base_url="https://openrouter.ai/api/v1", timeout=120.0,
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_exc: Exception | None = None
    for model_id in resolve_audit_models():
        try:
            resp = client.chat.completions.create(
                model=model_id, messages=messages,
                max_tokens=max_tokens, temperature=0.4,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                return (text, model_id)
        except Exception as exc:
            last_exc = exc
            continue
    log.warning("audit_fix: every audit model failed (last=%s)", last_exc)
    return ("", None)


def fix_empty_with_data(html: str, defects: list[dict], session: dict,
                        actions: list[FixAction]) -> tuple[str, str | None]:
    """F6: re-render an empty section using the available session data."""
    targets = [d for d in defects if d["type"] == "empty_with_data"
               and d.get("section")]
    if not targets:
        return html, None

    model_used: str | None = None
    h3_to_sectors = {
        "The Library Stacks": ["literary_pick"],
        "The Reading Room": ["intellectual_journals", "enriched_articles"],
        "The Domestic Sphere": ["local_news"],
        "Beyond the Geofence": ["family", "global_news"],
        "The Specific Enquiries": ["triadic_ontology", "ai_systems", "uap"],
        "The Commercial Ledger": ["wearable_ai", "ai_systems"],
        "Talk of the Town": ["newyorker"],
    }

    for d in targets:
        section_name = d["section"]
        sectors = h3_to_sectors.get(section_name, [])
        data_summary = []
        for sec in sectors:
            v = session.get(sec)
            if isinstance(v, list) and v:
                for item in v[:3]:
                    if isinstance(item, dict):
                        title = item.get("headline") or item.get("title") or ""
                        url = item.get("url") or ""
                        if url:
                            data_summary.append(f"- {title}: {url}")
            elif isinstance(v, dict) and v.get("available"):
                title = v.get("title") or v.get("headline") or ""
                url = v.get("url") or ""
                summary = v.get("summary") or v.get("dek") or ""
                if title:
                    data_summary.append(f"- {title}: {url}\n  {summary[:300]}")
        if not data_summary:
            actions.append(FixAction(
                type="rerender_empty_with_data",
                section=section_name,
                detail="skipped — no usable data extracted from session",
                status="skipped",
            ))
            continue

        system = (
            "You are Jeeves, a cultivated British butler writing a single "
            "paragraph of a daily morning briefing for Mister Lang. Voice: "
            "dry, precise, occasionally barbed; only swear if it lands. "
            "Output: ONE HTML <p>...</p> paragraph (or two if the data "
            "demands it), 60-180 words. NO h3, NO links to homepages, NO "
            "facts that aren't supported by the data given."
        )
        prompt = (
            f"Section: {section_name}\n\n"
            f"Available data:\n" + "\n".join(data_summary) + "\n\n"
            "Write the section paragraph(s). Use <a href=\"...\">title</a> "
            "for the URL anchors only — never bare hostnames. Return only HTML."
        )
        text, model = _call_audit_model(prompt, system=system, max_tokens=600)
        if not text or not model:
            actions.append(FixAction(
                type="rerender_empty_with_data",
                section=section_name,
                detail="skipped — LLM call failed",
                status="failed",
            ))
            continue
        # F-001 — gate the splice on structural validation. Reasoning models
        # leak chain-of-thought; without this gate the planning prose ends
        # up in the briefing (see 2026-05-06 incident).
        ok, reason = _validate_audit_model_output(text)
        if not ok:
            log.warning(
                "audit_fix: validator rejected output for %s (model=%s, reason=%s)",
                section_name, model, reason,
            )
            actions.append(FixAction(
                type="rerender_empty_with_data",
                section=section_name,
                detail=f"validator rejected: {reason}",
                status="failed",
                evidence={"model": model, "preview": text[:120]},
            ))
            continue
        model_used = model

        # Replace the empty section body. Find h3 + replace whatever is between
        # it and the next h3 (or end) with the new text.
        m = re.search(
            rf"(<h3[^>]*>{re.escape(section_name)}</h3>)",
            html, re.IGNORECASE,
        )
        if not m:
            actions.append(FixAction(
                type="rerender_empty_with_data",
                section=section_name,
                detail="skipped — h3 vanished mid-fix",
                status="failed",
            ))
            continue
        next_h3 = _H3_RE.search(html, m.end())
        end = next_h3.start() if next_h3 else html.find("</body>")
        if end == -1:
            end = len(html)
        html = html[:m.end()] + "\n" + text.strip() + "\n" + html[end:]
        actions.append(FixAction(
            type="rerender_empty_with_data",
            section=section_name,
            detail=f"re-rendered via {model}",
            status="applied",
            evidence={"model": model, "chars": len(text)},
        ))

    return html, model_used


def fix_greeting_incomplete(html: str, defects: list[dict], session: dict,
                            actions: list[FixAction]) -> tuple[str, str | None]:
    """F7: re-render Part 1 with weather + correspondence + date."""
    greeting_defects = [d for d in defects
                        if d["type"].startswith("greeting_")]
    if not greeting_defects:
        return html, None

    weather = session.get("weather") or ""
    corr = (session.get("correspondence") or {}).get("text", "")
    date_str = session.get("date") or ""

    if not (weather or corr):
        actions.append(FixAction(
            type="rerender_greeting",
            section="(greeting)",
            detail="skipped — no weather + correspondence to seed",
            status="skipped",
        ))
        return html, None

    system = (
        "You are Jeeves writing the OPENING paragraph of Mister Lang's "
        "morning briefing. Voice: dry, precise butler. Output: ONE HTML "
        "<p>...</p> paragraph, 80-150 words, mentioning the weekday, the "
        "weather, and a one-line preview of the correspondence load. Do "
        "NOT emit an h3 — this is the greeting, before any section. Return "
        "only the <p> tag."
    )
    prompt = (
        f"Today's date: {date_str}\n"
        f"Weather: {weather}\n"
        f"Correspondence handoff:\n{corr}\n\n"
        "Write the greeting paragraph in Jeeves voice."
    )
    text, model = _call_audit_model(prompt, system=system, max_tokens=400)
    if not text or not model:
        actions.append(FixAction(
            type="rerender_greeting",
            section="(greeting)",
            detail="skipped — LLM call failed",
            status="failed",
        ))
        return html, None
    # F-007 — gate the splice on structural validation. Same threat model
    # as F-001 in fix_empty_with_data: reasoning models leak chain-of-thought,
    # and without this gate the planning prose ends up in the greeting.
    ok, reason = _validate_audit_model_output(text)
    if not ok:
        log.warning(
            "audit_fix: validator rejected greeting output (model=%s, reason=%s)",
            model, reason,
        )
        actions.append(FixAction(
            type="rerender_greeting",
            section="(greeting)",
            detail=f"validator rejected: {reason}",
            status="failed",
            evidence={"model": model, "preview": text[:120]},
        ))
        return html, None

    # Replace the FIRST <p> in the document (greeting is always first).
    m = re.search(r"<p[^>]*>.*?</p>", html, re.DOTALL)
    if not m:
        actions.append(FixAction(
            type="rerender_greeting",
            section="(greeting)",
            detail="skipped — no <p> found to replace",
            status="failed",
        ))
        return html, None
    html = html[:m.start()] + text.strip() + html[m.end():]
    actions.append(FixAction(
        type="rerender_greeting",
        section="(greeting)",
        detail=f"re-rendered via {model}",
        status="applied",
        evidence={"model": model, "chars": len(text)},
    ))
    return html, model


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_fix(date: str, sessions_dir: Path, *, use_llm: bool,
            dry_run: bool) -> FixReport:
    audit_path = sessions_dir / f"audit-{date}.json"
    briefing_path = sessions_dir / f"briefing-{date}.html"
    session_path = sessions_dir / f"session-{date}.json"

    if not audit_path.exists():
        log.error("audit JSON missing: %s", audit_path)
        raise SystemExit(1)
    if not briefing_path.exists():
        log.error("briefing missing: %s", briefing_path)
        raise SystemExit(1)
    if not session_path.exists():
        log.error("session missing: %s", session_path)
        raise SystemExit(1)

    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    briefing = briefing_path.read_text(encoding="utf-8")
    session = json.loads(session_path.read_text(encoding="utf-8"))
    pre_chars = len(briefing)
    defects = audit.get("defects") or []

    actions: list[FixAction] = []
    model_used: str | None = None

    # Apply fixes in priority order.
    briefing = fix_hallucinated_urls(briefing, defects, actions)
    briefing = fix_section_order(briefing, defects, actions)
    briefing = fix_dedup_within_run(briefing, defects, actions)
    briefing = fix_aside_repetition(briefing, defects, actions)
    briefing = fix_missing_sections(briefing, defects, session, actions)

    if use_llm:
        briefing, m1 = fix_empty_with_data(briefing, defects, session, actions)
        if m1:
            model_used = m1
        briefing, m2 = fix_greeting_incomplete(briefing, defects, session, actions)
        model_used = model_used or m2

    post_chars = len(briefing)

    if not dry_run:
        briefing_path.write_text(briefing, encoding="utf-8")
        log.info("revised briefing written: %s (%d -> %d chars)",
                 briefing_path, pre_chars, post_chars)

    report = FixReport(
        date=date,
        pre_fix_chars=pre_chars,
        post_fix_chars=post_chars,
        actions=actions,
        pre_fix_defect_count=len(defects),
        post_fix_defect_count=None,
        audit_model_used=model_used,
    )

    if not dry_run:
        out = sessions_dir / f"audit-fix-{date}.json"
        out.write_text(json.dumps(asdict(report), indent=2, default=str),
                       encoding="utf-8")
        log.info("fix log written: %s (%d actions)", out, len(actions))

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5 — Auditor (PR2)")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--sessions-dir", default="sessions")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip LLM-backed fixes (F6, F7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't write briefing or fix log")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sessions_dir = Path(args.sessions_dir).resolve()
    report = run_fix(args.date, sessions_dir, use_llm=not args.no_llm,
                     dry_run=args.dry_run)

    print(f"\nFIX SUMMARY — {args.date}")
    print(f"  pre-fix defects:   {report.pre_fix_defect_count}")
    print(f"  pre-fix chars:     {report.pre_fix_chars}")
    print(f"  post-fix chars:    {report.post_fix_chars}")
    print(f"  actions taken:     {len(report.actions)}")
    applied = sum(1 for a in report.actions if a.status == "applied")
    skipped = sum(1 for a in report.actions if a.status == "skipped")
    failed = sum(1 for a in report.actions if a.status == "failed")
    print(f"  applied:           {applied}")
    print(f"  skipped:           {skipped}")
    print(f"  failed:            {failed}")
    print(f"  audit model used:  {report.audit_model_used or '(no LLM)'}")
    if report.actions:
        print(f"\nActions:")
        for a in report.actions[:12]:
            print(f"  [{a.status:8}] {a.type:30} {a.section or '-':25} {a.detail[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
