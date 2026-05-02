"""Unit tests for jeeves.tools.playwright_extractor.

Pure-helper tests — no actual browser launch. The integration paths
(``extract_article``, ``run_navigation_session``) are tested at the unit
level by patching the ``_playwright_available`` and ``_openai_available``
sentinels so we can exercise the failure-soft return contracts without
needing playwright installed.
"""

from __future__ import annotations

import json

import pytest

from jeeves.tools import playwright_extractor as pe


# ---------------------------------------------------------------------------
# Schema (LLMResponse / LLMCommand)
# ---------------------------------------------------------------------------


def test_llm_command_accepts_valid_actions():
    for action in ("scroll_down", "click_text", "extract_main", "done", "give_up"):
        cmd = pe.LLMCommand(action=action)
        assert cmd.action == action


def test_llm_command_rejects_unknown_action():
    with pytest.raises(Exception):
        pe.LLMCommand(action="hover")


def test_llm_response_validates_three_keys():
    r = pe.LLMResponse(
        current_objective="find article",
        observation="search results page",
        command=pe.LLMCommand(action="click_text", target="ProPublica"),
    )
    assert r.command.target == "ProPublica"


def test_llm_response_rejects_missing_keys():
    with pytest.raises(Exception):
        pe.LLMResponse(
            current_objective="x",
            command=pe.LLMCommand(action="done"),
        )  # observation missing


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


def test_parse_llm_response_strips_markdown_fence():
    raw = '```json\n{"current_objective":"x","observation":"y","command":{"action":"done"}}\n```'
    resp, err = pe.parse_llm_response(raw)
    assert resp is not None
    assert err is None
    assert resp.command.action == "done"


def test_parse_llm_response_picks_first_json_object_amid_prose():
    raw = (
        'Sure! Here is my response:\n'
        '{"current_objective":"x","observation":"y","command":{"action":"done"}}\n'
        'Hope that helps.'
    )
    resp, err = pe.parse_llm_response(raw)
    assert resp is not None
    assert err is None


def test_parse_llm_response_returns_error_on_no_json():
    resp, err = pe.parse_llm_response("just prose, no JSON here")
    assert resp is None
    assert err is not None
    assert "no JSON object" in err


def test_parse_llm_response_returns_error_on_invalid_json():
    resp, err = pe.parse_llm_response('{"not": valid json}')
    assert resp is None
    assert err is not None
    assert "json decode" in err


def test_parse_llm_response_returns_error_on_schema_violation():
    raw = '{"current_objective":"x","observation":"y","command":{"action":"hover"}}'
    resp, err = pe.parse_llm_response(raw)
    assert resp is None
    assert err is not None
    assert "schema validation" in err


def test_parse_llm_response_handles_empty_string():
    resp, err = pe.parse_llm_response("")
    assert resp is None
    assert err == "empty response"


# ---------------------------------------------------------------------------
# Dead-end detection
# ---------------------------------------------------------------------------


def test_is_dead_end_catches_paywall_keywords():
    assert pe.is_dead_end("Subscribe to read the full article")
    assert pe.is_dead_end("This content is for subscribers only")
    assert pe.is_dead_end("Create a free account to continue reading")


def test_is_dead_end_catches_captcha_keywords():
    assert pe.is_dead_end("Please verify you are human")
    assert pe.is_dead_end("Are you a robot? Complete the captcha.")
    assert pe.is_dead_end("Verifying you are human")


def test_is_dead_end_catches_403_404():
    assert pe.is_dead_end("403 Forbidden — Access Denied")
    assert pe.is_dead_end("404 Not Found")
    assert pe.is_dead_end("Page not found")


def test_is_dead_end_returns_false_on_clean_content():
    assert not pe.is_dead_end("This is a perfectly normal article body.")
    assert not pe.is_dead_end("")
    assert not pe.is_dead_end(None or "")


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------


def test_sanitize_html_strips_script_and_style():
    html = (
        "<article>Real text"
        "<script>alert('x')</script>"
        "<style>.x{color:red}</style>"
        "</article>"
    )
    out = pe.sanitize_html(html)
    assert "alert" not in out
    assert "color:red" not in out
    assert "Real text" in out


def test_sanitize_html_strips_nav_footer_header_aside():
    html = (
        "<header>SiteNav</header>"
        "<article>The article body.</article>"
        "<footer>FooterText</footer>"
        "<aside>SidebarAd</aside>"
        "<nav>NavLinks</nav>"
    )
    out = pe.sanitize_html(html)
    assert "SiteNav" not in out
    assert "FooterText" not in out
    assert "SidebarAd" not in out
    assert "NavLinks" not in out
    assert "article body" in out


def test_sanitize_html_drops_iframes_svg_noscript():
    html = (
        "<article>X"
        "<iframe src='ad.html'>fallback</iframe>"
        "<svg><path d='M0 0'/></svg>"
        "<noscript>noscript text</noscript>"
        "</article>"
    )
    out = pe.sanitize_html(html)
    assert "ad.html" not in out
    assert "M0 0" not in out
    assert "noscript text" not in out
    assert "X" in out


