"""Sprint-19 slice E: mine_golden_set.py output-shape tests."""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _write_session(path: Path, date: str, payload: dict) -> None:
    payload = {"date": date, "status": "complete", **payload}
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_mine_skips_outside_window(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.mine_golden_set import mine_sessions

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    _write_session(
        sessions / "session-2024-01-01.json",  # very old
        "2024-01-01",
        {"local_news": [{"urls": ["https://x.com/a", "https://x.com/b"]}]},
    )
    cases = mine_sessions(sessions, days=7)
    assert cases == []


def test_mine_extracts_from_recent_local_news(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.mine_golden_set import mine_sessions, write_yaml

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    today = _today()
    _write_session(
        sessions / f"session-{today}.json",
        today,
        {
            "local_news": [
                {
                    "category": "municipal",
                    "source": "City of Edmonds",
                    "urls": ["https://edmondswa.gov/a", "https://edmondswa.gov/b"],
                }
            ],
            "global_news": [{"urls": ["https://nyt.com/x", "https://bbc.com/y"]}],
        },
    )
    cases = mine_sessions(sessions, days=7)
    by_id = {c["id"]: c for c in cases}
    assert f"{today}_local_news" in by_id
    assert f"{today}_global_news" in by_id
    case = by_id[f"{today}_local_news"]
    assert case["category"] == "local_news"
    assert "Edmonds" in case["query"]
    assert "https://edmondswa.gov/a" in case["golden_urls"]
    assert "https://edmondswa.gov/b" in case["golden_urls"]


def test_mine_drops_sectors_with_under_two_urls(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.mine_golden_set import mine_sessions

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    today = _today()
    _write_session(
        sessions / f"session-{today}.json",
        today,
        {"career": [{"urls": ["https://only-one.com/a"]}]},
    )
    cases = mine_sessions(sessions, days=7)
    assert cases == []


def test_mine_writes_well_formed_yaml(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.mine_golden_set import mine_sessions, write_yaml

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    today = _today()
    _write_session(
        sessions / f"session-{today}.json",
        today,
        {
            "local_news": [
                {"urls": ["https://a.example/1", "https://a.example/2"]}
            ]
        },
    )
    cases = mine_sessions(sessions, days=7)
    out = tmp_path / "fixtures" / "out.yaml"
    write_yaml(cases, out)
    body = out.read_text(encoding="utf-8")
    assert "version: 1" in body
    assert f"id: {today}_local_news" in body
    assert "https://a.example/1" in body


def test_mine_is_deterministic(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts.mine_golden_set import mine_sessions

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    today = _today()
    _write_session(
        sessions / f"session-{today}.json",
        today,
        {
            "local_news": [{"urls": ["https://b.example/1", "https://b.example/2"]}],
            "career": [{"urls": ["https://c.example/1", "https://c.example/2"]}],
        },
    )
    a = mine_sessions(sessions, days=7)
    b = mine_sessions(sessions, days=7)
    assert [c["id"] for c in a] == [c["id"] for c in b]
    assert a == b
