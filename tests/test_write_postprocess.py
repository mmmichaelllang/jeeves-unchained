"""Unit tests for jeeves.write post-processing."""

from __future__ import annotations

from datetime import date

from jeeves.schema import SessionModel
from jeeves.testing.mocks import canned_session
from jeeves.write import (
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
