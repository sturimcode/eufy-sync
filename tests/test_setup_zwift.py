from __future__ import annotations

import getpass
import warnings
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eufy_sync import credentials
from eufy_sync.cli.maintenance import _disconnect_zwift, _reauth
from eufy_sync.cli.setup import _setup_zwift


def _config(path):
    path.write_text(yaml.safe_dump({
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "garmin": {"email": "g@example.com"},
        }],
    }))


def test_setup_zwift_reuses_probe_credentials_and_writes_no_secret(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _config(path)
    credentials.store_password("default:zwift", "private-value")
    credentials.store_token("zwift_probe", {
        "email": "z@example.com",
        "user_name": "default",
        "password_account": "default:zwift",
    })
    client = MagicMock()

    with patch("eufy_sync.zwift_client.ZwiftClient", return_value=client), \
         patch("builtins.input", side_effect=AssertionError("cached credentials should not prompt")):
        _setup_zwift(path)

    saved = yaml.safe_load(path.read_text())
    assert saved["users"][0]["zwift"] == {"email": "z@example.com"}
    assert "private-value" not in path.read_text()
    assert credentials.get_password("default:zwift") == "private-value"
    client.authenticate.assert_called_once_with(force=False)
    client.check_connection.assert_called_once_with()
    client.close.assert_called_once_with()
    assert "current weight only" in capsys.readouterr().out


def test_setup_zwift_does_not_enable_config_when_validation_fails(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path)
    client = MagicMock()
    client.authenticate.side_effect = RuntimeError("account rejected")
    credentials.store_password("default:zwift", "working-password")
    credentials.store_token("zwift", {"access_token": "working-token"})

    with patch("eufy_sync.zwift_client.ZwiftClient", return_value=client), \
         patch("builtins.input", return_value="z@example.com"), \
         patch("getpass.getpass", return_value="private-value"), \
         patch("sys.stdin.isatty", return_value=True), \
         pytest.raises(SystemExit):
        _setup_zwift(path)

    assert "zwift" not in yaml.safe_load(path.read_text())["users"][0]
    assert credentials.get_password("default:zwift") == "working-password"
    assert credentials.get_token("zwift") == {"access_token": "working-token"}
    client.authenticate.assert_called_once_with(force=True)
    client.check_connection.assert_not_called()
    client.close.assert_called_once_with()


def test_disconnect_zwift_preserves_other_config_and_arbitrary_probe_account(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path)
    config = yaml.safe_load(path.read_text())
    config["users"][0]["zwift"] = {"email": "z@example.com"}
    path.write_text(yaml.safe_dump(config))
    credentials.store_password("default:zwift", "production-password")
    credentials.store_password("probe-account", "probe-password")
    credentials.store_token("zwift", {"access_token": "production-token"})
    credentials.store_token("zwift_probe", {"password_account": "probe-account"})

    _disconnect_zwift(path)

    saved_user = yaml.safe_load(path.read_text())["users"][0]
    assert "zwift" not in saved_user
    assert saved_user["garmin"] == {"email": "g@example.com"}
    vault = credentials._load_vault()
    assert "default:zwift" not in vault["passwords"]
    assert vault["passwords"]["probe-account"] == "probe-password"
    assert "zwift" not in vault["tokens"]
    assert "zwift_probe" not in vault["tokens"]


def test_setup_zwift_ignores_probe_metadata_for_another_user(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _config(path)
    credentials.store_password("other:zwift", "other-password")
    credentials.store_token("zwift_probe", {
        "email": "other@example.com",
        "user_name": "other",
        "password_account": "other:zwift",
    })

    with patch("sys.stdin.isatty", return_value=False), \
         patch("builtins.input", side_effect=AssertionError("must refuse before prompting")), \
         pytest.raises(SystemExit):
        _setup_zwift(path)

    assert "interactive terminal" in capsys.readouterr().out
    assert "zwift" not in yaml.safe_load(path.read_text())["users"][0]


def test_setup_zwift_can_create_a_fresh_zwift_only_install(tmp_path):
    path = tmp_path / "config.yaml"
    zwift_client = MagicMock()
    eufy_client = MagicMock()
    eufy_client.list_profiles.return_value = []

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", side_effect=["e@example.com", "z@example.com"]), \
         patch("getpass.getpass", side_effect=["eufy-password", "zwift-password"]), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift_client), \
         patch("eufy_sync.eufy_client.EufyClient", return_value=eufy_client):
        _setup_zwift(path)

    saved = yaml.safe_load(path.read_text())
    assert saved == {"users": [{
        "name": "default",
        "eufy": {"email": "e@example.com"},
        "zwift": {"email": "z@example.com"},
    }]}
    assert "password" not in path.read_text()
    assert credentials.get_password("default:eufy") == "eufy-password"
    assert credentials.get_password("default:zwift") == "zwift-password"
    zwift_client.authenticate.assert_called_once_with(force=True)
    zwift_client.check_connection.assert_called_once_with()
    eufy_client.authenticate.assert_called_once_with()
    eufy_client.list_profiles.assert_called_once_with()


def test_setup_refuses_a_password_prompt_that_would_echo(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path)

    def unsafe_prompt(_):
        warnings.warn("Password may echo", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("must stop before reading an exposed password")

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="z@example.com"), \
         patch("getpass.getpass", side_effect=unsafe_prompt), \
         pytest.raises(SystemExit):
        _setup_zwift(path)

    assert "zwift" not in yaml.safe_load(path.read_text())["users"][0]


def test_failed_zwift_reauth_keeps_the_previous_session(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path)
    config = yaml.safe_load(path.read_text())
    config["users"][0]["zwift"] = {"email": "z@example.com"}
    path.write_text(yaml.safe_dump(config))
    credentials.store_password("default:zwift", "saved-password")
    credentials.store_token("zwift", {"access_token": "working-token"})
    client = MagicMock()
    client.authenticate.side_effect = RuntimeError("Temporary Zwift login failure (HTTP 503)")

    with patch("eufy_sync.zwift_client.ZwiftClient", return_value=client), pytest.raises(SystemExit):
        _reauth(path, target="zwift")

    client.authenticate.assert_called_once_with(force=True)
    assert credentials.get_token("zwift") == {"access_token": "working-token"}
