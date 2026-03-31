from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

from eufy_garmin_sync.garmin_auth import GarminAuth, GarminSession, TokenPair


def _make_token(access_expires_in: float = 3600, refresh_expires_in: float = 86400 * 365) -> TokenPair:
    now = time.time()
    return TokenPair(
        access_token="access",
        refresh_token="refresh",
        expires_at=now + access_expires_in,
        refresh_expires_at=now + refresh_expires_in,
    )


def test_token_status_valid():
    auth = GarminAuth("test@example.com", "pw")
    token = _make_token(access_expires_in=3600, refresh_expires_in=86400 * 30)
    session = GarminSession(di_token=token)

    with patch.object(auth, "_load_session", return_value=session):
        status = auth.token_status()

    assert status["state"] == "valid"
    assert status["days_remaining"] is not None
    assert status["days_remaining"] >= 29


def test_token_status_refresh_needed():
    auth = GarminAuth("test@example.com", "pw")
    # Access token expired, refresh token still valid
    token = _make_token(access_expires_in=-100, refresh_expires_in=86400 * 30)
    session = GarminSession(di_token=token)

    with patch.object(auth, "_load_session", return_value=session):
        status = auth.token_status()

    assert status["state"] == "refresh_needed"
    assert status["days_remaining"] >= 29


def test_token_status_expired():
    auth = GarminAuth("test@example.com", "pw")
    # Both tokens expired
    token = _make_token(access_expires_in=-100, refresh_expires_in=-100)
    session = GarminSession(di_token=token)

    with patch.object(auth, "_load_session", return_value=session):
        status = auth.token_status()

    assert status["state"] == "expired"
    assert status["days_remaining"] == 0


def test_token_status_no_session():
    auth = GarminAuth("test@example.com", "pw")

    with patch.object(auth, "_load_session", return_value=None):
        status = auth.token_status()

    assert status["state"] == "no_session"
    assert status["days_remaining"] is None
