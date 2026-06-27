from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from eufy_sync.config import load_config


def _write(path: Path, data: dict) -> None:
    path.write_text(yaml.dump(data))


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_single_user(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert len(cfg.users) == 1
    assert cfg.users[0].name == "default"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_rejects_multiple_users(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [
            {
                "name": "alice",
                "eufy": {"email": "a@example.com", "password": "pw"},
                "garmin": {"email": "a@example.com", "password": "pw"},
            },
            {
                "name": "bob",
                "eufy": {"email": "b@example.com", "password": "pw"},
                "garmin": {"email": "b@example.com", "password": "pw"},
            },
        ],
    })
    with pytest.raises(ValueError, match="single user"):
        load_config(path)


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_parses_customer_id(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw", "customer_id": "cust-42"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id == "cust-42"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_customer_id_defaults_none(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id is None


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_customer_id_coerced_to_str(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw", "customer_id": 12345},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id == "12345"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_with_zwift_only(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "zwift": {"email": "z@example.com", "password": "zwiftpw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].zwift is not None
    assert cfg.users[0].zwift.email == "z@example.com"
    assert cfg.users[0].zwift.password == "zwiftpw"
    assert cfg.users[0].garmin is None
    assert cfg.users[0].strava is None


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_with_all_three_targets(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
            "strava": {"client_id": "12345", "client_secret": "ssec"},
            "zwift": {"email": "z@example.com", "password": "zwiftpw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].garmin is not None
    assert cfg.users[0].strava is not None
    assert cfg.users[0].zwift is not None
