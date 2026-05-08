"""Tests for jeeves.alert — out-of-band pipeline-failure alerts."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import jeeves.alert as alert_mod


def test_send_failure_alert_no_app_password_returns_false(monkeypatch):
    """No GMAIL_APP_PASSWORD => fail-soft, no exception, returns False."""
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    sent = alert_mod.send_failure_alert(
        subject="Test",
        reason="test reason",
        recipient="x@example.com",
    )
    assert sent is False


def test_send_failure_alert_calls_send_html(monkeypatch):
    """Happy path — call send_html with the rendered alert HTML."""
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "dummy-pw")

    captured: dict = {}

    def fake_send_html(*, to, sender, subject, html, app_password, max_attempts):
        captured["to"] = to
        captured["sender"] = sender
        captured["subject"] = subject
        captured["html"] = html
        captured["app_password"] = app_password
        captured["max_attempts"] = max_attempts

    monkeypatch.setattr(alert_mod, "send_html", fake_send_html)

    sent = alert_mod.send_failure_alert(
        subject="OAuth dead",
        reason="invalid_grant",
        details="stack trace here",
        remediation="run gmail_auth.py",
        recipient="lang.mc@example.com",
    )

    assert sent is True
    assert captured["to"] == "lang.mc@example.com"
    assert captured["sender"] == "lang.mc@example.com"
    assert captured["subject"].startswith("[jeeves alert]")
    assert "OAuth dead" in captured["subject"]
    assert "invalid_grant" in captured["html"]
    assert "stack trace here" in captured["html"]
    assert "gmail_auth.py" in captured["html"]
    assert captured["app_password"] == "dummy-pw"
    assert captured["max_attempts"] == 2  # alerts are best-effort


def test_send_failure_alert_swallows_send_exceptions(monkeypatch):
    """If send_html raises, alert returns False instead of bubbling."""
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "dummy-pw")

    def boom(**_):
        raise RuntimeError("smtp went sideways")

    monkeypatch.setattr(alert_mod, "send_html", boom)

    sent = alert_mod.send_failure_alert(
        subject="Test", reason="x", recipient="y@example.com",
    )
    assert sent is False


def test_alert_html_escapes_angle_brackets(monkeypatch):
    """Stack traces commonly contain <module> / <stdin> — must not break HTML."""
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "dummy-pw")
    captured: dict = {}

    def fake_send_html(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(alert_mod, "send_html", fake_send_html)

    alert_mod.send_failure_alert(
        subject="x",
        reason="x",
        details="File <stdin>, line 1, in <module>",
        recipient="y@example.com",
    )
    html = captured["html"]
    assert "&lt;stdin&gt;" in html
    assert "&lt;module&gt;" in html
    # Raw angle brackets in user-supplied content should not appear.
    assert "<stdin>" not in html
    assert "<module>" not in html


def test_render_alert_html_includes_utc_timestamp():
    html = alert_mod._render_alert_html(
        reason="r", details="", remediation="",
    )
    assert "UTC" in html
