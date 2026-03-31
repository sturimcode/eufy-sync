from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

from eufy_garmin_sync.cli import (
    _write_config,
    _generate_plist,
    _install_launch_agent,
    _uninstall_launch_agent,
    _offer_launch_agent,
    LAUNCH_AGENT_LABEL,
    LAUNCH_AGENT_PATH,
)


def test_write_config_creates_file_with_restricted_permissions(tmp_path: Path):
    config_path = tmp_path / "subdir" / "config.yaml"
    config = {"users": [{"name": "test"}]}

    _write_config(config_path, config)

    assert config_path.exists()
    # File should be 600 (owner read/write only)
    mode = oct(config_path.stat().st_mode)[-3:]
    assert mode == "600"

    # Content should be valid YAML
    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["users"][0]["name"] == "test"


def test_write_config_parent_directory_is_restricted(tmp_path: Path):
    config_path = tmp_path / "secure_dir" / "config.yaml"
    _write_config(config_path, {"test": True})

    parent_mode = oct(config_path.parent.stat().st_mode)[-3:]
    assert parent_mode == "700"


def test_write_config_overwrites_existing(tmp_path: Path):
    config_path = tmp_path / "config.yaml"

    _write_config(config_path, {"version": 1})
    _write_config(config_path, {"version": 2})

    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["version"] == 2


# --- Launch Agent tests ---


def test_generate_plist_contains_binary_path():
    plist = _generate_plist("/usr/local/bin/eufy-sync")
    assert "/usr/local/bin/eufy-sync" in plist
    assert "--headless" in plist
    assert LAUNCH_AGENT_LABEL in plist
    assert "StartInterval" in plist
    assert "14400" in plist


def test_generate_plist_contains_log_path():
    plist = _generate_plist("/any/path")
    assert "/tmp/eufy-garmin-sync.log" in plist


@patch("eufy_garmin_sync.cli.subprocess.run")
@patch("eufy_garmin_sync.cli.shutil.which", return_value="/home/user/.local/bin/eufy-sync")
@patch("eufy_garmin_sync.cli.platform.system", return_value="Darwin")
@patch("eufy_garmin_sync.cli.LAUNCH_AGENT_PATH")
def test_install_launch_agent_writes_plist_and_loads(mock_path, mock_system, mock_which, mock_run):
    mock_path.parent.mkdir = MagicMock()
    mock_path.write_text = MagicMock()

    _install_launch_agent()

    mock_which.assert_called_once_with("eufy-sync")
    mock_path.write_text.assert_called_once()
    plist_content = mock_path.write_text.call_args[0][0]
    assert "/home/user/.local/bin/eufy-sync" in plist_content

    # Should call launchctl unload then load
    assert mock_run.call_count == 2
    assert "unload" in mock_run.call_args_list[0][0][0]
    assert "load" in mock_run.call_args_list[1][0][0]


@patch("eufy_garmin_sync.cli.platform.system", return_value="Linux")
def test_install_launch_agent_skips_on_linux(mock_system, capsys):
    _install_launch_agent()
    assert "only supported on macOS" in capsys.readouterr().out


@patch("eufy_garmin_sync.cli.shutil.which", return_value=None)
@patch("eufy_garmin_sync.cli.platform.system", return_value="Darwin")
def test_install_launch_agent_warns_if_binary_not_found(mock_system, mock_which, capsys):
    _install_launch_agent()
    assert "could not find eufy-sync" in capsys.readouterr().out


@patch("eufy_garmin_sync.cli.subprocess.run")
@patch("eufy_garmin_sync.cli.LAUNCH_AGENT_PATH")
def test_uninstall_launch_agent_removes_plist(mock_path, mock_run):
    mock_path.exists.return_value = True
    mock_path.unlink = MagicMock()

    _uninstall_launch_agent()

    assert mock_run.call_count == 1
    assert "unload" in mock_run.call_args[0][0]
    mock_path.unlink.assert_called_once()


@patch("eufy_garmin_sync.cli.LAUNCH_AGENT_PATH")
def test_uninstall_launch_agent_noop_if_not_installed(mock_path, capsys):
    mock_path.exists.return_value = False

    _uninstall_launch_agent()

    assert "No Launch Agent installed" in capsys.readouterr().out


@patch("eufy_garmin_sync.cli._install_launch_agent")
@patch("builtins.input", return_value="y")
@patch("eufy_garmin_sync.cli.sys.stdin")
@patch("eufy_garmin_sync.cli.platform.system", return_value="Darwin")
def test_offer_launch_agent_installs_on_yes(mock_system, mock_stdin, mock_input, mock_install):
    mock_stdin.isatty.return_value = True

    _offer_launch_agent()

    mock_install.assert_called_once()


@patch("eufy_garmin_sync.cli._install_launch_agent")
@patch("builtins.input", return_value="n")
@patch("eufy_garmin_sync.cli.sys.stdin")
@patch("eufy_garmin_sync.cli.platform.system", return_value="Darwin")
def test_offer_launch_agent_skips_on_no(mock_system, mock_stdin, mock_input, mock_install):
    mock_stdin.isatty.return_value = True

    _offer_launch_agent()

    mock_install.assert_not_called()


@patch("eufy_garmin_sync.cli._install_launch_agent")
@patch("eufy_garmin_sync.cli.sys.stdin")
@patch("eufy_garmin_sync.cli.platform.system", return_value="Darwin")
def test_offer_launch_agent_skips_non_interactive(mock_system, mock_stdin, mock_install):
    mock_stdin.isatty.return_value = False

    _offer_launch_agent()

    mock_install.assert_not_called()
