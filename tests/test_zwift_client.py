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
