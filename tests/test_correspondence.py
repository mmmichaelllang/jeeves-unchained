"""Unit tests for Phase 4 correspondence helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jeeves.correspondence import (
    _parse_json_array,
    _trim_for_render,
    build_handoff_json,
    build_handoff_text,
    classify_with_kimi,
    fixture_classified,
    fixture_previews,
    load_priority_contacts,
    postprocess_html,
    render_mock_correspondence,
)
from jeeves.gmail import (
    MessagePreview,
    _decode,
    _strip_tags,
    previews_to_classifier_input,
)

REPO = Path(__file__).resolve().parent.parent


def test_priority_contacts_loads_family():
    contacts = load_priority_contacts()
    labels = {c["label"] for c in contacts["household"]}
    assert "Mrs. Lang" in labels
    assert "Andy" in labels


def test_previews_to_classifier_input_shape():
    previews = fixture_previews()
    rows = previews_to_classifier_input(previews)
    assert len(rows) == 5
    assert rows[0]["sender"].startswith("Sarah Lang")
    assert "snippet" in rows[0]


def test_parse_json_array_tolerates_prose():
    raw = 'Here is the classification:\n```json\n[{"id":"m1","classification":"reply_needed"}]\n```\n'
    parsed = _parse_json_array(raw)
    assert parsed == [{"id": "m1", "classification": "reply_needed"}]


def test_parse_json_array_handles_bare_array():
    raw = '[{"id":"m1"},{"id":"m2"}]'
    parsed = _parse_json_array(raw)
    assert len(parsed) == 2


def test_parse_json_array_returns_empty_on_garbage():
    assert _parse_json_array("definitely not json") == []


def test_build_handoff_text_orders_by_severity():
    classified = fixture_classified()
    text = build_handoff_text(classified)
    # escalation items appear before no_action
    esc_pos = text.find("[escalation]")
    no_act_pos = text.find("[no action]")
    assert esc_pos >= 0
    assert no_act_pos > esc_pos


def test_build_handoff_text_tolerates_none_sender():
    """build_handoff_text must not crash when ClassifiedMessage.sender is None."""
    from jeeves.correspondence import ClassifiedMessage

    msg = ClassifiedMessage(
        id="m1",
        classification="no_action",
        priority_contact=False,
        priority_contact_label=None,
        summary="nothing to act on",
        suggested_action="",
        sender=None,  # type: ignore[arg-type]  — mimics a runtime None from Gmail API
    )
    text = build_handoff_text([msg])
    assert "nothing to act on" in text or text == ""  # must not raise


def test_handoff_json_shape():
    classified = fixture_classified()
    handoff = build_handoff_json(classified, fallback_used=False)
    assert handoff["found"] is True
    assert handoff["fallback_used"] is False
    assert "text" in handoff
    assert handoff["counts"]["escalation"] == 2
    assert handoff["counts"]["no_action"] == 1


def test_postprocess_strips_fences_and_detects_flags():
    fenced = "```html\n<!DOCTYPE html><html><body><p>Moving on, Sir. in a vacuum.</p></body></html>\n```"
    html, word_count, profane, banned_words, banned_trans, banned_filler = postprocess_html(fenced)
    assert html.startswith("<!DOCTYPE html>")
    assert "in a vacuum" in banned_words
    assert "Moving on," in banned_trans


def test_mock_correspondence_passes_postprocess():
    classified = fixture_classified()
    html_raw = render_mock_correspondence("2026-04-23", classified)
    html, wc, profane, bw, bt, bf = postprocess_html(html_raw)
    assert html.startswith("<!DOCTYPE html>")
    assert profane == 0  # correspondence has no profane asides
    assert not bw
    assert not bt


def test_render_mock_correspondence_escapes_html_in_user_fields():
    """render_mock_correspondence must html.escape sender and summary to prevent injection."""
    from jeeves.correspondence import ClassifiedMessage

    msg = ClassifiedMessage(
        id="m1",
        classification="reply_needed",
        priority_contact=False,
        priority_contact_label=None,
        summary='Test" onclick="alert(1)',
        suggested_action="",
        sender='Attacker <evil@x.com>" onload="alert(xss)',
    )
    html = render_mock_correspondence("2026-04-23", [msg])
    assert 'onclick="alert' not in html
    assert 'onload="alert' not in html
    assert "&quot;" in html or "&#x27;" in html or "onclick" not in html


def test_classify_with_kimi_batches_previews(monkeypatch):
    from llama_index.core.base.llms.types import ChatMessage

    from jeeves import llm as llm_mod

    previews = [
        MessagePreview(
            thread_id=f"t{i}", message_id=f"m{i}",
            sender=f"sender{i} <s{i}@example.com>", to="me", subject=f"subj {i}",
            date="Wed, 22 Apr 2026 13:42:00 -0700",
            snippet="snip", body_text="bt", unread=False,
        )
        for i in range(75)
    ]
    calls: list[int] = []

    class FakeResp:
        def __init__(self, content: str):
            self.message = type("M", (), {"content": content})()

    class FakeLLM:
        def chat(self, messages: list[ChatMessage]):
            payload = json.loads(messages[-1].content)
            ids = [m["id"] for m in payload["messages"]]
            calls.append(len(ids))
            rows = [
                {"id": mid, "classification": "no_action",
                 "priority_contact": False, "summary": "ok", "suggested_action": ""}
                for mid in ids
            ]
            return FakeResp(json.dumps(rows))

    monkeypatch.setattr(llm_mod, "build_kimi_llm", lambda *a, **kw: FakeLLM())

    out = classify_with_kimi(cfg=None, previews=previews, contacts={"household": []})

    assert calls == [15, 15, 15, 15, 15]
    assert len(out) == 75
    assert {c.id for c in out} == {f"m{i}" for i in range(75)}


def _make_cfg(tmp_path, monkeypatch):
    from jeeves.config import Config

    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-24")
    # Point Config at the tmp tree (repo_root is mutable on the dataclass).
    object.__setattr__(cfg, "repo_root", tmp_path)
    return cfg


def test_load_prior_briefing_text_returns_empty_when_absent(tmp_path, monkeypatch):
    from jeeves.correspondence import _load_prior_briefing_text

    cfg = _make_cfg(tmp_path, monkeypatch)
    assert _load_prior_briefing_text(cfg) == ""


def test_load_prior_briefing_text_strips_html_and_caps(tmp_path, monkeypatch):
    from jeeves.correspondence import _load_prior_briefing_text

    cfg = _make_cfg(tmp_path, monkeypatch)
    (tmp_path / "sessions").mkdir()
    body = (
        "<!DOCTYPE html><html><body>"
        "<h1>📫 Correspondence — Thursday, April 23, 2026</h1>"
        "<p>Good morning, Sir.</p>"
        "<p>The post was <em>thin</em> yesterday.</p>"
        "</body></html>"
    )
    (tmp_path / "sessions" / "correspondence-2026-04-23.local.html").write_text(body)
    out = _load_prior_briefing_text(cfg)
    assert "Good morning, Sir." in out
    assert "<" not in out and ">" not in out
    assert len(out) <= 3000


def test_trim_for_render_drops_no_action_detail():
    classified = fixture_classified()
    trimmed = _trim_for_render(classified)
    no_action = [r for r in trimmed if r["classification"] == "no_action"]
    actionable = [r for r in trimmed if r["classification"] != "no_action"]
    assert no_action, "fixture must contain at least one no_action row"
    for row in no_action:
        assert set(row.keys()) == {"classification", "sender", "subject"}
    for row in actionable:
        assert "summary" in row
        assert "date" in row


def test_sweep_recent_queries_unread_only(monkeypatch):
    from jeeves import gmail as gmail_mod

    captured: list[str] = []

    def fake_list_ids(service, query, max_results=150):
        captured.append(query)
        return []

    monkeypatch.setattr(gmail_mod, "list_message_ids", fake_list_ids)

    gmail_mod.sweep_recent(service=object(), days=45, max_results=50)

    assert captured == ["is:unread newer_than:45d -label:spam -label:promotions"]


def test_classify_with_kimi_empty_previews_short_circuits(monkeypatch):
    from jeeves import llm as llm_mod

    def boom(*a, **kw):
        raise AssertionError("build_kimi_llm should not be called on empty input")

    monkeypatch.setattr(llm_mod, "build_kimi_llm", boom)
    assert classify_with_kimi(cfg=None, previews=[], contacts={}) == []


def test_classify_with_kimi_retries_on_429(monkeypatch):
    """A 429/rate-limit on the first attempt must trigger sleep + retry,
    not propagate as an unhandled exception. (Production crash 2026-05-02.)"""
    from llama_index.core.base.llms.types import ChatMessage

    from jeeves import correspondence as corr_mod
    from jeeves import llm as llm_mod

    sleeps: list[int] = []
    monkeypatch.setattr(corr_mod.time, "sleep", lambda s: sleeps.append(s))

    previews = [
        MessagePreview(
            thread_id="t0", message_id="m0",
            sender="s@example.com", to="me", subject="subj",
            date="Wed, 22 Apr 2026 13:42:00 -0700",
            snippet="snip", body_text="bt", unread=False,
        )
    ]

    class FakeResp:
        def __init__(self, content: str):
            self.message = type("M", (), {"content": content})()

    call_count = {"n": 0}

    class FlakyLLM:
        def chat(self, messages: list[ChatMessage]):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Error code: 429 - Too Many Requests")
            rows = [
                {"id": "m0", "classification": "no_action",
                 "priority_contact": False, "summary": "ok",
                 "suggested_action": ""}
            ]
            return FakeResp(json.dumps(rows))

    monkeypatch.setattr(llm_mod, "build_kimi_llm", lambda *a, **kw: FlakyLLM())

    out = classify_with_kimi(cfg=None, previews=previews, contacts={"household": []})

    assert len(out) == 1
    assert out[0].id == "m0"
    rate_limit_sleeps = [s for s in sleeps if s >= 60]
    assert rate_limit_sleeps, f"expected >=60s sleep on 429; got {sleeps}"
    assert call_count["n"] == 2


def test_classify_with_kimi_inter_batch_sleep(monkeypatch):
    """Successful batches must be separated by a preemptive 15s sleep so NIM
    doesn't 429 on later batches. Skip after the final batch."""
    from llama_index.core.base.llms.types import ChatMessage

    from jeeves import correspondence as corr_mod
    from jeeves import llm as llm_mod

    sleeps: list[int] = []
    monkeypatch.setattr(corr_mod.time, "sleep", lambda s: sleeps.append(s))

    previews = [
        MessagePreview(
            thread_id=f"t{i}", message_id=f"m{i}",
            sender=f"s{i}@example.com", to="me", subject=f"subj {i}",
            date="Wed, 22 Apr 2026 13:42:00 -0700",
            snippet="snip", body_text="bt", unread=False,
        )
        for i in range(45)  # 3 batches of 15
    ]

    class FakeResp:
        def __init__(self, content: str):
            self.message = type("M", (), {"content": content})()

    class FakeLLM:
        def chat(self, messages: list[ChatMessage]):
            payload = json.loads(messages[-1].content)
            rows = [
                {"id": m["id"], "classification": "no_action",
                 "priority_contact": False, "summary": "ok",
                 "suggested_action": ""}
                for m in payload["messages"]
            ]
            return FakeResp(json.dumps(rows))

    monkeypatch.setattr(llm_mod, "build_kimi_llm", lambda *a, **kw: FakeLLM())

    out = classify_with_kimi(cfg=None, previews=previews, contacts={"household": []})

    assert len(out) == 45
    short_sleeps = [s for s in sleeps if s == 15]
    assert len(short_sleeps) == 2, (
        f"expected 2 inter-batch 15s sleeps for 3 batches; got {sleeps}"
    )


