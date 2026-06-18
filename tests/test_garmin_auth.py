from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.sync import PermanentSyncError

BLOB = {"di_token": "tok", "di_refresh_token": "rtok", "di_client_id": "cid"}


def _auth():
    return GarminAuth("test@example.com", "pw")


def test_login_restores_saved_blob_without_fresh_login(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    fake_garmin = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin) as ctor:
        result = auth.login(interactive=True)
    assert result is fake_garmin
    fake_garmin.login.assert_not_called()       # restored from blob, no fresh login
    assert ctor.call_count == 1
    fake_garmin.client.loads.assert_called_once()


def test_login_fresh_when_no_blob_and_interactive(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    saved = {}
    monkeypatch.setattr(auth, "_save_token", lambda g: saved.setdefault("called", True))
    fake_garmin = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin):
        auth.login(interactive=True)
    fake_garmin.login.assert_called_once()
    assert saved.get("called") is True


def test_login_raises_when_no_blob_and_not_interactive(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    with patch("eufy_sync.garmin_auth.Garmin", return_value=MagicMock()):
        with pytest.raises(PermanentSyncError):
            auth.login(interactive=False)


def test_token_status_valid_with_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    assert auth.token_status()["state"] == "valid"


def test_force_reauth_clears_token_logs_in_and_saves(monkeypatch):
    auth = _auth()
    calls = []
    monkeypatch.setattr(auth, "_clear_token", lambda: calls.append("clear"))
    monkeypatch.setattr(auth, "_save_token", lambda g: calls.append("save"))
    fake_garmin = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin):
        result = auth.force_reauth()
    assert result is fake_garmin
    fake_garmin.login.assert_called_once()
    assert calls == ["clear", "save"]   # cleared before login, saved after


def test_login_falls_back_to_fresh_when_blob_unusable(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake_garmin = MagicMock()
    fake_garmin.client.loads.side_effect = ValueError("corrupt")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin):
        auth.login(interactive=True)
    fake_garmin.login.assert_called_once()   # fell back to fresh login when restore failed


def test_token_status_no_session_without_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    assert auth.token_status()["state"] == "no_session"


def test_load_token_rejects_old_playwright_session(monkeypatch):
    # Old Playwright session stored di_token as a nested dict; must be rejected
    # so the user re-logs in once under the new library.
    auth = _auth()
    monkeypatch.setattr("eufy_sync.credentials._keyring_available", lambda: True)
    monkeypatch.setattr(
        "eufy_sync.credentials.get_token",
        lambda name: {"di_token": {"access_token": "x"}},
    )
    assert auth._load_token() is None


def test_load_token_accepts_new_library_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr("eufy_sync.credentials._keyring_available", lambda: True)
    monkeypatch.setattr("eufy_sync.credentials.get_token", lambda name: dict(BLOB))
    assert auth._load_token() == BLOB


def test_login_rate_limited_raises_clear_error(monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectTooManyRequestsError("rate limited")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=True)
    assert "rate-limiting" in str(exc_info.value)


def test_login_bad_credentials_raises_clear_error(monkeypatch):
    from garminconnect import GarminConnectAuthenticationError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectAuthenticationError("bad creds")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=True)
    assert "--update-password" in str(exc_info.value)


def test_force_reauth_rate_limited_raises_clear_error(monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError
    auth = _auth()
    monkeypatch.setattr(auth, "_clear_token", lambda: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectTooManyRequestsError("rate limited")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError):
            auth.force_reauth()
