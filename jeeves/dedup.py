"""Extract covered-URL sets from prior sessions for the dedup prompt context."""

from __future__ import annotations

from .schema import SessionModel


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
    for art in session.enriched_articles:
        if art.url:
            out.add(art.url)
    return {u.rstrip("/") for u in out if u}


def covered_headlines(session: SessionModel | None) -> set[str]:
    if session is None:
        return set()
    return {h for h in session.dedup.covered_headlines if h}
