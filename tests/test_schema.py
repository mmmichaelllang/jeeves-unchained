from jeeves.schema import SessionModel, apply_field_caps
from jeeves.testing.mocks import canned_session
from datetime import date


def test_session_round_trip():
    payload = canned_session(date(2026, 4, 23))
    model = SessionModel.model_validate(payload)
    assert model.date == "2026-04-23"
    assert model.newyorker.available is True
    assert len(model.enriched_articles) >= 3


def test_field_caps_truncate_long_text():
    payload = canned_session(date(2026, 4, 23))
    payload["weather"] = "x" * 2000
    payload["newyorker"]["text"] = "y" * 10000
    apply_field_caps(payload)
    assert len(payload["weather"]) <= 800 + len(" [TRUNCATED]")
    assert payload["weather"].endswith("[TRUNCATED]")
    assert len(payload["newyorker"]["text"]) <= 4000 + len(" [TRUNCATED]")
    assert payload["newyorker"]["text"].endswith("[TRUNCATED]")


def test_empty_session_validates():
    model = SessionModel(date="2026-04-23")
    assert model.status == "complete"
    assert model.newyorker.available is False
