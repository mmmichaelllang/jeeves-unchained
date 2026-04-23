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
    html, word_count, profane, banned_words, banned_trans = postprocess_html(fenced)
    assert html.startswith("<!DOCTYPE html>")
    assert "in a vacuum" in banned_words
    assert "Moving on," in banned_trans


def test_mock_correspondence_passes_postprocess():
    classified = fixture_classified()
    html_raw = render_mock_correspondence("2026-04-23", classified)
    html, wc, profane, bw, bt = postprocess_html(html_raw)
    assert html.startswith("<!DOCTYPE html>")
    assert profane >= 5
    assert not bw
    assert not bt


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

    assert calls == [30, 30, 15]
    assert len(out) == 75
    assert {c.id for c in out} == {f"m{i}" for i in range(75)}


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
