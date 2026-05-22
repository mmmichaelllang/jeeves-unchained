"""Extract covered-URL sets from prior sessions for the dedup prompt context.

Also home to the canonical normalization helpers (`canonical_url`,
`canonical_headline`) used across the research + audit + write phases.
Keeping both in one module so the three phases dedup against the *same*
keys — drift between phases was the root cause of cross-day overlap defects
the auditor kept reporting after research dedup said "no dupes here".
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .schema import SessionModel


# ---------------------------------------------------------------------------
# Normalization helpers (audit ↔ research ↔ write share these — DO NOT
# reimplement locally anywhere else)
# ---------------------------------------------------------------------------

# Query parameters routinely added by share/email/ad infrastructure that
# carry no article identity. Stripped before comparison so the same URL
# decorated for Twitter, RSS, and email all bucket together.
_TRACKING_PARAM_PREFIXES: tuple[str, ...] = (
    "utm_",
    "fb",
    "gc",
    "ref",
    "share",
    "igshid",
    "mc_",
    "_hsenc",
    "_hsmi",
    "wt_",
    "cmpid",
    "spm",
    "yclid",
    "msclkid",
)

# Host prefixes treated as canonical of the bare host. m.guardian.com,
# amp.cnn.com, mobile.reuters.com all collapse to the bare host.
_MOBILE_HOST_PREFIXES: tuple[str, ...] = ("m.", "amp.", "mobile.", "www.")


def _strip_tracking_params(query: str) -> str:
    """Drop common tracking params, preserve the rest in stable order."""
    if not query:
        return ""
    pairs = parse_qsl(query, keep_blank_values=True)
    kept = [(k, v) for k, v in pairs if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)]
    return urlencode(kept, doseq=True)


def canonical_url(url: str) -> str:
    """Normalize a URL for dedup comparison.

    Rules (every rule deliberate — change here ripples through every dedup
    path):
      - lowercase host
      - strip ``www.``, ``m.``, ``amp.``, ``mobile.`` host prefixes
      - drop fragment
      - drop tracking query params (utm_*, fb*, gc*, ref*, share*, igshid, etc.)
      - strip trailing slash from path
      - keep scheme as-is (http/https treated distinct so a misconfigured
        sector that emits http://foo doesn't accidentally clobber https://foo
        — this is rare in practice)

    Returns the original string on any urlparse failure; never raises.
    """
    if not url or not isinstance(url, str):
        return url or ""
    try:
        p = urlparse(url.strip())
    except Exception:
        return url
    host = (p.netloc or "").lower()
    for prefix in _MOBILE_HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    path = (p.path or "").rstrip("/")
    query = _strip_tracking_params(p.query)
    # Fragment dropped unconditionally — #section3 / #comments are never article
    # identity.
    return urlunparse((p.scheme, host, path, "", query, ""))


# Apostrophes and quote-marks are DELETED (not space-substituted) so
# "Trump's tariffs" → "trumps tariffs", bucketing with "Trumps tariffs".
# Includes straight + curly + prime variants the wild emits.
_APOSTROPHE_RE = re.compile(r"['’‘ʼ]+")
# All other punctuation is space-substituted so "U.S. policy" tokenizes as
# two words ("u", "s") rather than collapsing into "uspolicy" (which would
# accidentally match unrelated headlines).
_PUNCT_RE = re.compile(r"[^\w\s]+", flags=re.UNICODE)
_WS_RE = re.compile(r"\s+")

# Short stopwords removed before compare. Conservative — only words whose
# presence/absence shouldn't separate the same story into different keys.
# Verbs, prepositions, and rare-but-meaningful words are NOT in this list.
_STOPWORDS = frozenset({
    "a", "an", "the",
    "and", "or", "but",
    "of", "in", "on", "at", "to", "for", "with", "by",
    "is", "are", "was", "were", "be", "been",
})


def canonical_headline(s: str) -> str:
    """Normalize a headline string for substring/equality dedup compares.

    Rules:
      - lowercase
      - strip punctuation (including apostrophes — "Trump's" → "trumps")
      - collapse whitespace
      - drop English articles and a handful of stopwords

    Returns "" on empty/None input. Designed to be cheap (no allocations of
    intermediate lists) — called O(headlines × sectors) per research run.
    """
    if not s or not isinstance(s, str):
        return ""
    lowered = s.lower()
    # Delete apostrophes FIRST so "trump's" → "trumps".
    no_apos = _APOSTROPHE_RE.sub("", lowered)
    # Then replace remaining punctuation with space so "u.s." → "u s".
    cleaned = _PUNCT_RE.sub(" ", no_apos)
    tokens = [t for t in _WS_RE.split(cleaned) if t and t not in _STOPWORDS]
    return " ".join(tokens)


def _host_of(url: str) -> str:
    if not url:
        return ""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    # Strip leading "www." so guardian.com and www.guardian.com bucket together.
    return host[4:] if host.startswith("www.") else host


def covered_sources_by_host(session: SessionModel | None) -> dict[str, list[str]]:
    """Map host → list of titles/headlines cited from that host in this session.

    Used by the research phase to power source-rotation: when the agent has
    a choice between several articles from a host that already appeared
    yesterday, prefer ones whose title is NOT in that host's list.

    Walks all sectors that carry per-item URLs + titles. Returns {} for None.
    """
    if session is None:
        return {}
    out: dict[str, list[str]] = {}

    def _add(host: str, title: str) -> None:
        if not host or not title:
            return
        bucket = out.setdefault(host, [])
        if title not in bucket:
            bucket.append(title)

    # Findings-shape sectors: Finding has source/findings/urls but no title.
    # Use first non-empty sentence of `findings` as a stand-in title; cap to 80c.
    def _finding_pseudo_title(f) -> str:
        src = (getattr(f, "source", None) or "").strip()
        text = (getattr(f, "findings", "") or "").strip()
        if not text:
            return src
        first = text.split(".", 1)[0].strip()
        first = first[:77] + "…" if len(first) > 80 else first
        if src and first:
            return f"{src}: {first}"
        return first or src

    for f in (
        session.local_news
        + session.global_news
        + session.intellectual_journals
        + session.wearable_ai
    ):
        title = _finding_pseudo_title(f)
        for u in (f.urls or []):
            _add(_host_of(u), title)

    # Enriched articles carry a real title.
    for art in session.enriched_articles:
        if art.url:
            _add(_host_of(art.url), (art.title or "").strip())

    # New Yorker — known title.
    if session.newyorker.url and session.newyorker.title:
        _add(_host_of(session.newyorker.url), session.newyorker.title)

    # Literary pick — known title.
    if session.literary_pick.url and session.literary_pick.title:
        _add(_host_of(session.literary_pick.url), session.literary_pick.title)

    return out


def covered_urls(session: SessionModel | None) -> set[str]:
    if session is None:
        return set()
    out: set[str] = set(session.dedup.covered_urls)
    for f in (
        session.local_news
        + session.global_news
        + session.intellectual_journals
        + session.wearable_ai
    ):
        out.update(f.urls)
    for block in (session.triadic_ontology, session.ai_systems, session.uap):
        out.update(block.urls)
    if session.newyorker.url:
        out.add(session.newyorker.url)
    if session.literary_pick.url:
        out.add(session.literary_pick.url)
    for art in session.enriched_articles:
        if art.url:
            out.add(art.url)
    # canonical_url handles tracking-param/host/fragment normalization so the
    # rolling dedup set buckets cleanly across schemes/hosts/utm decorations.
    # Previously a bare `u.rstrip("/")` left m.guardian.com and www.guardian.com
    # as distinct entries — half-baked dedup.
    return {canonical_url(u) for u in out if u}


def covered_headlines(session: SessionModel | None) -> set[str]:
    if session is None:
        return set()
    out = {h for h in session.dedup.covered_headlines if h}
    # Explicitly include the New Yorker title so PART9 and the research context
    # both recognise a repeat article even if collect_headlines_from_sector
    # wasn't called on that session's newyorker value.
    if session.newyorker.title:
        out.add(session.newyorker.title)
    # Include the literary pick title so the same book is not re-selected the
    # following day.  The literary_pick instruction checks dedup.covered_headlines
    # before selecting a title; without this the same book can recur daily.
    if session.literary_pick.title:
        out.add(session.literary_pick.title)
    return out