def test_sanitize_html_strips_attributes():
    html = '<p class="banner" id="hero" style="color:red">Hello</p>'
    out = pe.sanitize_html(html)
    assert "banner" not in out
    assert "color:red" not in out
    assert "Hello" in out


def test_sanitize_html_caps_at_max_chars():
    html = "<article>" + ("x" * 50000) + "</article>"
    out = pe.sanitize_html(html, max_chars=1000)
    assert len(out) <= 1000


def test_sanitize_html_handles_empty_input():
    assert pe.sanitize_html("") == ""
    assert pe.sanitize_html(None or "") == ""


# ---------------------------------------------------------------------------
# Markdown crystallization
# ---------------------------------------------------------------------------


def test_html_to_markdown_preserves_headings():
    html = "<h1>Title</h1><h2>Subtitle</h2><p>Body</p>"
    out = pe.html_to_markdown(html)
    assert "# Title" in out
    assert "## Subtitle" in out
    assert "Body" in out


def test_html_to_markdown_paragraph_breaks():
    html = "<p>One.</p><p>Two.</p><p>Three.</p>"
    out = pe.html_to_markdown(html)
    assert "One." in out
    assert "Two." in out
    assert "Three." in out
    # Should be on separate lines (after blank-line collapsing).
    lines = [line for line in out.split("\n") if line.strip()]
    assert len(lines) == 3


def test_html_to_markdown_drops_anchor_href_keeps_text():
    html = '<p>Read <a href="https://x.com">this story</a> now.</p>'
    out = pe.html_to_markdown(html)
    assert "this story" in out
    assert "https://x.com" not in out


def test_html_to_markdown_decodes_entities():
    html = "<p>R&amp;D &mdash; what we do.</p>"
    out = pe.html_to_markdown(html)
    assert "R&D" in out
    assert "—" in out or "&mdash;" not in out


def test_html_to_markdown_collapses_extra_whitespace():
    html = "<p>One.</p>\n\n\n\n\n<p>Two.</p>"
    out = pe.html_to_markdown(html)
    assert "\n\n\n" not in out


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


def test_circuit_breaker_does_not_trip_on_first_record():
    cb = pe.CircuitBreaker(max_repeats=3)
    assert not cb.record("aaa")


def test_circuit_breaker_does_not_trip_with_distinct_hashes():
    cb = pe.CircuitBreaker(max_repeats=3)
    assert not cb.record("aaa")
    assert not cb.record("bbb")
    assert not cb.record("ccc")


def test_circuit_breaker_trips_after_three_identical():
    cb = pe.CircuitBreaker(max_repeats=3)
    cb.record("aaa")
    cb.record("aaa")
    assert cb.record("aaa") is True


def test_circuit_breaker_does_not_trip_after_break():
    cb = pe.CircuitBreaker(max_repeats=3)
    cb.record("aaa")
    cb.record("aaa")
    cb.record("bbb")  # different hash breaks the run
    assert not cb.record("aaa")


def test_circuit_breaker_reset_clears_history():
    cb = pe.CircuitBreaker(max_repeats=3)
    cb.record("aaa")
    cb.record("aaa")
    cb.reset()
    assert cb.history == []
    assert not cb.record("aaa")


def test_dom_hash_is_deterministic():
    h1 = pe.dom_hash("hello world")
    h2 = pe.dom_hash("hello world")
    assert h1 == h2


def test_dom_hash_differs_on_input_change():
    h1 = pe.dom_hash("hello")
    h2 = pe.dom_hash("hello!")
    assert h1 != h2


def test_dom_hash_is_16_chars():
    assert len(pe.dom_hash("anything")) == 16


# ---------------------------------------------------------------------------
# ActionLog (context flushing)
# ---------------------------------------------------------------------------


def test_action_log_stores_recent_entries():
    log = pe.ActionLog(max_entries=3)
    log.push("a")
    log.push("b")
    log.push("c")
    rendered = log.render()
    assert "a" in rendered
    assert "b" in rendered
    assert "c" in rendered


def test_action_log_drops_oldest_when_capacity_reached():
    log = pe.ActionLog(max_entries=3)
    log.push("a")
    log.push("b")
    log.push("c")
    log.push("d")
    rendered = log.render()
    assert "a" not in rendered  # dropped
    assert "d" in rendered


def test_action_log_empty_render():
    log = pe.ActionLog()
    assert "no prior actions" in log.render()


# ---------------------------------------------------------------------------
# extract_article — fail-soft contracts (no real browser)
# ---------------------------------------------------------------------------


def test_extract_article_returns_failure_on_empty_url():
    out = pe.extract_article("")
    assert out["success"] is False
    assert out["text"] == ""
    assert "error" in out


def test_extract_article_returns_failure_when_playwright_missing(monkeypatch):
    monkeypatch.setattr(pe, "_playwright_available", lambda: False)
    out = pe.extract_article("https://example.com/article")
    assert out["success"] is False
    assert "playwright not installed" in out["error"]
    assert out["text"] == ""
    assert out["url"] == "https://example.com/article"


