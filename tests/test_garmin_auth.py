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


def test_token_status_no_session_without_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    assert auth.token_status()["state"] == "no_session"
