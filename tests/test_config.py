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


# --- Empty or shapeless config -----------------------------------------------
#
# A config that is not a mapping with a non-empty users list used to die on
# AttributeError ('NoneType' object has no attribute 'get') or a bare KeyError.
# Both callers print the exception text, so the message has to name the file
# and the way out.


@pytest.mark.parametrize("content", [
    "",
    "   \n\n   \n",
    "- one\n- two\n",
    "sync_interval_minutes: 15\n",
    "users: []\n",
    "users:\n",
    "users: not-a-list\n",
])
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_rejects_shapeless_document(_keyring, tmp_path: Path, content: str):
    path = tmp_path / "config.yaml"
    path.write_text(content)

    with pytest.raises(ValueError) as exc:
        load_config(path)

    message = str(exc.value)
    assert str(path) in message
    assert "eufy-sync" in message


# --- Strava client secret ----------------------------------------------------
#
# The Strava API app's client secret used to live in plain text in config.yaml,
# unlike every other secret. It now resolves from the credential store, with
# the YAML value still honored for configs that have not been migrated yet.


def _strava_config(secret: str | None) -> dict:
    strava: dict = {"client_id": "12345"}
    if secret is not None:
        strava["client_secret"] = secret
    return {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "strava": strava,
        }],
    }


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_resolves_strava_secret_from_vault(_keyring, tmp_path: Path):
    from eufy_sync.credentials import store_password

    store_password("default:strava", "vault-secret")
    path = tmp_path / "config.yaml"
    _write(path, _strava_config(None))

    cfg = load_config(path)
    assert cfg.users[0].strava.client_secret == "vault-secret"
    assert cfg.users[0].strava.client_id == "12345"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_falls_back_to_yaml_strava_secret(_keyring, tmp_path: Path):
    """A config that has not been migrated yet must keep working."""
    path = tmp_path / "config.yaml"
    _write(path, _strava_config("yaml-secret"))

    cfg = load_config(path)
    assert cfg.users[0].strava.client_secret == "yaml-secret"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_prefers_vault_strava_secret_over_yaml(_keyring, tmp_path: Path):
    from eufy_sync.credentials import store_password

    store_password("default:strava", "vault-secret")
    path = tmp_path / "config.yaml"
    _write(path, _strava_config("yaml-secret"))

    cfg = load_config(path)
    assert cfg.users[0].strava.client_secret == "vault-secret"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_missing_strava_secret_names_setup_strava(_keyring, tmp_path: Path):
    """--update-password prompts for the Eufy and Garmin account passwords and
    would never fix this, so the message must name --setup-strava instead."""
    path = tmp_path / "config.yaml"
    _write(path, _strava_config(None))

    with pytest.raises(ValueError, match="--setup-strava"):
        load_config(path)