def test_run_navigation_session_returns_failure_when_playwright_missing(monkeypatch):
    monkeypatch.setattr(pe, "_playwright_available", lambda: False)
    out = pe.run_navigation_session("https://example.com", "find article")
    assert out["success"] is False
    assert "playwright" in out["error"]


def test_run_navigation_session_returns_failure_when_openrouter_missing(monkeypatch):
    monkeypatch.setattr(pe, "_playwright_available", lambda: True)
    monkeypatch.setattr(pe, "_openai_available", lambda: False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    out = pe.run_navigation_session("https://example.com", "find article")
    assert out["success"] is False
    assert "openrouter" in out["error"]


# ---------------------------------------------------------------------------
# Universal-fallback wiring — verify Playwright is reached from every fetcher.
# ---------------------------------------------------------------------------


def test_tavily_extract_uses_playwright_when_tavily_returns_empty_body(monkeypatch):
    """When Tavily returns fetch_failed for a URL, tavily_extract must call
    the Playwright fallback. Verify the wiring exists by patching the
    fallback to a sentinel and checking it gets invoked.

    CRITICAL: must use ``monkeypatch.setattr`` / ``monkeypatch.setitem`` so
    the patched ``tavily.TavilyClient`` is auto-restored after this test —
    ``sys.modules["tavily"].TavilyClient = ...`` (direct mutation) leaks the
    fake into subsequent tests in the suite (see sprint-13 CI failure where
    test_tavily_extract_coerces_string_url_to_list saw the wrong fake).
    """
    import json
    import sys
    import types
    from datetime import date
    from pathlib import Path

    from jeeves.config import Config
    from jeeves.tools.quota import QuotaLedger

    class _FakeTavilyClient:
        def __init__(self, api_key):
            pass

        def extract(self, urls):
            # Tavily "found nothing" — empty raw_content.
            return {
                "results": [
                    {"url": u, "title": "", "raw_content": "", "content": ""}
                    for u in urls
                ]
            }

    if "tavily" not in sys.modules:
        fake_mod = types.ModuleType("tavily")
        fake_mod.TavilyClient = _FakeTavilyClient
        monkeypatch.setitem(sys.modules, "tavily", fake_mod)
    else:
        monkeypatch.setattr("tavily.TavilyClient", _FakeTavilyClient, raising=False)

    monkeypatch.setattr(
        "jeeves.tools.tavily.TavilyClient",
        _FakeTavilyClient,
        raising=False,
    )

    fallback_calls: list[str] = []

    def _fake_pw(url, **kwargs):
        fallback_calls.append(url)
        return {
            "url": url,
            "title": "Recovered title",
            "text": "Recovered article body via playwright " * 30,
            "success": True,
            "extracted_via": "playwright",
        }

    monkeypatch.setattr(
        "jeeves.tools.playwright_extractor.extract_article",
        _fake_pw,
    )

    cfg = Config(
        nvidia_api_key="", serper_api_key="", tavily_api_key="key", exa_api_key="",
        google_api_key="", groq_api_key="", gmail_app_password="",
        gmail_oauth_token_json="", github_token="", github_repository="t/r",
        run_date=date(2026, 5, 2),
    )
    ledger = QuotaLedger.__new__(QuotaLedger)
    ledger._state = {"providers": {}}
    import threading
    ledger._lock = threading.Lock()

    from jeeves.tools.tavily import make_tavily_extract

    fn = make_tavily_extract(cfg, ledger)
    out = fn(["https://example.com/a"])
    data = json.loads(out)

    # Playwright fallback was called for the failed URL.
    assert fallback_calls == ["https://example.com/a"]
    # The result contains the Playwright-recovered text + extracted_via marker.
    assert data["results"][0]["fetch_failed"] is False
    assert "Recovered article body" in data["results"][0]["text"]
    assert data["results"][0].get("extracted_via") == "playwright"


def test_enrichment_uses_playwright_when_httpx_fails(monkeypatch):
    """fetch_article_text must reach the Playwright fallback when both httpx
    and trafilatura yield nothing. Patch the fallback and verify."""
    import json

    pytest.importorskip("httpx")

    fallback_calls: list[str] = []

    def _fake_pw(url, **kwargs):
        fallback_calls.append(url)
        return {
            "url": url,
            "title": "Recovered",
            "text": "Recovered body " * 50,
            "success": True,
            "extracted_via": "playwright",
        }

    monkeypatch.setattr(
        "jeeves.tools.playwright_extractor.extract_article",
        _fake_pw,
    )

    # Force httpx GET to raise so the function bails before trafilatura.
    class _BoomClient:
        def get(self, url, *args, **kwargs):
            raise RuntimeError("connection reset by peer")

    from jeeves.tools import enrichment as _enr

    monkeypatch.setattr(_enr, "_HTTP_CLIENT", _BoomClient())

    out = _enr.fetch_article_text("https://example.com/x")
    data = json.loads(out)

    assert fallback_calls == ["https://example.com/x"]
    assert data["fetch_failed"] is False
    assert "Recovered body" in data["text"]
    assert data.get("extracted_via") == "playwright"