def test_gmail_decode_roundtrips():
    import base64
    encoded = base64.urlsafe_b64encode(b"hello world").decode().rstrip("=")
    assert _decode(encoded) == "hello world"


def test_gmail_strip_tags():
    html = "<p>Hello <b>world</b>!</p><script>evil()</script>"
    assert _strip_tags(html) == "Hello world !"


def test_message_preview_dataclass():
    p = MessagePreview(
        thread_id="t", message_id="m", sender="a", to="b", subject="s",
        date="d", snippet="sn", body_text="bt", unread=True,
    )
    assert p.thread_id == "t"
    assert p.unread is True
    assert p.labels == []


# ---- End-to-end dry-run for scripts/correspondence.py ----


@pytest.fixture
def isolated_repo(tmp_path: Path):
    target = tmp_path / "repo"
    target.mkdir()
    for name in ("scripts", "jeeves", "pyproject.toml"):
        (target / name).symlink_to(REPO / name)
    (target / "sessions").mkdir()
    yield target


def _run(isolated_repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["GITHUB_REPOSITORY"] = "test/fixture"
    env["JEEVES_REPO_ROOT"] = str(isolated_repo)
    return subprocess.run(
        [sys.executable, "scripts/correspondence.py", *args],
        cwd=isolated_repo, env=env, capture_output=True, text=True, timeout=60,
    )


def test_correspondence_dry_run_emits_artifacts(isolated_repo: Path):
    result = _run(isolated_repo, "--dry-run", "--date", "2026-04-23")
    assert result.returncode == 0, f"stderr: {result.stderr}"

    json_path = isolated_repo / "sessions" / "correspondence-2026-04-23.local.json"
    html_path = isolated_repo / "sessions" / "correspondence-2026-04-23.local.html"
    assert json_path.exists(), f"stderr: {result.stderr}"
    assert html_path.exists()

    handoff = json.loads(json_path.read_text(encoding="utf-8"))
    assert handoff["found"] is True
    assert "Sarah Lang" in handoff["text"] or "Mrs. Lang" in handoff["text"]

    html = html_path.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")


def test_correspondence_skip_send_requires_keys(isolated_repo: Path):
    env = os.environ.copy()
    env["GITHUB_REPOSITORY"] = "test/fixture"
    env["JEEVES_REPO_ROOT"] = str(isolated_repo)
    env.pop("NVIDIA_API_KEY", None)
    env.pop("GROQ_API_KEY", None)
    result = subprocess.run(
        [sys.executable, "scripts/correspondence.py", "--skip-send", "--use-fixture", "--date", "2026-04-23"],
        cwd=isolated_repo, env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 2
    assert "NVIDIA_API_KEY" in result.stderr or "GROQ_API_KEY" in result.stderr
