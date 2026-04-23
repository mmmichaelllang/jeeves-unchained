from jeeves.dedup import covered_headlines, covered_urls
from jeeves.schema import SessionModel
from jeeves.testing.mocks import canned_session
from datetime import date


def test_covered_urls_collects_from_all_sectors():
    payload = canned_session(date(2026, 4, 23))
    model = SessionModel.model_validate(payload)
    urls = covered_urls(model)
    # From dedup.covered_urls + local_news + global_news + newyorker + enriched.
    assert "https://www.example.com/story-1" in urls
    assert "https://myedmondsnews.com/council-parking" in urls
    assert "https://www.newyorker.com/magazine/mock" in urls


def test_covered_headlines_reads_from_dedup_only():
    payload = canned_session(date(2026, 4, 23))
    model = SessionModel.model_validate(payload)
    heads = covered_headlines(model)
    assert "Example breaking story" in heads


def test_covered_urls_handles_none():
    assert covered_urls(None) == set()
    assert covered_headlines(None) == set()
