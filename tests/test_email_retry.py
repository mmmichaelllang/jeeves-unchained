"""Unit tests for jeeves.email SMTP retry behavior (sprint 16 hardening)."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

import pytest

import jeeves.email as email_mod


def _make_send_kwargs(**overrides):
    base = dict(
        to="lang.mc@gmail.com",
        sender="lang.mc@gmail.com",
        subject="Test",
        html="<html><body>x</body></html>",
        app_password="dummy-app-pw",
    )
    base.update(overrides)
    return base


def test_send_html_succeeds_on_first_attempt(monkeypatch):
    """Happy path — single SMTP_SSL call, no retry."""
    smtp_instances = []

    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            smtp_instances.append(self)
            self.logged_in = False
            self.sent_count = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, sender, pw):
            self.logged_in = True

        def send_message(self, msg):
            self.sent_count += 1

    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_mod.time, "sleep", lambda s: None)

    email_mod.send_html(**_make_send_kwargs())
    assert len(smtp_instances) == 1
    assert smtp_instances[0].logged_in
    assert smtp_instances[0].sent_count == 1


def test_send_html_retries_on_transient_failure(monkeypatch):
    """First attempt fails with timeout; second attempt succeeds."""
    call_count = 0

    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            nonlocal call_count
            call_count += 1
            self._fail = call_count == 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, sender, pw):
            if self._fail:
                raise TimeoutError("connection reset")

        def send_message(self, msg):
            pass

    sleep_calls = []
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_mod.time, "sleep", lambda s: sleep_calls.append(s))

    email_mod.send_html(**_make_send_kwargs())
    assert call_count == 2
    # Slept exactly once between attempts 1 and 2.
    assert sleep_calls == [30]


def test_send_html_retries_on_421_try_again(monkeypatch):
    """SMTP 421 'Try again later' is treated as transient and retried."""
    call_count = 0

    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            nonlocal call_count
            call_count += 1
            self._fail = call_count <= 2

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, sender, pw):
            if self._fail:
                raise smtplib.SMTPResponseException(421, b"4.7.0 Try again later")

        def send_message(self, msg):
            pass

    sleep_calls = []
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_mod.time, "sleep", lambda s: sleep_calls.append(s))

    email_mod.send_html(**_make_send_kwargs())
    assert call_count == 3
    # Two backoff sleeps (between attempts 1-2 and 2-3).
    assert sleep_calls == [30, 60]


def test_send_html_does_not_retry_535_auth_failure(monkeypatch):
    """Permanent 535 auth failure must NOT be retried — re-trying gets the
    Gmail account temporarily locked."""
    call_count = 0

    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            nonlocal call_count
            call_count += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, sender, pw):
            raise smtplib.SMTPResponseException(535, b"5.7.8 Username and Password not accepted")

        def send_message(self, msg):
            pass

    sleep_calls = []
    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_mod.time, "sleep", lambda s: sleep_calls.append(s))

    with pytest.raises(smtplib.SMTPResponseException):
        email_mod.send_html(**_make_send_kwargs())
    # Single attempt, no retry, no sleeps.
    assert call_count == 1
    assert sleep_calls == []


def test_send_html_exhausts_all_retries_then_raises(monkeypatch):
    """All max_attempts fail → final exception raised, no silent loss."""
    call_count = 0

    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            nonlocal call_count
            call_count += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, sender, pw):
            raise OSError("network unreachable")

        def send_message(self, msg):
            pass

    monkeypatch.setattr(email_mod.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(email_mod.time, "sleep", lambda s: None)

    with pytest.raises(OSError, match="network unreachable"):
        email_mod.send_html(**_make_send_kwargs(max_attempts=3))
    assert call_count == 3


def test_send_html_raises_smtp_config_error_when_no_password(monkeypatch):
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    with pytest.raises(email_mod.SMTPConfigError):
        email_mod.send_html(
            to="x@y.com", sender="x@y.com", subject="t", html="<x/>", app_password=None,
        )
