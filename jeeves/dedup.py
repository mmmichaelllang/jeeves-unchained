"""Extract covered-URL sets from prior sessions for the dedup prompt context."""

from __future__ import annotations

from urllib.parse import urlparse

from .schema import SessionModel


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
    return {u.rstrip("/") for u in out if u}


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
