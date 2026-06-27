from __future__ import annotations

import time
from unittest.mock import patch

from eufy_sync.config import ZwiftConfig
from eufy_sync.zwift_client import ZwiftClient, _load_tokens, _save_tokens


def _make_config():
    return ZwiftConfig(email="z@example.com", password="pw")


def _make_tokens(expired: bool = False):
    return {
        "access_token": "old" if expired else "valid",
        "refresh_token": "rtok",
        "expires_at": time.time() + (-3600 if expired else 3600),
    }


def test_save_and_load_tokens_roundtrip_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    tokens = _make_tokens()
    _save_tokens(tokens)
    loaded = _load_tokens()
    assert loaded["access_token"] == "valid"
    assert loaded["refresh_token"] == "rtok"


def test_load_tokens_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _load_tokens() is None


from unittest.mock import MagicMock


def test_authenticate_fresh_login_when_no_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "fresh_access",
        "refresh_token": "fresh_refresh",
        "expires_in": 3600,
    }

    client = ZwiftClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response) as mock_post:
        client.authenticate()

    # Called with password grant
    call = mock_post.call_args
    assert call.args[0] == "https://secure.zwift.com/auth/realms/zwift/tokens/access/codes"
    assert call.kwargs["data"]["grant_type"] == "password"
    assert call.kwargs["data"]["username"] == "z@example.com"
    assert call.kwargs["data"]["password"] == "pw"
    assert "Bearer fresh_access" in client._client.headers.get("Authorization", "")
    client.close()


def test_authenticate_refreshes_expired_token(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens(expired=True))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "refreshed",
        "refresh_token": "new_rtok",
        "expires_in": 3600,
    }

    client = ZwiftClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response) as mock_post:
        client.authenticate()

    assert mock_post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert mock_post.call_args.kwargs["data"]["refresh_token"] == "rtok"
    assert "Bearer refreshed" in client._client.headers["Authorization"]
    client.close()


def test_authenticate_falls_back_to_password_if_refresh_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens(expired=True))

    refresh_fail = MagicMock(status_code=401, text="refresh dead")
    fresh_ok = MagicMock(status_code=200)
    fresh_ok.json.return_value = {
        "access_token": "fresh_after_refresh_fail",
        "refresh_token": "fresh_rtok",
        "expires_in": 3600,
    }

    client = ZwiftClient(_make_config())
    with patch.object(client._client, "post", side_effect=[refresh_fail, fresh_ok]):
        client.authenticate()
    assert "Bearer fresh_after_refresh_fail" in client._client.headers["Authorization"]
    client.close()


import httpx
import pytest
from eufy_sync.sync import PermanentSyncError


def test_update_weight_sends_grams_not_kg(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"weight": 80000}

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response) as mock_put:
        client.update_weight(80.0)

    # Weight must be sent in grams, JSON body
    payload = mock_put.call_args.kwargs["json"]
    assert payload["weight"] == 80000
    assert mock_put.call_args.args[0] == "https://us-or-rly101.zwift.com/api/profiles/me"
    client.close()


def test_update_weight_rounds_to_grams(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {}

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response) as mock_put:
        client.update_weight(80.456)

    # 80.456 kg = 80456 g
    assert mock_put.call_args.kwargs["json"]["weight"] == 80456
    client.close()


def test_update_weight_4xx_raises_permanent(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=401, text="unauth")

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response):
        with pytest.raises(PermanentSyncError):
            client.update_weight(80.0)
    client.close()


def test_update_weight_5xx_raises_retryable(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=503, text="busy")

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response):
        with pytest.raises(RuntimeError) as exc_info:
            client.update_weight(80.0)
    assert not isinstance(exc_info.value, PermanentSyncError)
    client.close()


def test_token_status_no_session(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    client = ZwiftClient(_make_config())
    assert client.token_status()["state"] == "no_session"
    client.close()


def test_token_status_valid(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())
    client = ZwiftClient(_make_config())
    status = client.token_status()
    assert status["state"] == "valid"
    client.close()


def test_token_status_refresh_needed(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens(expired=True))
    client = ZwiftClient(_make_config())
    assert client.token_status()["state"] == "refresh_needed"
    client.close()
