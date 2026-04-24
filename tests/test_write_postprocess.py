"""Unit tests for jeeves.write post-processing."""

from __future__ import annotations

from datetime import date

from jeeves.schema import SessionModel
from jeeves.testing.mocks import canned_session
from jeeves.write import (
    PART1_SECTORS,
    PART2_SECTORS,
    PART3_SECTORS,
    _session_subset,
    _stitch_parts,
    _system_prompt_for_parts,
    load_write_system_prompt,
    postprocess_html,
    render_mock_briefing,
)


def _session() -> SessionModel:
    return SessionModel.model_validate(canned_session(date(2026, 4, 23)))


def test_write_prompt_loads_and_has_persona():
    prompt = load_write_system_prompt()
    assert prompt.strip().startswith("# Jeeves Write")
    assert "Mister Lang" in prompt
    assert "clusterfuck" in prompt
    assert "Sector 7" in prompt


def test_render_mock_briefing_validates():
    session = _session()
    html = render_mock_briefing(session)
    assert html.startswith("<!DOCTYPE html>")
    assert "</html>" in html
    assert "COVERAGE_LOG" in html


def test_postprocess_strips_markdown_fence():
    session = _session()
    fenced = "```html\n<!DOCTYPE html><html><body>hi</body></html>\n```"
    result = postprocess_html(fenced, session)
    assert result.html.startswith("<!DOCTYPE html>")
    assert "```" not in result.html


def test_postprocess_wraps_missing_doctype():
    session = _session()
    raw = "<html><body><p>oops no doctype</p></body></html>"
    result = postprocess_html(raw, session)
    assert result.html.startswith("<!DOCTYPE html>")


def test_postprocess_builds_coverage_log_from_anchors():
    session = _session()
    raw = """<!DOCTYPE html><html><body>
    <p><a href="https://www.example.com/story-1">Example story</a> happened.</p>
    <p><a href="https://www.nybooks.com/mock">NYRB essay</a> appeared.</p>
    <!-- COVERAGE_LOG_PLACEHOLDER -->
    </body></html>"""
    result = postprocess_html(raw, session)
    urls = {entry["url"] for entry in result.coverage_log}
    assert "https://www.example.com/story-1" in urls
    assert "https://www.nybooks.com/mock" in urls


def test_postprocess_preserves_existing_coverage_log():
    session = _session()
    raw = (
        "<!DOCTYPE html><html><body><p>content</p>"
        '<!-- COVERAGE_LOG: [{"headline":"h","url":"https://x.example.com","sector":"Sector 3"}] -->'
        "</body></html>"
    )
    result = postprocess_html(raw, session)
    assert len(result.coverage_log) == 1
    assert result.coverage_log[0]["url"] == "https://x.example.com"


def test_postprocess_flags_banned_words():
    session = _session()
    raw = """<!DOCTYPE html><html><body>
    <p>This happens in a vacuum and forms a rich tapestry.</p>
    </body></html>"""
    result = postprocess_html(raw, session)
    assert "in a vacuum" in result.banned_word_hits
    assert "tapestry" in result.banned_word_hits


def test_postprocess_flags_banned_transitions():
    session = _session()
    raw = """<!DOCTYPE html><html><body>
    <p>Moving on, Sir. Next, the weather.</p>
    </body></html>"""
    result = postprocess_html(raw, session)
    assert "Moving on," in result.banned_transition_hits
    assert "Next," in result.banned_transition_hits


def test_mock_briefing_has_enough_profane_asides():
    session = _session()
    html = render_mock_briefing(session)
    result = postprocess_html(html, session)
    assert result.profane_aside_count >= 5


def test_session_subset_only_keeps_requested_fields_plus_housekeeping():
    payload = {
        "date": "2026-04-24",
        "status": "complete",
        "dedup": {"covered_headlines": ["a"]},
        "weather": "W",
        "career": {"openings": []},
        "family": {"choir": "..."},
        "triadic_ontology": {"findings": "..."},
    }
    out = _session_subset(payload, ["weather", "career"])
    assert set(out.keys()) == {"date", "status", "dedup", "weather", "career"}
    # Housekeeping keys always present even if not listed.
    assert out["dedup"] == {"covered_headlines": ["a"]}
    # Non-listed sector dropped.
    assert "triadic_ontology" not in out


def test_stitch_parts_three_way_preserves_structure():
    p1 = (
        '<!DOCTYPE html><html><head></head><body><div class="container">'
        '<h1>Header</h1><p>Sector 1.</p><!-- PART1 END -->'
    )
    p2 = '<p>Sector 3.</p><!-- PART2 END -->'
    p3 = (
        '<p>Sector 4.</p><div class="closing"><p>Jeeves</p></div>'
        '<!-- COVERAGE_LOG_PLACEHOLDER --></div></body></html>'
    )
    out = _stitch_parts(p1, p2, p3)
    assert out.count("<!DOCTYPE") == 1
    assert out.lower().count("<h1>") == 1
    assert "<!-- PART1 END" not in out
    assert "<!-- PART2 END" not in out
    assert "<!-- COVERAGE_LOG_PLACEHOLDER -->" in out
    assert "</body>" in out and "</html>" in out


def test_stitch_strips_continuation_wrapper_if_model_leaks_it():
    p1 = '<!DOCTYPE html><html><body><div><h1>X</h1><p>1</p><!-- PART1 END -->'
    # p2 wrongly includes DOCTYPE/h1 — must be stripped
    p2 = '<!DOCTYPE html><html><body><h1>X</h1><p>2</p><!-- PART2 END -->'
    p3 = '<p>3</p></div></body></html>'
    out = _stitch_parts(p1, p2, p3)
    assert out.count("<!DOCTYPE") == 1
    assert out.lower().count("<h1>") == 1
    assert "<p>1</p>" in out and "<p>2</p>" in out and "<p>3</p>" in out


def test_sector_groups_partition_writable_fields_without_overlap():
    all_parts = PART1_SECTORS + PART2_SECTORS + PART3_SECTORS
    assert len(all_parts) == len(set(all_parts)), "sector appears in multiple parts"


def test_system_prompt_for_parts_strips_html_scaffold_block():
    base = load_write_system_prompt()
    trimmed = _system_prompt_for_parts()
    assert "## HTML scaffold" in base
    assert "## HTML scaffold" not in trimmed
    # Persona and mandatory rules still present.
    assert "You are **Jeeves**" in trimmed
    assert "Deduplication" in trimmed
