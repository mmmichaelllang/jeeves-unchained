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
