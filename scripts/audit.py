#!/usr/bin/env python3
"""Phase 5 — Auditor (PR1: detection only, no fixes).

Reads:
    sessions/session-<date>.json
    sessions/briefing-<date>.html

Writes:
    sessions/audit-<date>.json

Detectors (priority order — top three are user-flagged top priorities):

    D7  narrative_flow         (LLM-judged) — logical progression, transitions
    D8  dedup_violations       (deterministic) — within-run + cross-day URL/topic dupes
    D9  writing_quality        (LLM-judged) — voice consistency, filler, specificity

    D1  hallucinated_urls      (deterministic) — hrefs not in session JSON
    D2  empty_with_data        (deterministic) — h3 sections under 30 words despite available data
    D3  missing_section        (deterministic) — expected h3 absent
    D4  section_order          (deterministic) — h3 order vs PART_PLAN
    D5  aside_repetition       (deterministic) — same template 3+ times
    D6  greeting_incomplete    (deterministic) — missing weather/correspondence/date in Part 1

LLM detectors use ``jeeves.audit_models.resolve_audit_models()`` —
reasoning-first free-tier picker.

Usage:
    python scripts/audit.py --date 2026-05-06
    python scripts/audit.py --date 2026-05-06 --dry-run     # don't write audit JSON
    python scripts/audit.py --date 2026-05-06 --no-llm       # skip D7+D9 (deterministic only)

Exit code:
    0 — audit completed, JSON written
    1 — could not load session/briefing
    2 — internal error
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Allow running from repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

log = logging.getLogger("audit")


# ---------------------------------------------------------------------------
# Canonical structure (mirrors jeeves.write.PART_PLAN — kept independent so
# audit can run without importing write.py and pulling its dependencies).
# ---------------------------------------------------------------------------

CANONICAL_PART_PLAN: list[tuple[str, list[str]]] = [
    ("part1", ["correspondence", "weather"]),                                # greeting (no h3)
    ("part2", ["local_news"]),                                               # The Domestic Sphere
    ("part3", ["career"]),                                                   # (continues Domestic)
    ("part4", ["family", "global_news"]),                                    # The Wider World (global_news); family bundled here historically
    ("part5", ["intellectual_journals", "enriched_articles"]),               # The Reading Room
    ("part6", ["triadic_ontology", "ai_systems"]),                           # The Specific Enquiries
    ("part7", ["uap", "wearable_ai", "literary_pick"]),                      # The Specific Enquiries / Commercial Ledger / Library Stacks
    ("part8", ["literary_pick"]),                                            # The Library Stacks (vault_insight)
    ("part9", ["newyorker"]),                                                # Talk of the Town
]

# Canonical h3 ordering. Used for D4 (section_order).
# 2026-05-10: "The Wider World" is the canonical header for global_news per
# write_system.md. "Beyond the Geofence" was the historical (incorrect) header
# used in PART 4 prompt — kept in the canonical list at its same slot so old
# briefings + tests that reference it continue to validate. The postprocess
# h3 rewriter migrates forward going forward; both names are accepted in the
# auditor's section_order check during the transition.
EXPECTED_H3_ORDER: list[str] = [
    "The Domestic Sphere",
    "Beyond the Geofence",
    "The Wider World",
    "The Reading Room",
    "The Specific Enquiries",
    "The Commercial Ledger",
    "The Library Stacks",
    "Talk of the Town",
]


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class Defect:
    type: str
    severity: str             # "high" | "medium" | "low"
    section: str | None
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditReport:
    date: str
    briefing_chars: int
    session_status: str
    detectors_run: list[str]
    detectors_skipped: list[str]
    defects: list[Defect]
    # Top-level summary metrics for dashboard use.
    section_count: int = 0
    hallucinated_url_count: int = 0
    empty_section_count: int = 0
    aside_template_count: int = 0
    # LLM judgment tallies.
    narrative_flow_score: int | None = None
    writing_quality_score: int | None = None
    dedup_score: int | None = None
    audit_model_used: str | None = None


# ---------------------------------------------------------------------------
# Loading helpers
# ---------------------------------------------------------------------------


def load_session(date: str, sessions_dir: Path) -> dict | None:
    p = sessions_dir / f"session-{date}.json"
    if not p.exists():
        log.error("session JSON missing: %s", p)
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        log.error("session JSON unreadable: %s — %s", p, exc)
        return None


def load_briefing(date: str, sessions_dir: Path) -> str | None:
    p = sessions_dir / f"briefing-{date}.html"
    if not p.exists():
        log.error("briefing HTML missing: %s", p)
        return None
    try:
        return p.read_text(encoding="utf-8")
    except Exception as exc:
        log.error("briefing HTML unreadable: %s — %s", p, exc)
        return None


# ---------------------------------------------------------------------------
# Session URL aggregation (D1, D8 use this)
# ---------------------------------------------------------------------------


def aggregate_session_urls(session: dict) -> set[str]:
    """Return the set of every URL present anywhere in the session JSON.

    Sectors with various shapes:
      * dict with `findings: [...]` and `urls: [...]`
      * dict with `urls: list[str]`
      * flat list[dict] — items each having `url` or `urls`
      * single-object sectors: literary_pick.url, newyorker.url
    """
    urls: set[str] = set()

    def _add(u: Any) -> None:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            urls.add(u.strip())

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in ("url", "link", "href"):
                    _add(v)
                elif k in ("urls", "links"):
                    if isinstance(v, list):
                        for u in v:
                            _add(u)
                else:
                    _walk(v)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(session)
    return urls


# ---------------------------------------------------------------------------
# HTML parsing helpers (regex-only; deliberately no bs4 dep)
# ---------------------------------------------------------------------------


_H3_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)
_H2_RE = re.compile(r"<h2[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_HREF_RE = re.compile(r"<a[^>]*\bhref=[\"']([^\"']+)[\"'][^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_h3_sequence(html: str) -> list[str]:
    """Return every h3 inner text in document order, plain-text only."""
    out: list[str] = []
    for m in _H3_RE.finditer(html):
        text = _TAG_RE.sub("", m.group(1)).strip()
        out.append(text)
    return out


def extract_section_blocks(html: str) -> dict[str, str]:
    """Split the briefing into {h3_text: section_body_html} pairs.

    Body extends from after the </h3> closer up to the next h3 or end.
    Plain-text-only. Used for D2 (empty section detect) and D9 quality
    sampling.
    """
    blocks: dict[str, str] = {}
    matches = list(_H3_RE.finditer(html))
    for i, m in enumerate(matches):
        h3 = _TAG_RE.sub("", m.group(1)).strip()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(html)
        blocks[h3] = html[start:end]
    return blocks


def section_word_count(section_html: str) -> int:
    text = _TAG_RE.sub(" ", section_html)
    return len([w for w in text.split() if w.strip()])


def extract_hrefs(html: str) -> list[str]:
    return [m.group(1) for m in _HREF_RE.finditer(html)]


def extract_paragraph_texts(html: str) -> list[str]:
    """Return paragraph plain-text bodies. Used for D5 (aside repetition)."""
    out: list[str] = []
    for m in re.finditer(r"<p[^>]*>(.*?)</p>", html, re.IGNORECASE | re.DOTALL):
        text = _TAG_RE.sub(" ", m.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        if text:
            out.append(text)
    return out


# ---------------------------------------------------------------------------
# DETECTORS
# ---------------------------------------------------------------------------


def detect_hallucinated_urls(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D1: every <a href> in briefing must trace to session JSON."""
    session_urls = aggregate_session_urls(session)
    flagged = 0
    blocks = extract_section_blocks(html)
    h3_for_offset: list[tuple[int, str]] = []
    for m in _H3_RE.finditer(html):
        h3_for_offset.append((m.start(), _TAG_RE.sub("", m.group(1)).strip()))

    def _section_for_offset(offset: int) -> str:
        section = "(preamble)"
        for off, h3 in h3_for_offset:
            if off <= offset:
                section = h3
            else:
                break
        return section

    for m in _HREF_RE.finditer(html):
        href = m.group(1).strip()
        if not href.startswith(("http://", "https://")):
            continue
        # Allow archive.org / archive.ph fallbacks
        if "web.archive.org" in href or "archive.ph" in href:
            continue
        if href in session_urls:
            continue
        # Homepage URLs (path == "/" or empty) are auto-suspect.
        try:
            parsed = urlparse(href)
            is_homepage = parsed.path in ("", "/") and not parsed.query
        except Exception:
            is_homepage = False
        sev = "high" if is_homepage else "medium"
        section = _section_for_offset(m.start())
        defects.append(Defect(
            type="hallucinated_url",
            severity=sev,
            section=section,
            detail=f"href {href!r} not present in any session JSON sector",
            evidence={"url": href, "is_homepage": is_homepage},
        ))
        flagged += 1
    return flagged


