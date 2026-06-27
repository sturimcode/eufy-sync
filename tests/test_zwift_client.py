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
