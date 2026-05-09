"""Tests for scripts/check_gmail_oauth.py — OAuth refresh-token preflight."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_PATH = REPO_ROOT / "scripts" / "check_gmail_oauth.py"


def _load_module():
    """Import scripts/check_gmail_oauth.py as a module for direct testing."""
    spec = importlib.util.spec_from_file_location(
        "check_gmail_oauth", SCRIPT_PATH,
    )
    assert spec and spec.loader, "could not build spec for check_gmail_oauth"
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_gmail_oauth"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def test_classify_invalid_grant(mod):
    err = RuntimeError("invalid_grant: Token has been expired or revoked.")
    assert mod._classify_refresh_error(err) == mod.EXIT_INVALID_GRANT


def test_classify_other_error(mod):
    err = RuntimeError("503 Service Unavailable from accounts.google.com")
    assert mod._classify_refresh_error(err) == mod.EXIT_OTHER_AUTH


def test_check_token_unparseable_json(mod):
    code, msg = mod.check_token("not-json")
    assert code == mod.EXIT_MISSING_ENV
    assert "not valid JSON" in msg


def test_check_token_invalid_grant_path(mod, monkeypatch):
    """Simulate Google library raising the canonical revoked-token error."""
    fake_token = json.dumps({
        "client_id": "x.apps.googleusercontent.com",
        "client_secret": "y",
        "refresh_token": "1//revoked",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    })

    class FakeCreds:
        token = "old-access"

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            return cls()

        def refresh(self, request):
            raise RuntimeError(
                "invalid_grant: Token has been expired or revoked."
            )

    fake_oauth = type(sys)("google.oauth2")
    fake_oauth_credentials = type(sys)("google.oauth2.credentials")
    fake_oauth_credentials.Credentials = FakeCreds
    fake_transport = type(sys)("google.auth.transport")
    fake_transport_requests = type(sys)("google.auth.transport.requests")
    fake_transport_requests.Request = lambda: object()

    # Patch only the names the script imports inside check_token().
    monkeypatch.setitem(sys.modules, "google.oauth2", fake_oauth)
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", fake_oauth_credentials)
    monkeypatch.setitem(sys.modules, "google.auth.transport", fake_transport)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", fake_transport_requests)

    code, msg = mod.check_token(fake_token)
    assert code == mod.EXIT_INVALID_GRANT
    assert "invalid_grant" in msg


def test_check_token_other_auth_error(mod, monkeypatch):
    fake_token = json.dumps({
        "client_id": "x.apps.googleusercontent.com",
        "client_secret": "y",
        "refresh_token": "1//ok",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    })

    class FakeCreds:
        token = None

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            return cls()

        def refresh(self, request):
            raise RuntimeError("503 backend error")

    fake_oauth_credentials = type(sys)("google.oauth2.credentials")
    fake_oauth_credentials.Credentials = FakeCreds
    fake_transport_requests = type(sys)("google.auth.transport.requests")
    fake_transport_requests.Request = lambda: object()
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", fake_oauth_credentials)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", fake_transport_requests)

    code, msg = mod.check_token(fake_token)
    assert code == mod.EXIT_OTHER_AUTH


def test_check_token_happy_path(mod, monkeypatch):
    fake_token = json.dumps({
        "client_id": "x.apps.googleusercontent.com",
        "client_secret": "y",
        "refresh_token": "1//ok",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    })

    class FakeCreds:
        token = None

        @classmethod
        def from_authorized_user_info(cls, info, scopes):
            return cls()

        def refresh(self, request):
            self.token = "new-access-token"

    fake_oauth_credentials = type(sys)("google.oauth2.credentials")
    fake_oauth_credentials.Credentials = FakeCreds
    fake_transport_requests = type(sys)("google.auth.transport.requests")
    fake_transport_requests.Request = lambda: object()
    monkeypatch.setitem(sys.modules, "google.oauth2.credentials", fake_oauth_credentials)
    monkeypatch.setitem(sys.modules, "google.auth.transport.requests", fake_transport_requests)

    code, msg = mod.check_token(fake_token)
    assert code == mod.EXIT_OK
    assert "ok" in msg.lower()


def test_main_missing_env_returns_4(mod, monkeypatch):
    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_JSON", raising=False)
    rc = mod.main(["--no-alert", "--quiet"])
    assert rc == mod.EXIT_MISSING_ENV
