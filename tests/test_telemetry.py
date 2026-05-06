"""Sprint-19 slice E: telemetry.emit() smoke tests."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_telemetry(monkeypatch, tmp_path):
    """Force a fresh handle per test, write into tmp_path."""
    from jeeves.tools import telemetry

    telemetry._close()
    monkeypatch.setenv("JEEVES_TELEMETRY_DIR", str(tmp_path))
    yield
    telemetry._close()


def test_emit_disabled_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("JEEVES_TELEMETRY", raising=False)
    from jeeves.tools.telemetry import emit

    emit("tool_call", provider="serper", ok=True)
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files == []


def test_emit_enabled_writes_jsonl(monkeypatch, tmp_path):
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    from jeeves.tools.telemetry import emit

    emit("tool_call", provider="serper", ok=True, latency_ms=42)
    emit("shadow_compare", primary="serper", shadow="jina_search", jaccard=0.5)

    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert len(files) == 1
    lines = files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    rec0 = json.loads(lines[0])
    assert rec0["event"] == "tool_call"
    assert rec0["provider"] == "serper"
    assert rec0["ok"] is True
    assert rec0["latency_ms"] == 42
    assert "ts" in rec0

    rec1 = json.loads(lines[1])
    assert rec1["event"] == "shadow_compare"
    assert rec1["jaccard"] == 0.5


def test_emit_handles_unserialisable_field(monkeypatch, tmp_path):
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    from jeeves.tools.telemetry import emit

    class _Weird:
        def __repr__(self) -> str:
            return "<Weird>"

    emit("oddity", thing=_Weird())
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    rec = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert rec["thing"] == "<Weird>"


def test_emit_swallows_event_with_no_name(monkeypatch, tmp_path):
    monkeypatch.setenv("JEEVES_TELEMETRY", "1")
    from jeeves.tools.telemetry import emit

    emit("")
    files = list(tmp_path.glob("telemetry-*.jsonl"))
    assert files == []