def detect_empty_with_data(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D2: an h3 section under 30 words when the corresponding sector has data."""
    blocks = extract_section_blocks(html)
    flagged = 0
    h3_to_sectors = {
        "The Domestic Sphere": ["local_news"],
        "The Wider World": ["family", "global_news"],
        # Transitional alias 2026-05-10 — old briefings used the wrong
        # header name for global news. Accept either to avoid spurious
        # missing-section defects on prior briefings.
        "Beyond the Geofence": ["family", "global_news"],
        "The Reading Room": ["intellectual_journals", "enriched_articles"],
        "The Specific Enquiries": ["triadic_ontology", "ai_systems", "uap"],
        "The Commercial Ledger": ["wearable_ai", "ai_systems"],
        "The Library Stacks": ["literary_pick"],
        "Talk of the Town": ["newyorker"],
    }
    for h3, body in blocks.items():
        wc = section_word_count(body)
        if wc >= 30:
            continue
        sectors = h3_to_sectors.get(h3, [])
        has_data = False
        evidence_data = []
        for sec in sectors:
            v = session.get(sec)
            if isinstance(v, list) and v:
                has_data = True
                evidence_data.append(f"{sec}={len(v)}items")
            elif isinstance(v, dict):
                if v.get("findings") or v.get("urls") or v.get("available"):
                    has_data = True
                    evidence_data.append(f"{sec}=populated")
        if has_data:
            defects.append(Defect(
                type="empty_with_data",
                severity="high",
                section=h3,
                detail=(
                    f"section under 30 words ({wc}) but sectors "
                    f"{sectors} have available data"
                ),
                evidence={"word_count": wc, "data_signals": evidence_data},
            ))
            flagged += 1
    return flagged


def detect_missing_sections(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D3: an expected h3 absent given the session data we have."""
    h3s = set(extract_h3_sequence(html))
    flagged = 0
    rules = [
        ("The Domestic Sphere", ["local_news"]),
        ("The Reading Room", ["intellectual_journals", "enriched_articles"]),
        ("The Library Stacks", ["literary_pick"]),
        ("Talk of the Town", ["newyorker"]),
    ]
    for expected_h3, sectors in rules:
        if expected_h3 in h3s:
            continue
        has_data = False
        for s in sectors:
            v = session.get(s)
            if isinstance(v, list) and v:
                has_data = True
            elif isinstance(v, dict) and (v.get("findings") or v.get("urls")
                                         or v.get("available")):
                has_data = True
        if has_data:
            defects.append(Defect(
                type="missing_section",
                severity="high",
                section=expected_h3,
                detail=(
                    f"h3 {expected_h3!r} absent but sectors {sectors} "
                    "have data"
                ),
                evidence={"sectors": sectors},
            ))
            flagged += 1
    return flagged


def detect_section_order(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D4: h3 sequence vs canonical order. Skips h3s not in canonical list."""
    actual = [h for h in extract_h3_sequence(html) if h in EXPECTED_H3_ORDER]
    canonical = [h for h in EXPECTED_H3_ORDER if h in actual]
    if actual == canonical:
        return 0
    defects.append(Defect(
        type="section_order",
        severity="high",
        section=None,
        detail="h3 order does not match canonical PART_PLAN",
        evidence={"actual": actual, "canonical": canonical},
    ))
    return 1


def detect_aside_repetition(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D5: aside templates repeated 3+ times."""
    paragraphs = extract_paragraph_texts(html)
    # Match the closing aside templates (e.g. "A proper, top-tier fucking
    # shambles." or "A right old fucking shambles."). Captures the wit
    # adjective + noun cluster but is liberal — we just need recurrence.
    aside_re = re.compile(
        r"(?i)\ba\s+(?:proper|right|total|absolute|utter|complete)[^.]{0,80}"
        r"(?:shambles|shitshow|shit-show|clusterfuck|shit-tornado|"
        r"shit-storm|fuckup|fuck-up|fucking\s+\w+|deep-fried[^.]{0,30}|"
        r"gold-plated[^.]{0,30}|bespoke[^.]{0,30}|hand-crafted[^.]{0,30})"
        r"[^.]*?\."
    )
    counter: dict[str, int] = {}
    for p in paragraphs:
        for m in aside_re.finditer(p):
            template = m.group(0).strip().lower()
            counter[template] = counter.get(template, 0) + 1
    flagged = 0
    for template, n in counter.items():
        if n >= 2:
            defects.append(Defect(
                type="aside_repetition",
                severity="medium",
                section=None,
                detail=f"aside template repeated {n} times (formulaic)",
                evidence={"template": template, "count": n},
            ))
            flagged += 1
    # Bigger-picture: count unique aside-ish closers across all paragraphs.
    all_aside_count = sum(counter.values())
    if all_aside_count and all_aside_count > len(paragraphs) * 0.5:
        defects.append(Defect(
            type="aside_overuse",
            severity="medium",
            section=None,
            detail=(
                f"aside closers in {all_aside_count}/{len(paragraphs)} "
                "paragraphs — formula degenerated"
            ),
            evidence={"aside_count": all_aside_count,
                      "paragraph_count": len(paragraphs)},
        ))
        flagged += 1
    return flagged


def detect_greeting_incomplete(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D6: Part 1 (greeting) must mention weather + correspondence + date."""
    # Pull the greeting region — everything before the first h3.
    first_h3 = _H3_RE.search(html)
    greeting = html[:first_h3.start()] if first_h3 else html
    greeting_lower = greeting.lower()

    flagged = 0

    weather = session.get("weather") or ""
    if isinstance(weather, str) and weather.strip():
        # Look for a temp signal (°F or "high") OR a key weather word.
        has_weather_signal = bool(
            re.search(r"\d{1,3}\s*°\s*f", greeting_lower)
            or "°f" in greeting_lower
            or "high " in greeting_lower
            or "temperature" in greeting_lower
        )
        if not has_weather_signal:
            defects.append(Defect(
                type="greeting_missing_weather",
                severity="medium",
                section="(greeting)",
                detail="weather string present in session but not in greeting",
                evidence={"weather_preview": weather[:100]},
            ))
            flagged += 1

    corr = session.get("correspondence") or {}
    corr_text = corr.get("text") if isinstance(corr, dict) else ""
    if corr_text:
        # Greeting should preview correspondence — count of escalations or
        # named contacts. Look for "escalation" / "reply needed" / a named
        # contact from the handoff.
        # Format from build_handoff_text: "- [tag] Name: summary"
        # Extract Name = text between bracket-close and first colon.
        contact_names: list[str] = []
        for l in corr_text.split("\n"):
            m = re.search(r"\]\s+([^:]+):", l)
            if m:
                contact_names.append(m.group(1).strip())
        # Crude check: any contact name in greeting?
        if not any(name and name.lower() in greeting_lower
                   for name in contact_names):
            defects.append(Defect(
                type="greeting_missing_correspondence",
                severity="medium",
                section="(greeting)",
                detail="correspondence handoff text present but no preview "
                       "in greeting",
                evidence={"handoff_preview": corr_text[:200]},
            ))
            flagged += 1

    # Date check.
    date_str = session.get("date") or ""
    if date_str:
        # Strip dashes, check if any chunk appears in greeting.
        weekday_present = any(
            d in greeting_lower
            for d in ("monday", "tuesday", "wednesday", "thursday",
                      "friday", "saturday", "sunday")
        )
        if not weekday_present:
            defects.append(Defect(
                type="greeting_missing_date",
                severity="low",
                section="(greeting)",
                detail="weekday/date not mentioned in greeting",
                evidence={"session_date": date_str},
            ))
            flagged += 1

    return flagged


def detect_recurring_opener(
    html: str, session: dict, sessions_dir: Path,
    defects: list[Defect],
) -> int:
    """D10 (2026-05-10): today's first body paragraph matches a prior briefing.

    Run after D6_greeting_incomplete. The opener "The world has not improved
    overnight, but it has at least produced several new opportunities to
    observe it failing." shipped 2026-04-28, 2026-05-09, AND 2026-05-10 —
    day-over-day recurrence the auditor must flag for F7 to rewrite.

    Compares the first ~250 chars of today's first <p> in the greeting
    region against the same slice from each of the last 4 days' briefings
    on disk. Exact (case-insensitive) match → recurring_opener defect.
    """
    from datetime import date as _date_t, timedelta

    # Pull today's first body <p>.
    body = re.sub(r"<head>[\s\S]*?</head>", "", html, flags=re.IGNORECASE)
    body = re.sub(r"<style>[\s\S]*?</style>", "", body, flags=re.IGNORECASE)
    today_p_match = None
    for m in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", body, re.IGNORECASE):
        block = m.group(0).lower()
        if 'class="signoff"' in block or "coverage_log" in block:
            continue
        today_p_match = m
        break
    if not today_p_match:
        return 0
    today_text = re.sub(r"<[^>]+>", " ", today_p_match.group(1))
    today_text = re.sub(r"\s+", " ", today_text).strip().lower()[:250]
    if not today_text:
        return 0

    # Resolve today's date.
    date_str = session.get("date") or ""
    if not date_str:
        return 0
    try:
        today = _date_t.fromisoformat(date_str)
    except ValueError:
        return 0

    flagged = 0
    for days_back in range(1, 5):
        prior = today - timedelta(days=days_back)
        prior_path = sessions_dir / f"briefing-{prior.isoformat()}.html"
        if not prior_path.exists():
            continue
        try:
            prior_html = prior_path.read_text(encoding="utf-8")
        except OSError:
            continue
        prior_body = re.sub(r"<head>[\s\S]*?</head>", "", prior_html, flags=re.IGNORECASE)
        prior_body = re.sub(r"<style>[\s\S]*?</style>", "", prior_body, flags=re.IGNORECASE)
        prior_first = None
        for m in re.finditer(r"<p[^>]*>([\s\S]*?)</p>", prior_body, re.IGNORECASE):
            block = m.group(0).lower()
            if 'class="signoff"' in block or "coverage_log" in block:
                continue
            prior_first = m
            break
        if not prior_first:
            continue
        prior_text = re.sub(r"<[^>]+>", " ", prior_first.group(1))
        prior_text = re.sub(r"\s+", " ", prior_text).strip().lower()[:250]
        if prior_text and prior_text == today_text:
            defects.append(Defect(
                type="recurring_opener",
                severity="medium",
                section="(greeting)",
                detail=(
                    f"today's first paragraph matches {prior.isoformat()} "
                    "briefing — opener has been recycled."
                ),
                evidence={
                    "matches_date": prior.isoformat(),
                    "opener_preview": today_text[:120],
                },
            ))
            flagged += 1
            break  # one match is enough — F7 will rewrite either way
    return flagged


def detect_dedup_violations(
    html: str, session: dict, defects: list[Defect]
) -> int:
    """D8: within-run + cross-day duplicates. Top priority detector.

    Within-run: a URL or named entity appearing in 2+ sections.
    Cross-day: URLs from session.dedup.covered_urls re-appearing.
    """
    flagged = 0

    # Within-run URL dupes across sections.
    blocks = extract_section_blocks(html)
    url_to_sections: dict[str, list[str]] = {}
    for h3, body in blocks.items():
        for href in extract_hrefs(body):
            url_to_sections.setdefault(href, []).append(h3)
    for url, sections in url_to_sections.items():
        unique_sections = list(dict.fromkeys(sections))
        if len(unique_sections) >= 2:
            defects.append(Defect(
                type="dedup_url_cross_section",
                severity="high",
                section=None,
                detail=f"URL appears in {len(unique_sections)} sections",
                evidence={"url": url, "sections": unique_sections},
            ))
            flagged += 1

    # Cross-day dupes — URLs in covered_urls (last 4 days).
    dedup = session.get("dedup") or {}
    covered = set(dedup.get("covered_urls") or [])
    if covered:
        all_briefing_urls = set(extract_hrefs(html))
        # Strip query strings / fragments for a softer compare.
        def _norm(u: str) -> str:
            try:
                p = urlparse(u)
                return f"{p.scheme}://{p.netloc}{p.path}".rstrip("/")
            except Exception:
                return u
        norm_briefing = {_norm(u) for u in all_briefing_urls}
        norm_covered = {_norm(u) for u in covered}
        overlap = norm_briefing & norm_covered
        if overlap:
            defects.append(Defect(
                type="dedup_cross_day_overlap",
                severity="medium",
                section=None,
                detail=f"{len(overlap)} URLs already covered in prior briefings",
                evidence={"overlap_count": len(overlap),
                          "samples": list(overlap)[:5]},
            ))
            flagged += 1

    return flagged


# ---------------------------------------------------------------------------
# LLM-judged detectors (D7, D9). Optional — gated by --no-llm.
# ---------------------------------------------------------------------------


def _call_audit_model(prompt: str, system: str = "") -> tuple[str, str | None]:
    """Run one chat completion via the reasoning-first OR resolver.

    Returns (response_text, model_id_used). On total failure returns
    ('', None) — caller skips the detector.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        log.warning("audit: OPENROUTER_API_KEY not set — skipping LLM detectors")
        return ("", None)
    try:
        from openai import OpenAI
    except ImportError:
        log.warning("audit: openai SDK not installed — skipping LLM detectors")
        return ("", None)
    try:
        from jeeves.audit_models import resolve_audit_models
    except Exception as exc:
        log.warning("audit: cannot import audit_models (%s)", exc)
        return ("", None)

    client = OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        timeout=120.0,
    )
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_exc: Exception | None = None
    for model_id in resolve_audit_models():
        try:
            resp = client.chat.completions.create(
                model=model_id,
                messages=messages,
                max_tokens=2048,
                temperature=0.2,
            )
            text = (resp.choices[0].message.content or "").strip()
            if text:
                log.info("audit: LLM call succeeded via %s", model_id)
                return (text, model_id)
        except Exception as exc:
            last_exc = exc
            log.debug("audit: %s failed (%s) — trying next", model_id, exc)
            continue
    log.warning("audit: every audit model failed (last=%s)", last_exc)
    return ("", None)


def detect_narrative_flow(
    html: str, session: dict, defects: list[Defect]
) -> tuple[int, int | None, str | None]:
    """D7: LLM-judged narrative flow + logical progression. Top priority.

    Returns (flagged_count, score_0_10, model_used).
    """
    plain = _TAG_RE.sub(" ", html)
    plain = re.sub(r"\s+", " ", plain).strip()
    sample = plain[:18000]

    system = (
        "You are a literary editor auditing a daily morning briefing in the "
        "voice of a cultivated British butler (the ‘Jeeves’ register). Your "
        "job is to assess narrative flow and logical progression — NOT facts, "
        "NOT spelling. Score from 0-10 (10 = seamless prose, 0 = a list of "
        "disconnected paragraphs).\n\n"
        "Identify specific defects. Be terse; one line per defect."
    )
    prompt = (
        "Briefing text:\n\n"
        f"{sample}\n\n"
        "Output JSON exactly in this shape, nothing else:\n"
        '{"score": <int 0-10>, "defects": [{"section": "...", "issue": "..."}, ...]}'
    )

    text, model = _call_audit_model(prompt, system=system)
    if not text:
        return (0, None, None)

    score: int | None = None
    issues: list[dict] = []
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            score = int(parsed.get("score", 0))
            issues = parsed.get("defects") or []
    except Exception:
        log.warning("audit: narrative flow JSON parse failed; raw=%r", text[:200])
        return (0, None, model)

    flagged = 0
    if score is not None and score < 7:
        defects.append(Defect(
            type="narrative_flow_low",
            severity="high" if score < 5 else "medium",
            section=None,
            detail=f"narrative flow score {score}/10",
            evidence={"score": score, "issues": issues[:10],
                      "model": model},
        ))
        flagged += 1
    elif issues:
        for issue in issues[:5]:
            defects.append(Defect(
                type="narrative_flow_issue",
                severity="low",
                section=str(issue.get("section") or ""),
                detail=str(issue.get("issue") or ""),
                evidence={"score": score, "model": model},
            ))
            flagged += 1
    return (flagged, score, model)


def detect_writing_quality(
    html: str, session: dict, defects: list[Defect]
) -> tuple[int, int | None, str | None]:
    """D9: LLM-judged writing quality + voice consistency. Top priority.

    Returns (flagged_count, score_0_10, model_used).
    """
    plain = _TAG_RE.sub(" ", html)
    plain = re.sub(r"\s+", " ", plain).strip()
    sample = plain[:18000]

    system = (
        "You are auditing the writing quality of a daily morning briefing "
        "addressed to one specific reader (Mister Lang). The voice is a "
        "cultivated British butler who occasionally swears (sparingly). "
        "Score 0-10:\n"
        "  10 = sentences are specific, varied, earn every word\n"
        "   5 = competent but generic, formulaic transitions, vague claims\n"
        "   0 = filler-heavy, repetitive, voice-broken\n\n"
        "Flag SPECIFICALLY: filler phrases ('it is worth noting'), "
        "repetitive sentence shapes, vague claims that don't ground in a "
        "named entity, formulaic asides used as verbal punctuation rather "
        "than wit."
    )
    prompt = (
        "Briefing text:\n\n"
        f"{sample}\n\n"
        "Output JSON exactly:\n"
        '{"score": <int 0-10>, "defects": [{"type": "filler|repetition|vague|voice_break|aside_overuse", "evidence": "<short quote>"}, ...]}'
    )

    text, model = _call_audit_model(prompt, system=system)
    if not text:
        return (0, None, None)

    score: int | None = None
    issues: list[dict] = []
    try:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            score = int(parsed.get("score", 0))
            issues = parsed.get("defects") or []
    except Exception:
        log.warning("audit: writing quality JSON parse failed; raw=%r", text[:200])
        return (0, None, model)

    flagged = 0
    if score is not None and score < 7:
        defects.append(Defect(
            type="writing_quality_low",
            severity="high" if score < 5 else "medium",
            section=None,
            detail=f"writing quality score {score}/10",
            evidence={"score": score, "issues": issues[:10], "model": model},
        ))
        flagged += 1
    else:
        for issue in issues[:6]:
            defects.append(Defect(
                type="writing_quality_issue",
                severity="low",
                section=None,
                detail=f"{issue.get('type','?')}: {issue.get('evidence','')[:160]}",
                evidence={"score": score, "model": model},
            ))
            flagged += 1
    return (flagged, score, model)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_audit(date: str, sessions_dir: Path, *, use_llm: bool = True) -> AuditReport:
    session = load_session(date, sessions_dir)
    if session is None:
        raise SystemExit(1)
    briefing = load_briefing(date, sessions_dir)
    if briefing is None:
        raise SystemExit(1)

    defects: list[Defect] = []
    detectors_run: list[str] = []
    detectors_skipped: list[str] = []

    # TOP-PRIORITY detectors first (D7, D8, D9).
    h_count = detect_dedup_violations(briefing, session, defects)
    detectors_run.append("D8_dedup")

    flow_score: int | None = None
    quality_score: int | None = None
    model_used: str | None = None
    if use_llm:
        flow_n, flow_score, m1 = detect_narrative_flow(briefing, session, defects)
        if m1:
            detectors_run.append("D7_narrative_flow")
            model_used = m1
        else:
            detectors_skipped.append("D7_narrative_flow")
        quality_n, quality_score, m2 = detect_writing_quality(briefing, session, defects)
        if m2:
            detectors_run.append("D9_writing_quality")
            model_used = model_used or m2
        else:
            detectors_skipped.append("D9_writing_quality")
    else:
        detectors_skipped.extend(["D7_narrative_flow", "D9_writing_quality"])

    # Deterministic detectors.
    halluc = detect_hallucinated_urls(briefing, session, defects)
    detectors_run.append("D1_hallucinated_urls")

    empty = detect_empty_with_data(briefing, session, defects)
    detectors_run.append("D2_empty_with_data")

    detect_missing_sections(briefing, session, defects)
    detectors_run.append("D3_missing_section")

    detect_section_order(briefing, session, defects)
    detectors_run.append("D4_section_order")

    asides = detect_aside_repetition(briefing, session, defects)
    detectors_run.append("D5_aside_repetition")

    detect_greeting_incomplete(briefing, session, defects)
    detectors_run.append("D6_greeting_incomplete")

    detect_recurring_opener(briefing, session, sessions_dir, defects)
    detectors_run.append("D10_recurring_opener")

    return AuditReport(
        date=date,
        briefing_chars=len(briefing),
        session_status=str(session.get("status") or "unknown"),
        detectors_run=detectors_run,
        detectors_skipped=detectors_skipped,
        defects=defects,
        section_count=len(extract_h3_sequence(briefing)),
        hallucinated_url_count=halluc,
        empty_section_count=empty,
        aside_template_count=asides,
        narrative_flow_score=flow_score,
        writing_quality_score=quality_score,
        audit_model_used=model_used,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 5 — Auditor (PR1)")
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--sessions-dir", default="sessions",
                        help="path to sessions/ (default: ./sessions)")
    parser.add_argument("--dry-run", action="store_true",
                        help="don't write audit JSON")
    parser.add_argument("--no-llm", action="store_true",
                        help="skip LLM-judged detectors (D7, D9)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    sessions_dir = Path(args.sessions_dir).resolve()
    report = run_audit(args.date, sessions_dir, use_llm=not args.no_llm)

    out_path = sessions_dir / f"audit-{args.date}.json"
    payload = asdict(report)
    if args.dry_run:
        print(json.dumps(payload, indent=2, default=str))
        return 0

    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    log.info("audit written: %s (%d defects, %d detectors run, %d skipped)",
             out_path, len(report.defects),
             len(report.detectors_run), len(report.detectors_skipped))
    # Summary table to stdout.
    print(f"\nAUDIT SUMMARY — {args.date}")
    print(f"  defects:                {len(report.defects)}")
    print(f"  hallucinated URLs:      {report.hallucinated_url_count}")
    print(f"  empty-with-data:        {report.empty_section_count}")
    print(f"  aside repetition:       {report.aside_template_count}")
    print(f"  narrative flow score:   {report.narrative_flow_score}")
    print(f"  writing quality score:  {report.writing_quality_score}")
    print(f"  audit model used:       {report.audit_model_used or '(no LLM)'}")
    if report.defects:
        print(f"\nTop defects:")
        for d in report.defects[:8]:
            print(f"  [{d.severity:6}] {d.type:30} {d.section or '-':25} {d.detail[:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
