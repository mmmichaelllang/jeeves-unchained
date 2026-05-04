"""Standalone tests that exercise the dedup-fix helpers WITHOUT importing
the full jeeves.write module (which requires pydantic).

Used as a sandbox sanity check. The full test_dedup_repetition_fix.py
tests run in the project venv.
"""

from __future__ import annotations

import re

# Pull only the regex constants + helper functions we need to copy/test.

_H3_TAG_RE = re.compile(r"<h3[^>]*>(.*?)</h3>", re.IGNORECASE | re.DOTALL)
_P_TAG_RE = re.compile(r"<p\b[^>]*>(.*?)</p>", re.IGNORECASE | re.DOTALL)
_NY_BLOCK_FENCE_RE = re.compile(
    r"<!--\s*NEWYORKER_START\s*-->.*?<!--\s*NEWYORKER_END\s*-->",
    re.IGNORECASE | re.DOTALL,
)


# ---- _shingles + _jaccard + _paragraph_quality_score (Phase 6) ------------


def _shingles(text: str, k: int = 3) -> set[str]:
    words = re.sub(r"\s+", " ", text).strip().lower().split()
    if len(words) < k:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i : i + k]) for i in range(len(words) - k + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def _paragraph_quality_score(body: str) -> tuple[int, int, int]:
    anchors = len(re.findall(r"<a\s[^>]*href=", body, re.IGNORECASE))
    plain = re.sub(r"<[^>]+>", " ", body)
    plain = re.sub(r"\s+", " ", plain).strip()
    words = len(plain.split())
    return (anchors, words, len(plain))


def _dedup_paragraphs(html: str, jaccard_threshold: float = 0.5) -> str:
    paragraphs = []
    for m in _P_TAG_RE.finditer(html):
        body = m.group(1)
        plain = re.sub(r"<[^>]+>", " ", body)
        plain = re.sub(r"\s+", " ", plain).strip()
        if len(plain.split()) <= 6:
            continue
        sh = _shingles(plain, k=3)
        if len(sh) < 3:
            continue
        score = _paragraph_quality_score(body)
        paragraphs.append((m.start(), m.end(), body, sh, score))
    drop = set()
    for i in range(len(paragraphs)):
        if i in drop:
            continue
        sh_i, score_i = paragraphs[i][3], paragraphs[i][4]
        for j in range(i + 1, len(paragraphs)):
            if j in drop:
                continue
            sh_j, score_j = paragraphs[j][3], paragraphs[j][4]
            if _jaccard(sh_i, sh_j) < jaccard_threshold:
                continue
            if score_j > score_i:
                drop.add(i)
                break
            else:
                drop.add(j)
    out = html
    for idx in sorted(drop, reverse=True):
        start, end, _, _, _ = paragraphs[idx]
        out = out[:start] + out[end:]
    return out


# ---- _truncate_to_h3_budget (Phase 1) ------------------------------------


def _truncate_to_h3_budget(html: str, max_h3: int) -> str:
    matches = list(_H3_TAG_RE.finditer(html))
    if len(matches) <= max_h3:
        return html
    cut_at = matches[max_h3].start()
    return html[:cut_at].rstrip()


# ---- topic extractor (Phase 5) -------------------------------------------

_TOPIC_SKIP = frozenset({
    "sir", "jeeves", "mister", "lang", "the", "and", "or", "of", "a",
    "mister lang", "this", "that", "these", "those", "with", "from",
    "today", "yesterday", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday", "january", "february", "march",
    "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "however", "indeed", "rather", "given",
    "their", "there", "here", "would", "could", "should", "shall",
    "will", "must", "have", "been", "part", "page", "vol", "iii",
    "html", "http", "https",
})


def _extract_written_topics(text: str) -> list[str]:
    plain = re.sub(r"<[^>]+>", " ", text)
    titles = re.findall(r'"([^"]{5,80})"', plain)
    multi = re.findall(r'\b([A-Z][a-z]{1,}(?:\s[A-Z][a-z]{1,}){1,3})\b', plain)
    single = re.findall(r'\b([A-Z][a-z]{3,})\b', plain)
    acronyms = re.findall(r'\b([A-Z]{2,6})\b', plain)
    acts = re.findall(
        r'\b([A-Z][A-Za-z\s]{3,40}(?:Act|Bill|Amendment|Law|Resolution))\b', plain
    )
    combined = titles + multi + single + acronyms + acts
    seen = set()
    out = []
    for t in combined:
        cleaned = t.strip()
        slug = cleaned.lower()
        if slug in _TOPIC_SKIP or len(slug) < 2 or slug in seen:
            continue
        seen.add(slug)
        out.append(cleaned)
        if len(out) >= 80:
            break
    return out


# ---- Tests ---------------------------------------------------------------


def test_paragraph_dedup_keeps_richer_copy():
    html = (
        "<p>The Iran conflict remains at deadlock with peace talks stalled "
        "over control of the Strait of Hormuz and Iran's nuclear program.</p>"
        "<p>The Iran conflict remains at deadlock with peace talks stalled "
        'over control of the Strait of Hormuz, the <a href="https://bbc.com">'
        "BBC reports</a>, and Iran's nuclear program continues.</p>"
    )
    out = _dedup_paragraphs(html)
    assert out.count("<p>") == 1
    assert "BBC reports" in out


def test_paragraph_dedup_keeps_distinct():
    html = (
        "<p>Iran conflict update one with details about the strait closure.</p>"
        "<p>The Edmonds council adopted a budget for the next fiscal year.</p>"
    )
    out = _dedup_paragraphs(html)
    assert out.count("<p>") == 2


def test_truncate_to_h3_budget_cuts_excess():
    html = (
        "<h3>One</h3><p>a</p>"
        "<h3>Two</h3><p>b</p>"
        "<h3>Three</h3><p>c</p>"
    )
    out = _truncate_to_h3_budget(html, max_h3=1)
    assert "One" in out
    assert "Two" not in out
    assert "Three" not in out


def test_truncate_to_h3_budget_unchanged_within():
    html = "<h3>Only</h3><p>fine</p>"
    assert _truncate_to_h3_budget(html, max_h3=2) == html


def test_extract_topics_single_proper_nouns():
    text = "<p>President Trump met with Iran officials in Tehran today.</p>"
    topics = [t.lower() for t in _extract_written_topics(text)]
    assert "trump" in topics
    assert "iran" in topics
    assert "tehran" in topics


def test_extract_topics_acronyms():
    text = "<p>The OFAC issued an alert; AARO confirmed UAP review.</p>"
    topics = [t.upper() for t in _extract_written_topics(text)]
    assert "OFAC" in topics
    assert "AARO" in topics
    assert "UAP" in topics


def test_extract_topics_skips_filler():
    text = "<p>This morning Mister Lang and Sir Jeeves had tea together.</p>"
    topics = [t.lower() for t in _extract_written_topics(text)]
    assert "this" not in topics
    assert "mister" not in topics
    assert "jeeves" not in topics


if __name__ == "__main__":
    fns = [
        test_paragraph_dedup_keeps_richer_copy,
        test_paragraph_dedup_keeps_distinct,
        test_truncate_to_h3_budget_cuts_excess,
        test_truncate_to_h3_budget_unchanged_within,
        test_extract_topics_single_proper_nouns,
        test_extract_topics_acronyms,
        test_extract_topics_skips_filler,
    ]
    failed = 0
    for fn in fns:
        try:
            fn()
            print("PASS", fn.__name__)
        except AssertionError as e:
            print("FAIL", fn.__name__, "—", e)
            failed += 1
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    raise SystemExit(failed)
