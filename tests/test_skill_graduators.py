"""Tests for the weekly skill-graduator scripts (2026-05-09).

Coverage:
  - graduate_skip_lists: consecutive-streak detection, in-place rewrite,
    idempotency, missing-section insertion
  - graduate_skill_body: per-day URL set walk, host distribution,
    new-vs-prior ratio, stable/stuck producer detection
  - tokens_per_call_report: aggregation by provider × model, percentile
    helper, markdown rendering with no-event fallback
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _load(script_name: str):
    """Load a script as a module without going through scripts/* import path."""
    spec = importlib.util.spec_from_file_location(
        script_name, SCRIPTS_DIR / f"{script_name}.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[script_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def skip_lists():
    return _load("graduate_skip_lists")


@pytest.fixture
def skill_body():
    # body graduator imports skip_lists helpers — load that first.
    _load("graduate_skip_lists")
    return _load("graduate_skill_body")


@pytest.fixture
def tokens_report():
    return _load("tokens_per_call_report")


# ============================================================================
# graduate_skip_lists — URL canonicalization + streak detection
# ============================================================================

def test_strip_url_arxiv_canonical_form(skip_lists):
    """abs/ID, pdf/ID, abs/IDvN, pdf/IDvN.pdf all canonicalize to abs/ID."""
    forms = [
        "https://arxiv.org/abs/2603.13327",
        "https://arxiv.org/pdf/2603.13327",
        "https://www.arxiv.org/abs/2603.13327v1",
        "https://arxiv.org/pdf/2603.13327v3.pdf",
        "https://arxiv.org/abs/2603.13327/",
    ]
    canonical = {skip_lists._strip_url(u) for u in forms}
    assert len(canonical) == 1
    assert "arxiv.org/abs/2603.13327" in canonical.pop()


def test_strip_url_non_arxiv_passes_through(skip_lists):
    u = "https://example.com/foo/bar"
    assert skip_lists._strip_url(u) == u


def _make_session_with_urls(d: date, sector: str, urls: list[str]) -> dict:
    """Synthesize a session JSON with `urls` shoved into `sector` as a deep
    block (the simpler shape — list-shaped sectors take a different path)."""
    return {
        "date": d.isoformat(),
        "status": "complete",
        sector: {"findings": "x", "urls": urls},
    }


def test_consecutive_streak_detection(skip_lists):
    sessions = [
        (date(2026, 5, 1), _make_session_with_urls(date(2026, 5, 1), "ai_systems", ["https://arxiv.org/abs/A"])),
        (date(2026, 5, 2), _make_session_with_urls(date(2026, 5, 2), "ai_systems", ["https://arxiv.org/abs/A"])),
        (date(2026, 5, 3), _make_session_with_urls(date(2026, 5, 3), "ai_systems", ["https://arxiv.org/abs/A"])),
        (date(2026, 5, 4), _make_session_with_urls(date(2026, 5, 4), "ai_systems", ["https://arxiv.org/abs/B"])),
    ]
    streaks = skip_lists._items_with_consecutive_streaks(
        sessions, "ai_systems",
        min_consecutive=3,
        item_extractor=skip_lists._collect_sector_urls,
    )
    # A shipped 3 days, B 1 day. Only A meets the threshold.
    assert "https://arxiv.org/abs/A" in streaks
    assert "https://arxiv.org/abs/B" not in streaks
    assert streaks["https://arxiv.org/abs/A"] == 3


def test_streak_resets_on_gap(skip_lists):
    """A non-consecutive day breaks the streak — D1, D2, gap, D4 = max 2."""
    sessions = [
        (date(2026, 5, 1), _make_session_with_urls(date(2026, 5, 1), "ai_systems", ["X"])),
        (date(2026, 5, 2), _make_session_with_urls(date(2026, 5, 2), "ai_systems", ["X"])),
        (date(2026, 5, 4), _make_session_with_urls(date(2026, 5, 4), "ai_systems", ["X"])),
    ]
    streaks = skip_lists._items_with_consecutive_streaks(
        sessions, "ai_systems",
        min_consecutive=3,
        item_extractor=skip_lists._collect_sector_urls,
    )
    assert streaks == {}


def test_splice_skip_list_inserts_when_missing(skip_lists):
    text = (
        "---\nname: t\ntitle: T\ndescription: x\nsectors: [ai_systems]\nhosts: []\n---\n\n"
        "## Empty-feed protocol\n\nblah\n"
    )
    new = skip_lists._splice_skip_list(text, "BODY\n")
    assert skip_lists.SECTION_HEADING in new
    assert "BODY" in new
    # Inserted BEFORE the empty-feed anchor.
    assert new.index(skip_lists.SECTION_HEADING) < new.index(
        "## Empty-feed protocol"
    )


def test_splice_skip_list_replaces_existing_block(skip_lists):
    text = (
        f"---\nname: t\n---\n\n"
        f"{skip_lists.SECTION_HEADING}\n\n"
        f"{skip_lists.BEGIN_MARKER}\nOLD BODY\n{skip_lists.END_MARKER}\n\n"
        f"## Empty-feed protocol\n"
    )
    new = skip_lists._splice_skip_list(text, "NEW BODY\n")
    assert "NEW BODY" in new
    assert "OLD BODY" not in new
    # Section appears exactly once.
    assert new.count(skip_lists.SECTION_HEADING) == 1


def test_splice_skip_list_idempotent(skip_lists):
    text = (
        f"---\nname: t\n---\n\n"
        f"## Empty-feed protocol\nblah\n"
    )
    once = skip_lists._splice_skip_list(text, "BODY\n")
    twice = skip_lists._splice_skip_list(once, "BODY\n")
    assert once == twice


def test_render_skip_list_section_no_hits(skip_lists):
    body = skip_lists._render_skip_list_section(
        urls={}, headlines={}, days=14, min_consecutive=3,
    )
    assert "have crossed the streak threshold" in body


def test_render_skip_list_section_with_hits_orders_by_streak(skip_lists):
    body = skip_lists._render_skip_list_section(
        urls={"A": 5, "B": 3, "C": 7},
        headlines={},
        days=14,
        min_consecutive=3,
    )
    # C (7) before A (5) before B (3).
    assert body.index("`C`") < body.index("`A`")
    assert body.index("`A`") < body.index("`B`")


# ============================================================================
# graduate_skill_body — host distribution + new-vs-prior
# ============================================================================

def test_host_distribution_counts_unique_days(skill_body):
    sessions = [
        (date(2026, 5, 1), _make_session_with_urls(date(2026, 5, 1), "ai_systems",
                                                    ["https://arxiv.org/abs/A", "https://arxiv.org/abs/B"])),
        (date(2026, 5, 2), _make_session_with_urls(date(2026, 5, 2), "ai_systems",
                                                    ["https://arxiv.org/abs/C"])),
        (date(2026, 5, 3), _make_session_with_urls(date(2026, 5, 3), "ai_systems",
                                                    ["https://example.com/X"])),
    ]
    a = skill_body._analyze_sector(sessions, "ai_systems", stable_floor=2)
    hosts = dict(a["host_dist"])
    # arxiv.org appears day 1 + 2 = 2 days; example.com 1 day.
    assert hosts.get("arxiv.org") == 2
    assert hosts.get("example.com") == 1


def test_new_vs_prior_ratio(skill_body):
    sessions = [
        (date(2026, 5, 1), _make_session_with_urls(date(2026, 5, 1), "ai_systems", ["A"])),
        (date(2026, 5, 2), _make_session_with_urls(date(2026, 5, 2), "ai_systems", ["A", "B"])),
        (date(2026, 5, 3), _make_session_with_urls(date(2026, 5, 3), "ai_systems", ["A"])),
    ]
    a = skill_body._analyze_sector(sessions, "ai_systems", stable_floor=2)
    rows = {r["date"]: r for r in a["new_vs_prior"]}
    # Day 1 — both URLs new.
    assert rows["2026-05-01"]["new_count"] == 1
    assert rows["2026-05-01"]["ratio"] == 1.0
    # Day 2 — A is prior, B is new → new=1, total=2, ratio=0.5
    assert rows["2026-05-02"]["new_count"] == 1
    assert rows["2026-05-02"]["ratio"] == 0.5
    # Day 3 — A is prior, total=1, new=0, ratio=0
    assert rows["2026-05-03"]["new_count"] == 0
    assert rows["2026-05-03"]["ratio"] == 0.0


def test_stuck_producers_flagged(skill_body):
    sessions = [
        (date(2026, 5, d), _make_session_with_urls(date(2026, 5, d), "ai_systems", ["STUCK"]))
        for d in range(1, 6)
    ]
    a = skill_body._analyze_sector(sessions, "ai_systems", stable_floor=4)
    stuck = dict(a["stuck_producers"])
    assert stuck.get("STUCK") == 5


def test_render_observed_section_handles_no_data(skill_body):
    body = skill_body._render_observed_section(
        sectors=["nonexistent"],
        analyses={"nonexistent": {"total_days": 0,
                                   "host_dist": [], "new_vs_prior": [],
                                   "stable_producers": [], "stuck_producers": []}},
        days=14,
        stable_floor=4,
    )
    assert "no data in window" in body


# ---------- query analysis (Patch I) ----------

def test_queries_for_sector_filters_to_search_providers(skill_body):
    events = [
        {"event": "tool_call", "provider": "serper", "query": "edmonds news today", "ok": True},
        {"event": "tool_call", "provider": "serper", "query": "edmonds news today", "ok": True},
        {"event": "tool_call", "provider": "exa", "query": "intellectual journals 2026", "ok": True},
        # Non-search providers ignored.
        {"event": "tool_call", "provider": "fetch_article_text", "query": "irrelevant", "ok": True},
        # Missing query string ignored.
        {"event": "tool_call", "provider": "serper", "ok": True},
    ]
    queries = skill_body._queries_for_sector_hosts(events, hosts=[])
    # serper "edmonds news today" appears 2x (calls=2 ok=2), exa once.
    assert queries[0] == ("serper", "edmonds news today", 2, 2)
    assert ("exa", "intellectual journals 2026", 1, 1) in queries
    # fetch_article_text MUST be filtered out.
    assert all(p != "fetch_article_text" for p, _, _, _ in queries)


def test_queries_distinguishes_ok_from_failed_calls(skill_body):
    events = [
        {"event": "tool_call", "provider": "serper", "query": "x", "ok": True},
        {"event": "tool_call", "provider": "serper", "query": "x", "ok": False},
        {"event": "tool_call", "provider": "serper", "query": "x", "ok": True},
    ]
    queries = skill_body._queries_for_sector_hosts(events, hosts=[])
    # 3 calls total, 2 ok.
    assert queries == [("serper", "x", 3, 2)]


def test_render_query_block_no_events(skill_body):
    out = skill_body._render_query_block(queries=[], stuck_count=0, days=14)
    assert "No `tool_call` telemetry events" in out
    assert "JEEVES_TELEMETRY=1" in out


def test_render_query_block_high_stuck_suggests_rotation(skill_body):
    queries = [("serper", "ai_systems autonomous research", 12, 12)]
    out = skill_body._render_query_block(
        queries=queries, stuck_count=5, days=14,
    )
    assert "Suggested rewrite" in out
    # Without urls_returned correlation, suggestion falls back to general guidance.
    assert "highest-call" in out or "rotate" in out.lower()
    assert "ai_systems autonomous research" in out


def test_render_query_block_zero_stuck_signals_healthy(skill_body):
    queries = [("serper", "x", 3, 3)]
    out = skill_body._render_query_block(
        queries=queries, stuck_count=0, days=14,
    )
    assert "0 stuck URLs" in out
    assert "No rotation needed" in out


def test_render_query_block_truncates_long_queries(skill_body):
    long_query = "x" * 200
    queries = [("exa", long_query, 1, 1)]
    out = skill_body._render_query_block(
        queries=queries, stuck_count=0, days=14,
    )
    # Truncated to 100 chars + ellipsis (101 chars total in the table cell).
    assert long_query not in out  # full version not present
    assert "x" * 97 + "…" in out


def test_queries_returning_stuck_urls_correlates_precisely(skill_body):
    """Per-query stuck-URL correlation reads urls_returned from tool_call events."""
    events = [
        {"event": "tool_call", "provider": "serper", "query": "DOVA arxiv 2026",
         "ok": True, "urls_returned": ["https://arxiv.org/abs/2603.13327", "https://other.com/x"]},
        {"event": "tool_call", "provider": "serper", "query": "DOVA arxiv 2026",
         "ok": True, "urls_returned": ["https://arxiv.org/abs/2603.13327"]},
        {"event": "tool_call", "provider": "exa", "query": "narrow query",
         "ok": True, "urls_returned": ["https://fresh.com/new"]},
    ]
    stuck = ["https://arxiv.org/abs/2603.13327"]
    corr = skill_body._queries_returning_stuck_urls(events, stuck)
    assert corr[0] == ("serper", "DOVA arxiv 2026", 2)
    # exa "narrow query" returned no stuck URL → not in result.
    assert all(q != "narrow query" for _p, q, _h in corr)


def test_queries_returning_stuck_urls_empty_inputs(skill_body):
    assert skill_body._queries_returning_stuck_urls([], ["x"]) == []
    assert skill_body._queries_returning_stuck_urls(
        [{"event": "tool_call", "provider": "serper", "query": "q",
          "ok": True, "urls_returned": ["x"]}],
        [],
    ) == []


def test_queries_returning_stuck_urls_handles_arxiv_canonicalisation(skill_body):
    """Stuck-list contains canonical abs/ID; events return pdf/IDvN — must match."""
    events = [
        {"event": "tool_call", "provider": "exa", "query": "ai_systems",
         "ok": True, "urls_returned": ["https://arxiv.org/pdf/2603.13327v2.pdf"]},
    ]
    stuck = ["https://arxiv.org/abs/2603.13327"]
    corr = skill_body._queries_returning_stuck_urls(events, stuck)
    assert corr == [("exa", "ai_systems", 1)]


def test_render_query_block_with_correlation_names_top_query(skill_body):
    queries = [("serper", "high call", 12, 12), ("exa", "low call", 1, 1)]
    correlation = [("serper", "high call", 5)]
    out = skill_body._render_query_block(
        queries=queries, stuck_count=5, days=14,
        stuck_query_correlation=correlation,
    )
    assert "Stuck-URL correlation" in out
    assert "high call" in out
    assert "rotate it FIRST" in out


def test_render_query_block_falls_back_when_no_correlation(skill_body):
    """Without urls_returned telemetry, suggestion notes the missing data."""
    queries = [("serper", "q", 12, 12)]
    out = skill_body._render_query_block(
        queries=queries, stuck_count=5, days=14,
        stuck_query_correlation=None,
    )
    assert "urls_returned` telemetry is" in out
    assert "patched 2026-05-09" in out


def test_walk_tool_call_events_skips_non_tool_call_events(skill_body, tmp_path, monkeypatch):
    today = date(2026, 5, 9)
    monkeypatch.setattr(skill_body, "_utc_today", lambda: today)
    p = tmp_path / f"telemetry-{today.isoformat()}.jsonl"
    p.write_text(
        '{"event": "tool_call", "provider": "serper", "query": "a"}\n'
        '{"event": "llm_call", "provider": "groq"}\n'  # different event type — skip
        '{"event": "tool_call", "provider": "exa", "query": "b"}\n'
        'malformed\n',
        encoding="utf-8",
    )
    events = skill_body._walk_tool_call_events(tmp_path, days=1)
    assert len(events) == 2
    assert {e["provider"] for e in events} == {"serper", "exa"}


# ============================================================================
# tokens_per_call_report
# ============================================================================

def test_percentile_simple(tokens_report):
    assert tokens_report._percentile([1, 2, 3, 4, 5], 50) == 3
    assert tokens_report._percentile([1, 2, 3, 4, 5], 100) == 5
    assert tokens_report._percentile([1, 2, 3, 4, 5], 0) == 1


def test_percentile_empty_list_returns_zero(tokens_report):
    assert tokens_report._percentile([], 50) == 0.0


def test_aggregate_groups_by_provider_model(tokens_report):
    events = [
        {"event": "llm_call", "provider": "groq", "model": "llama-3.3",
         "ok": True, "prompt_tokens": 1000, "completion_tokens": 500,
         "latency_ms": 1200, "label": "part4", "sector": ""},
        {"event": "llm_call", "provider": "groq", "model": "llama-3.3",
         "ok": False, "latency_ms": 800, "label": "part5"},
        {"event": "llm_call", "provider": "nim", "model": "kimi",
         "ok": True, "latency_ms": 5000, "sector": "triadic_ontology"},
        {"event": "tool_call", "provider": "serper", "ok": True, "latency_ms": 200},
        {"event": "tool_call", "provider": "serper", "ok": True, "latency_ms": 350},
    ]
    agg = tokens_report._aggregate(events)
    groq = agg["llm"][("groq", "llama-3.3")]
    assert groq["calls"] == 2
    assert groq["errors"] == 1
    assert sum(groq["prompt_tokens"]) == 1000
    assert dict(groq["labels"]) == {"part4": 1, "part5": 1}
    assert agg["tool"]["serper"]["calls"] == 2


def test_render_markdown_no_events_message(tokens_report):
    md = tokens_report._render_markdown(
        agg={"llm": {}, "tool": {}}, events=[], days=7,
    )
    assert "No `llm_call` events in window" in md
    assert "No `tool_call` events in window" in md


def test_render_markdown_with_events(tokens_report):
    events = [
        {"event": "llm_call", "provider": "groq", "model": "llama-3.3",
         "ok": True, "prompt_tokens": 1000, "completion_tokens": 500,
         "latency_ms": 1200},
    ]
    agg = tokens_report._aggregate(events)
    md = tokens_report._render_markdown(agg=agg, events=events, days=7)
    assert "groq" in md
    assert "llama-3.3" in md
    assert "1000" in md  # sum prompt tokens


def test_walk_telemetry_skips_malformed_lines(tokens_report, tmp_path, monkeypatch):
    today = date(2026, 5, 9)
    monkeypatch.setattr(tokens_report, "_utc_today", lambda: today)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    path = sessions / f"telemetry-{today.isoformat()}.jsonl"
    path.write_text(
        '{"event": "llm_call", "provider": "groq"}\n'
        'not valid json\n'
        '{"event": "tool_call", "provider": "serper"}\n',
        encoding="utf-8",
    )
    events = tokens_report._walk_telemetry(sessions, days=1)
    assert len(events) == 2
    assert {e["provider"] for e in events} == {"groq", "serper"}


# ============================================================================
# emit_llm_call helper smoke test
# ============================================================================

def test_emit_llm_call_no_op_when_telemetry_disabled(tmp_path, monkeypatch):
    """Telemetry disabled → emit_llm_call does NOT write. No exception."""
    from jeeves.tools.telemetry import emit_llm_call
    monkeypatch.delenv("JEEVES_TELEMETRY", raising=False)
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    emit_llm_call(provider="x", prompt_tokens=10, ok=True)
    # No file should have been created.
    assert not list(tmp_path.glob("telemetry-*.jsonl"))


def test_emit_llm_call_writes_when_enabled(tmp_path, monkeypatch):
    from jeeves.tools.telemetry import _LOCK, emit_llm_call
    import jeeves.tools.telemetry as tel
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    # Reset the cached file handle so the new env path takes effect.
    with _LOCK:
        if tel._FH is not None:
            try:
                tel._FH.close()
            except Exception:
                pass
        tel._FH = None
        tel._FH_DATE = ""
    emit_llm_call(
        provider="groq",
        model="llama-3.3-70b",
        label="part4",
        prompt_tokens=1234,
        completion_tokens=567,
        latency_ms=1500.5,
        ok=True,
    )
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert len(files) == 1
    line = files[0].read_text(encoding="utf-8").strip()
    record = json.loads(line)
    assert record["event"] == "llm_call"
    assert record["provider"] == "groq"
    assert record["model"] == "llama-3.3-70b"
    assert record["prompt_tokens"] == 1234
    assert record["completion_tokens"] == 567
    assert record["latency_ms"] == 1500.5
