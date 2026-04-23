"""Unit tests for jeeves.research_sectors — per-sector dedup + parsing helpers."""

from __future__ import annotations

import pytest

from jeeves.research_sectors import (
    SECTOR_SPECS,
    _parse_sector_output,
    collect_headlines_from_sector,
    collect_urls_from_sector,
    extract_correspondence_references,
)


def _spec(name: str):
    return next(s for s in SECTOR_SPECS if s.name == name)


def test_parse_sector_output_string_sector_returns_raw_text():
    raw = "Partly cloudy with a 40% chance of rain. High 58F."
    assert _parse_sector_output(raw, _spec("weather")) == raw


def test_parse_sector_output_strips_markdown_fences():
    raw = '```json\n[{"category":"municipal","source":"My Edmonds News","findings":"x","urls":["https://a"]}]\n```'
    out = _parse_sector_output(raw, _spec("local_news"))
    assert isinstance(out, list) and out[0]["source"] == "My Edmonds News"


def test_parse_sector_output_tolerates_prose_around_json():
    raw = 'Here is the result:\n[{"source":"BBC","findings":"...","urls":[]}]\nEnd.'
    out = _parse_sector_output(raw, _spec("global_news"))
    assert isinstance(out, list) and len(out) == 1


def test_parse_sector_output_returns_default_on_unparseable():
    out = _parse_sector_output("completely not json", _spec("local_news"))
    assert out == []


def test_parse_sector_output_deep_shape():
    raw = '{"findings":"triadic stuff","urls":["https://x"]}'
    out = _parse_sector_output(raw, _spec("triadic_ontology"))
    assert out["findings"] == "triadic stuff"
    assert out["urls"] == ["https://x"]


def test_collect_urls_walks_nested_structures():
    value = [
        {"category": "a", "urls": ["https://one"], "findings": "x"},
        {"category": "b", "urls": ["https://two", "https://three"]},
    ]
    assert sorted(collect_urls_from_sector(value)) == [
        "https://one", "https://three", "https://two"
    ]


def test_collect_urls_picks_single_url_field_too():
    value = {"available": True, "url": "https://newyorker.com/x", "title": "T"}
    assert collect_urls_from_sector(value) == ["https://newyorker.com/x"]


def test_collect_headlines_pulls_title_headline_subject_role_district():
    value = [
        {"title": "Tacoma flooding", "urls": []},
        {"headline": "Senate hearing", "urls": []},
        {"openings": [{"role": "HS English", "district": "Edmonds"}]},
    ]
    out = collect_headlines_from_sector(value)
    assert "Tacoma flooding" in out
    assert "Senate hearing" in out
    assert "HS English" in out
    assert "Edmonds" in out


def test_collect_headlines_ignores_plain_strings_and_urls():
    assert collect_headlines_from_sector("a weather string") == []
    assert collect_headlines_from_sector({"urls": ["https://x"]}) == []


def test_extract_correspondence_references_parses_handoff_lines():
    text = (
        "- [escalation] Sarah Lang: pick up milk, confirm storytime\n"
        "- [scheduling] Northshore SD HR: first-round interview\n"
        "- [no action] GitHub: workflow queued\n"
    )
    refs = extract_correspondence_references(text)
    assert refs == [
        "email | Sarah Lang",
        "email | Northshore SD HR",
        "email | GitHub",
    ]


def test_extract_correspondence_references_handles_empty_and_malformed():
    assert extract_correspondence_references("") == []
    assert extract_correspondence_references("just prose, no bracketed classifications") == []


def test_sector_specs_cover_every_researched_session_field():
    # Guard against schema drift: every researched SessionModel field should
    # have a matching SECTOR_SPEC. Housekeeping fields (date/status/dedup) are
    # populated by the driver, `correspondence` by Phase 4, and `vault_insight`
    # is an offline hook that may be filled by a separate sync — none of those
    # are researched by the agent.
    from jeeves.schema import SessionModel

    excluded = {"date", "status", "dedup", "correspondence", "vault_insight"}
    researched = set(SessionModel.model_fields.keys()) - excluded
    spec_names = {s.name for s in SECTOR_SPECS}
    assert researched == spec_names, (
        f"spec/schema mismatch. spec has {spec_names}, researched schema has {researched}"
    )
