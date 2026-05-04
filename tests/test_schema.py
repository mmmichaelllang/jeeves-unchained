from datetime import date
from pathlib import Path

import pytest

from jeeves.schema import SessionModel, apply_field_caps
from jeeves.testing.mocks import canned_session


def test_session_round_trip():
    payload = canned_session(date(2026, 4, 23))
    model = SessionModel.model_validate(payload)
    assert model.date == "2026-04-23"
    assert model.newyorker.available is True
    assert len(model.enriched_articles) >= 3


def test_field_caps_truncate_long_text():
    payload = canned_session(date(2026, 4, 23))
    payload["weather"] = "x" * 2000
    payload["newyorker"]["text"] = "y" * 50000
    apply_field_caps(payload)
    assert len(payload["weather"]) <= 800 + len(" [TRUNCATED]")
    assert payload["weather"].endswith("[TRUNCATED]")
    assert len(payload["newyorker"]["text"]) <= 40000 + len(" [TRUNCATED]")
    assert payload["newyorker"]["text"].endswith("[TRUNCATED]")


def test_newyorker_text_cap_is_40k():
    """Talk of the Town pieces top out at ~9000 chars; cap raised to 40k."""
    from jeeves.schema import FIELD_CAPS

    assert FIELD_CAPS["newyorker.text"] == 40000


def test_full_tott_article_survives_cap():
    """A realistic ~8000-char Talk of the Town must survive apply_field_caps unmodified."""
    payload = canned_session(date(2026, 4, 23))
    full_article = "The Reverend Billy preached at the gates. " * 200  # ~8400 chars
    payload["newyorker"]["text"] = full_article
    apply_field_caps(payload)
    assert payload["newyorker"]["text"] == full_article
    assert "[TRUNCATED]" not in payload["newyorker"]["text"]


def test_empty_session_validates():
    model = SessionModel(date="2026-04-23")
    assert model.status == "complete"
    assert model.newyorker.available is False


def _make_session_cfg(tmp_path: Path, monkeypatch):
    from jeeves.config import Config
    monkeypatch.setenv("GITHUB_REPOSITORY", "test/fixture")
    cfg = Config.from_env(dry_run=True, run_date="2026-04-23")
    object.__setattr__(cfg, "repo_root", tmp_path)
    return cfg


def test_load_session_raises_on_empty_file(tmp_path: Path, monkeypatch):
    """A zero-byte session file must raise ValueError (corruption — not absent).
    Sprint 16: previously raised FileNotFoundError which made write.py print
    misleading 'no session, run research?' guidance for a file that existed
    but was broken.
    """
    from jeeves.session_io import load_session_by_date

    cfg = _make_session_cfg(tmp_path, monkeypatch)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session-2026-04-23.json").write_text("")

    with pytest.raises(ValueError, match="empty or corrupted"):
        load_session_by_date(cfg, date(2026, 4, 23))


def test_load_session_raises_on_truncated_json(tmp_path: Path, monkeypatch):
    """A truncated session JSON must raise ValueError (corruption)."""
    from jeeves.session_io import load_session_by_date

    cfg = _make_session_cfg(tmp_path, monkeypatch)
    (tmp_path / "sessions").mkdir()
    (tmp_path / "sessions" / "session-2026-04-23.json").write_text(
        '{"date": "2026-04-23", "status": "complete", "truncated'  # deliberate truncation
    )

    with pytest.raises(ValueError, match="empty or corrupted"):
        load_session_by_date(cfg, date(2026, 4, 23))
