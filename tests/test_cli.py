from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from eufy_sync.cli.shared import _write_config
from eufy_sync.platform_support.macos import LAUNCH_AGENT_LABEL, LAUNCH_AGENT_PATH, _generate_plist
from eufy_sync.cli.maintenance import (
    _install_launch_agent,
    _uninstall,
    _uninstall_launch_agent,
    _offer_launch_agent,
)


@pytest.fixture
def pin_macos_impl(monkeypatch):
    """Pin the launch-agent dispatch to the macOS implementation.

    maintenance delegates through platform_support._impl(), which resolves the
    active module from platform.system(). On a Windows CI runner these
    macOS-specific tests would otherwise drive the real Windows schtasks path;
    pinning _active (the way test_doctor.py does) keeps them on the macOS module
    regardless of host OS, so the eufy_sync.platform_support.macos.* patches
    below actually take effect."""
    from eufy_sync import platform_support
    from eufy_sync.platform_support import macos
    monkeypatch.setattr(platform_support, "_active", macos)


def test_main_ctrl_c_exits_cleanly(capsys):
    from eufy_sync.cli import app
    with patch.object(app, "_main", side_effect=KeyboardInterrupt):
        with pytest.raises(SystemExit) as exc_info:
            app.main()
    assert exc_info.value.code == 130
    assert "Cancelled" in capsys.readouterr().out


def test_update_password_path_configures_logging(monkeypatch, tmp_path: Path):
    # --update-password reaches a Garmin login, so main() must configure
    # logging first or garminconnect's per-strategy 429 warnings leak through
    # logging's last-resort handler.
    import sys as _sys
    from eufy_sync.cli import app, shared
    calls = []
    monkeypatch.setattr(shared, "_configure_logging", lambda v: calls.append(v))
    missing_config = tmp_path / "missing.yaml"
    monkeypatch.setattr(_sys, "argv",
                        ["eufy-sync", "--update-password", "--config", str(missing_config)])
    with pytest.raises(SystemExit):
        app.main()
    assert calls == [False]


def test_write_config_creates_file_with_restricted_permissions(tmp_path: Path):
    config_path = tmp_path / "subdir" / "config.yaml"
    config = {"users": [{"name": "test"}]}

    _write_config(config_path, config)

    assert config_path.exists()
    # File should be 600 (owner read/write only). Windows reports 666/777
    # regardless of the mode passed, so skip the POSIX-mode check there.
    if os.name != "nt":
        mode = oct(config_path.stat().st_mode)[-3:]
        assert mode == "600"

    # Content should be valid YAML
    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["users"][0]["name"] == "test"


def test_write_config_parent_directory_is_restricted(tmp_path: Path):
    config_path = tmp_path / "secure_dir" / "config.yaml"
    _write_config(config_path, {"test": True})

    # POSIX modes only; Windows does not honor them.
    if os.name != "nt":
        parent_mode = oct(config_path.parent.stat().st_mode)[-3:]
        assert parent_mode == "700"


def test_write_config_overwrites_existing(tmp_path: Path):
    config_path = tmp_path / "config.yaml"

    _write_config(config_path, {"version": 1})
    _write_config(config_path, {"version": 2})

    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["version"] == 2


def test_interrupted_config_write_keeps_previous_file(tmp_path: Path):
    """config.yaml is replaced atomically: a write that dies partway through
    must leave the previous contents intact instead of truncating the file in
    place and stranding the user with an empty config."""
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {"users": [{"name": "default"}]})
    before = config_path.read_bytes()

    with patch("eufy_sync.cli.shared.yaml.dump", side_effect=RuntimeError("disk full")), \
         pytest.raises(RuntimeError):
        _write_config(config_path, {"users": [{"name": "replacement"}]})

    assert config_path.read_bytes() == before
    # No partial temp file left behind (the temp name carries the writer pid).
    leftovers = list(config_path.parent.glob(config_path.name + ".*.tmp"))
    assert leftovers == []


# --- Launch Agent tests ---


def test_generate_plist_points_at_given_program():
    plist = _generate_plist("/home/user/.garmin-sync/eufy-sync-agent")
    assert "/home/user/.garmin-sync/eufy-sync-agent" in plist
    # --headless lives in the wrapper script, not the plist, so the registered
    # program's bytes stay stable across updates.
    assert "--headless" not in plist
    assert LAUNCH_AGENT_LABEL in plist
    assert "StartInterval" in plist
    assert "14400" in plist


def test_write_run_script_creates_executable_wrapper(tmp_path):
    from eufy_sync.platform_support.macos import _write_run_script
    with patch("eufy_sync.cli.shared.DATA_DIR", tmp_path):
        script = _write_run_script("/home/user/.local/bin/eufy-sync")
    assert script == tmp_path / "eufy-sync-agent"
    content = script.read_text()
    assert content.startswith("#!/bin/sh")
    assert 'exec "/home/user/.local/bin/eufy-sync" --headless' in content
    # POSIX modes only; Windows does not honor them.
    if os.name != "nt":
        assert oct(script.stat().st_mode)[-3:] == "755"


def test_write_run_script_rotates_the_log_before_exec(tmp_path):
    # launchd appends to StandardOutPath forever and never rotates, so the
    # wrapper does it: past 1 MB, sync.log moves onto sync.log.1.
    from eufy_sync.cli import shared
    from eufy_sync.platform_support.macos import _write_run_script
    with patch("eufy_sync.cli.shared.DATA_DIR", tmp_path):
        content = _write_run_script("/home/user/.local/bin/eufy-sync").read_text()

    assert f'log="{shared.LOG_FILE}"' in content
    # BSD stat; this script only ever runs on macOS.
    assert '[ "$(stat -f%z "$log" 2>/dev/null || echo 0)" -gt 1048576 ]' in content
    assert 'mv -f "$log" "$log.1"' in content
    # Rotation happens while nothing new has been written, and the exec is
    # outside the if, so a skipped roll still starts the sync.
    assert content.index("mv -f") < content.index("\nfi\n") < content.index("exec ")


def test_write_run_script_rotation_cannot_stop_the_sync(tmp_path):
    # Every step of the roll is best-effort: an unreadable size falls back to 0
    # (no rotation) and a failed move is swallowed, so neither ends the run
    # before the sync starts.
    from eufy_sync.platform_support.macos import _write_run_script
    with patch("eufy_sync.cli.shared.DATA_DIR", tmp_path):
        content = _write_run_script("/home/user/.local/bin/eufy-sync").read_text()

    assert "|| echo 0" in content
    assert 'mv -f "$log" "$log.1" 2>/dev/null || true' in content
    assert content.rstrip().endswith('exec "/home/user/.local/bin/eufy-sync" --headless')


def test_write_run_script_is_byte_stable_across_installs(tmp_path):
    # macOS re-announces background items when the registered file changes;
    # a second install with the same binary must not rewrite the script.
    import os
    from eufy_sync.platform_support.macos import _write_run_script
    with patch("eufy_sync.cli.shared.DATA_DIR", tmp_path):
        first = _write_run_script("/home/user/.local/bin/eufy-sync")
        mtime_before = os.stat(first).st_mtime_ns
        second = _write_run_script("/home/user/.local/bin/eufy-sync")
    assert first == second
    assert os.stat(second).st_mtime_ns == mtime_before
    assert first.read_text() == second.read_text()


def test_generate_plist_contains_log_path():
    # The plist embeds str(shared.LOG_FILE), which conftest points at a tmp
    # path; assert against that actual path rather than a hardcoded
    # forward-slash literal, which would not match Windows separators.
    from eufy_sync.cli import shared
    plist = _generate_plist("/any/path")
    assert str(shared.LOG_FILE) in plist


@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.platform_support.macos.shutil.which", return_value="/home/user/.local/bin/eufy-sync")
@patch("eufy_sync.platform_support.macos.platform.system", return_value="Darwin")
@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
def test_install_launch_agent_writes_plist_and_loads(mock_path, mock_system, mock_which, mock_run, tmp_path, pin_macos_impl):
    mock_path.parent.mkdir = MagicMock()
    mock_path.write_text = MagicMock()

    with patch("eufy_sync.cli.shared.DATA_DIR", tmp_path):
        _install_launch_agent()

    mock_which.assert_called_once_with("eufy-sync")
    mock_path.write_text.assert_called_once()
    plist_content = mock_path.write_text.call_args[0][0]
    # The agent registers the stable wrapper, never the pipx/uv binary, so
    # updates do not re-trigger the macOS background-activity announcement.
    assert str(tmp_path / "eufy-sync-agent") in plist_content
    assert "/home/user/.local/bin/eufy-sync" not in plist_content
    wrapper = tmp_path / "eufy-sync-agent"
    assert 'exec "/home/user/.local/bin/eufy-sync" --headless' in wrapper.read_text()

    # Should call launchctl unload then load
    assert mock_run.call_count == 2
    assert "unload" in mock_run.call_args_list[0][0][0]
    assert "load" in mock_run.call_args_list[1][0][0]


@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.platform_support.macos.shutil.which", return_value="/home/user/.local/bin/eufy-sync")
@patch("eufy_sync.platform_support.macos.platform.system", return_value="Darwin")
@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
def test_install_launch_agent_removes_legacy_wrapper(mock_path, mock_system, mock_which, mock_run, tmp_path, pin_macos_impl):
    """Re-installing must delete the pre-1.7.17 run-sync.sh wrapper so it does
    not linger as an orphan next to the new eufy-sync-agent script."""
    mock_path.parent.mkdir = MagicMock()
    mock_path.write_text = MagicMock()

    legacy = tmp_path / "run-sync.sh"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("#!/bin/sh\nexec old\n")

    with patch("eufy_sync.cli.shared.DATA_DIR", tmp_path):
        _install_launch_agent()

    assert not legacy.exists()
    assert (tmp_path / "eufy-sync-agent").exists()


@patch("eufy_sync.platform_support.macos.platform.system", return_value="Linux")
def test_install_launch_agent_skips_on_linux(mock_system, capsys, pin_macos_impl):
    _install_launch_agent()
    assert "only supported on macOS" in capsys.readouterr().out


@patch("eufy_sync.platform_support.macos.shutil.which", return_value=None)
@patch("eufy_sync.platform_support.macos.platform.system", return_value="Darwin")
def test_install_launch_agent_warns_if_binary_not_found(mock_system, mock_which, capsys, pin_macos_impl):
    _install_launch_agent()
    assert "could not find eufy-sync" in capsys.readouterr().out


@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
def test_uninstall_launch_agent_removes_plist(mock_path, mock_run, pin_macos_impl):
    mock_path.exists.return_value = True
    mock_path.unlink = MagicMock()

    _uninstall_launch_agent()

    assert mock_run.call_count == 1
    assert "unload" in mock_run.call_args[0][0]
    mock_path.unlink.assert_called_once()


@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
def test_uninstall_launch_agent_noop_if_not_installed(mock_path, capsys, pin_macos_impl):
    mock_path.exists.return_value = False

    _uninstall_launch_agent()

    assert "No Launch Agent installed" in capsys.readouterr().out


@patch("eufy_sync.platform_support.macos._install_launch_agent")
@patch("builtins.input", return_value="y")
@patch("eufy_sync.platform_support.macos.sys.stdin")
@patch("eufy_sync.platform_support.macos.platform.system", return_value="Darwin")
def test_offer_launch_agent_installs_on_yes(mock_system, mock_stdin, mock_input, mock_install, pin_macos_impl):
    mock_stdin.isatty.return_value = True

    _offer_launch_agent()

    mock_install.assert_called_once()


@patch("eufy_sync.platform_support.macos._install_launch_agent")
@patch("builtins.input", return_value="n")
@patch("eufy_sync.platform_support.macos.sys.stdin")
@patch("eufy_sync.platform_support.macos.platform.system", return_value="Darwin")
def test_offer_launch_agent_skips_on_no(mock_system, mock_stdin, mock_input, mock_install):
    mock_stdin.isatty.return_value = True

    _offer_launch_agent()

    mock_install.assert_not_called()


@patch("eufy_sync.platform_support.macos._install_launch_agent")
@patch("eufy_sync.platform_support.macos.sys.stdin")
@patch("eufy_sync.platform_support.macos.platform.system", return_value="Darwin")
def test_offer_launch_agent_skips_non_interactive(mock_system, mock_stdin, mock_install):
    mock_stdin.isatty.return_value = False

    _offer_launch_agent()

    mock_install.assert_not_called()


# --- _uninstall keychain cleanup ---


@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.credentials._keyring_available", return_value=True)
@patch("eufy_sync.credentials.delete_token")
@patch("eufy_sync.credentials.delete_password")
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_clears_keychain_for_configured_user_name(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path,
):
    """_uninstall must read the user's name from config, not assume 'default'."""
    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "elias",
            "eufy": {"email": "e@example.com"},
            "garmin": {"email": "g@example.com"},
        }],
    })

    _uninstall(data_dir)

    deleted_accounts = {call.args[0] for call in mock_delete_pw.call_args_list}
    assert "elias:eufy" in deleted_accounts, (
        f"_uninstall should clear keychain for the configured username, got {deleted_accounts}"
    )
    assert "elias:garmin" in deleted_accounts
    # The Strava client secret lives in the vault too, so --uninstall has to
    # name it as well or it survives the uninstall.
    assert "elias:strava" in deleted_accounts


@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.credentials._keyring_available", return_value=True)
@patch("eufy_sync.credentials.delete_password", side_effect=RuntimeError("keychain locked"))
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_survives_locked_keychain(
    mock_input, mock_stdin, mock_delete_pw,
    mock_keyring, mock_run, mock_launch_path, tmp_path, capsys,
):
    """A locked keychain makes the vault clear raise; _uninstall must catch
    that, note it, and still erase the data dir, not abort with a traceback
    and a half-removed install."""
    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    config_path = data_dir / "config.yaml"
    _write_config(config_path, {
        "users": [{"name": "default", "eufy": {"email": "e@example.com"}}],
    })

    _uninstall(data_dir)  # must not raise

    assert not data_dir.exists()
    out = capsys.readouterr().out
    assert "keychain" in out.lower()


@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.credentials._keyring_available", return_value=True)
@patch("eufy_sync.credentials.delete_token")
@patch("eufy_sync.credentials.delete_password")
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_names_uv_for_uv_installs(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path, capsys,
):
    """When eufy-sync is running from a `uv tool install` venv, the
    uninstall hint must tell the user to run `uv tool uninstall`, not the
    always-pipx line - pipx was never involved in a uv install."""
    from eufy_sync.cli.maintenance import _uninstall

    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with patch("eufy_sync.cli.maintenance.sys.executable", "/Users/x/.local/share/uv/tools/eufy-sync/bin/python"):
        _uninstall(data_dir)

    out = capsys.readouterr().out
    assert "uv tool uninstall eufy-sync" in out
    assert "pipx uninstall" not in out


@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.credentials._keyring_available", return_value=True)
@patch("eufy_sync.credentials.delete_token")
@patch("eufy_sync.credentials.delete_password")
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_names_pipx_when_not_uv(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path, capsys,
):
    """A regular pipx install should keep naming pipx as before."""
    from eufy_sync.cli.maintenance import _uninstall

    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with patch("eufy_sync.cli.maintenance.sys.executable", "/Users/x/.local/pipx/venvs/eufy-sync/bin/python"), \
         patch("eufy_sync.cli.maintenance.shutil.which", return_value="/usr/local/bin/pipx"):
        _uninstall(data_dir)

    out = capsys.readouterr().out
    assert "pipx uninstall eufy-sync" in out


@patch("eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH")
@patch("eufy_sync.platform_support.macos.subprocess.run")
@patch("eufy_sync.credentials._keyring_available", return_value=True)
@patch("eufy_sync.credentials.delete_token")
@patch("eufy_sync.credentials.delete_password")
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
def test_uninstall_removes_custom_config_and_db_paths(
    mock_input, mock_stdin, mock_delete_pw, mock_delete_tok,
    mock_keyring, mock_run, mock_launch_path, tmp_path,
):
    """--uninstall must delete a custom --config/--db path, not just the
    files under the default ~/.garmin-sync directory. Before the fix,
    _uninstall(DATA_DIR) always looked at data_dir/config.yaml and
    data_dir/state.db, so custom paths elsewhere on disk were left behind."""
    from eufy_sync.cli.maintenance import _uninstall

    mock_stdin.isatty.return_value = True
    mock_launch_path.exists.return_value = False

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    custom_config = tmp_path / "custom" / "myconfig.yaml"
    custom_db = tmp_path / "custom" / "mystate.db"
    custom_config.parent.mkdir(parents=True)
    custom_config.write_text("users:\n  - name: elias\n    eufy:\n      email: e@example.com\n")
    custom_db.write_text("not a real sqlite file, just needs to exist")

    _uninstall(data_dir, config_path=custom_config, db_path=custom_db)

    assert not custom_config.exists()
    assert not custom_db.exists()

    deleted_accounts = {call.args[0] for call in mock_delete_pw.call_args_list}
    assert "elias:eufy" in deleted_accounts, (
        "keychain cleanup should read the username from the custom config path"
    )


def test_prompt_profile_choice_returns_selected_customer_id():
    from datetime import datetime, timezone
    from unittest.mock import patch
    from eufy_sync.cli.profiles import _prompt_profile_choice
    from eufy_sync.eufy_client import EufyProfile
    profiles = [
        EufyProfile("cid-a", datetime(2026, 6, 1, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 2, tzinfo=timezone.utc), 62.0),
    ]
    with patch("builtins.input", return_value="2"):
        assert _prompt_profile_choice(profiles) == "cid-b"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_select_profile_writes_chosen_customer_id(_keyring, tmp_path: Path):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch
    from eufy_sync.cli.profiles import _select_profile
    from eufy_sync.cli.shared import _write_config
    from eufy_sync.eufy_client import EufyProfile

    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })

    fake = MagicMock()
    fake.list_profiles.return_value = [
        EufyProfile("cid-a", datetime(2026, 6, 2, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 1, tzinfo=timezone.utc), 62.0),
    ]

    with patch("eufy_sync.eufy_client.EufyClient", return_value=fake), \
         patch("builtins.input", return_value="1"):
        _select_profile(cfg_path)

    written = yaml.safe_load(cfg_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-a"


def test_prompt_profile_choice_retries_on_invalid_input():
    from datetime import datetime, timezone
    from unittest.mock import patch
    from eufy_sync.cli.profiles import _prompt_profile_choice
    from eufy_sync.eufy_client import EufyProfile
    profiles = [
        EufyProfile("cid-a", datetime(2026, 6, 1, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 2, tzinfo=timezone.utc), 62.0),
    ]
    with patch("builtins.input", side_effect=["abc", "9", "1"]):
        assert _prompt_profile_choice(profiles) == "cid-a"


def test_configure_logging_quiets_garminconnect_when_not_verbose():
    import logging
    from eufy_sync.cli.shared import _configure_logging

    logging.getLogger("garminconnect").setLevel(logging.NOTSET)
    _configure_logging(verbose=False)
    assert logging.getLogger("garminconnect").level == logging.ERROR


def test_configure_logging_keeps_garminconnect_detail_when_verbose():
    import logging
    from eufy_sync.cli.shared import _configure_logging

    logging.getLogger("garminconnect").setLevel(logging.ERROR)
    _configure_logging(verbose=True)
    assert logging.getLogger("garminconnect").level == logging.DEBUG


def test_save_customer_id_writes_into_config(tmp_path: Path):
    from eufy_sync.cli.profiles import _save_customer_id
    from eufy_sync.cli.shared import _write_config

    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })

    _save_customer_id(cfg_path, "cid-xyz")

    written = yaml.safe_load(cfg_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-xyz"
    # Existing fields are left intact.
    assert written["users"][0]["eufy"]["email"] == "e@example.com"
    assert written["users"][0]["name"] == "default"


def _ambiguous_profiles():
    from datetime import datetime, timezone
    from eufy_sync.eufy_client import EufyProfile
    return [
        EufyProfile("cid-human", datetime(2026, 6, 27, tzinfo=timezone.utc), 88.0),
        EufyProfile("cid-pet", datetime(2026, 4, 4, tzinfo=timezone.utc), 4.5),
    ]


def _write_synced_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    return config_path


@patch("eufy_sync.credentials._keyring_available", return_value=True)
def test_migration_overwrites_stale_keychain_entry_with_yaml_value(_keyring, tmp_path):
    """A user who corrects a password in the YAML file must have that new
    value win, even if the keychain already has a (now-stale) entry for the
    same account. The old behavior only stored to the keychain when nothing
    was there yet, then deleted the YAML key regardless - silently keeping
    the stale keychain value."""
    from eufy_sync.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "new-corrected-password"},
        }],
    })

    with patch("eufy_sync.credentials.get_password", return_value="stale-old-password"), \
         patch("eufy_sync.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    mock_store.assert_called_once_with("default:eufy", "new-corrected-password")

    written = yaml.safe_load(config_path.read_text())
    assert "password" not in written["users"][0]["eufy"]


@patch("eufy_sync.credentials._keyring_available", return_value=True)
def test_migration_skips_env_var_reference_passwords(_keyring, tmp_path):
    """A password of the form ${VAR_NAME} is a deliberate env-var reference
    (resolved later by config.py's interpolation), not a literal secret. The
    migration must leave it untouched in the YAML and must not store the
    literal placeholder string into the keychain."""
    from eufy_sync.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "${EUFY_PASSWORD}"},
        }],
    })

    with patch("eufy_sync.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    mock_store.assert_not_called()

    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"]["password"] == "${EUFY_PASSWORD}"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_first_run_setup_keeps_the_strava_secret_out_of_the_yaml(_keyring, tmp_path):
    """A fresh install must never write the client secret to config.yaml -
    otherwise new users land in exactly the state the migration exists to
    clean up."""
    from eufy_sync.cli.setup import _first_run_setup
    from eufy_sync.credentials import get_password

    config_path = tmp_path / "config.yaml"
    answers = ["e@example.com", "n", "y", "12345", "sekrit"]

    with patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", return_value="eufy-pw"), \
         patch("eufy_sync.eufy_client.EufyClient", side_effect=RuntimeError("offline")), \
         patch("eufy_sync.strava_client.authorize_strava", return_value={}):
        _first_run_setup(config_path)

    assert get_password("default:strava") == "sekrit"
    assert get_password("default:eufy") == "eufy-pw"
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["strava"] == {"client_id": "12345"}


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_migration_moves_strava_client_secret_to_the_vault(_keyring, tmp_path):
    """The Strava API client secret used to be the one secret left in plain
    text in config.yaml. The migration must move it into the credential store
    and delete the key, the same as the account passwords."""
    from eufy_sync.cli.setup import _migrate_config_passwords
    from eufy_sync.credentials import get_password

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "strava": {"client_id": "12345", "client_secret": "s3cr3t"},
        }],
    })

    _migrate_config_passwords(config_path)

    assert get_password("default:strava") == "s3cr3t"
    written = yaml.safe_load(config_path.read_text())
    assert "client_secret" not in written["users"][0]["strava"]
    # The public client id stays in the YAML.
    assert written["users"][0]["strava"]["client_id"] == "12345"


@patch("eufy_sync.credentials._keyring_available", return_value=True)
def test_migration_skips_env_var_reference_strava_secret(_keyring, tmp_path):
    """A ${VAR} client secret is a deliberate env-var reference, resolved by
    config.py's interpolation. Storing the literal placeholder would win over
    the YAML from then on and break the setup."""
    from eufy_sync.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "strava": {"client_id": "12345", "client_secret": "${STRAVA_SECRET}"},
        }],
    })

    with patch("eufy_sync.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    mock_store.assert_not_called()

    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["strava"]["client_secret"] == "${STRAVA_SECRET}"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_migration_survives_a_bare_eufy_section(_keyring, tmp_path):
    """A hand-edited `eufy:` with nothing indented under it parses to None,
    and the {} default in user.get(service, {}) only covers a missing key. The
    migration runs on every command, so an AttributeError here took the whole
    tool down at startup."""
    from eufy_sync.cli.setup import _migrate_config_passwords
    from eufy_sync.credentials import get_password

    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "users:\n"
        "  - name: default\n"
        "    eufy:\n"
        "    garmin:\n"
        "      email: g@example.com\n"
        "      password: pw\n"
    )

    _migrate_config_passwords(config_path)

    # The rest of the user migrated normally, and the bare section is left as
    # the user wrote it.
    assert get_password("default:garmin") == "pw"
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"] is None
    assert "password" not in written["users"][0]["garmin"]


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_migration_survives_a_bare_strava_section(_keyring, tmp_path):
    """Same shape on the newer Strava client-secret lookup."""
    from eufy_sync.cli.setup import _migrate_config_passwords

    config_path = tmp_path / "config.yaml"
    original = (
        "users:\n"
        "  - name: default\n"
        "    eufy:\n"
        "      email: e@example.com\n"
        "    strava:\n"
    )
    config_path.write_text(original)

    with patch("eufy_sync.credentials.store_password") as mock_store:
        _migrate_config_passwords(config_path)

    # Nothing to move, so nothing is stored and the file is not rewritten.
    mock_store.assert_not_called()
    assert config_path.read_text() == original


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_setup_strava_stores_secret_in_vault_and_strips_yaml_key(_keyring, tmp_path):
    """--setup-strava writes only the client id to the YAML, and clears any
    secret an earlier version already wrote there."""
    from eufy_sync.cli.setup import _setup_strava
    from eufy_sync.credentials import get_password

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com"},
            "strava": {"client_id": "old-id", "client_secret": "old-plaintext"},
        }],
    })

    with patch("builtins.input", side_effect=["12345", "new-secret"]), \
         patch("eufy_sync.strava_client.authorize_strava", return_value={}):
        _setup_strava(config_path)

    assert get_password("default:strava") == "new-secret"
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["strava"] == {"client_id": "12345"}


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_reauth_strava_resolves_secret_from_vault(_keyring, tmp_path):
    """_reauth built StravaConfig straight from the YAML, so after the
    migration removed the key it would have raised KeyError."""
    from eufy_sync.cli.maintenance import _reauth
    from eufy_sync.credentials import store_password

    store_password("default:strava", "vault-secret")
    config = {
        "users": [{
            "name": "default",
            "strava": {"client_id": "12345"},
        }],
    }

    with patch("eufy_sync.strava_client.authorize_strava") as mock_auth:
        _reauth(tmp_path / "config.yaml", config=config, target="strava")

    assert mock_auth.call_args.args[0].client_secret == "vault-secret"


def test_setup_strava_exits_cleanly_on_oauth_failure(tmp_path, capsys):
    """A Strava OAuth failure (timeout, denial, occupied port) must print the
    error message and exit 1 - not escape as a raw traceback."""
    from eufy_sync.cli.setup import _setup_strava

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
        }],
    })

    with patch("builtins.input", side_effect=["12345", "secret"]), \
         patch("eufy_sync.strava_client.authorize_strava",
               side_effect=RuntimeError("Strava authorization timed out")), \
         pytest.raises(SystemExit) as exc:
        _setup_strava(config_path)

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Strava authorization timed out" in out
    assert "eufy-sync --setup-strava" in out


def test_reauth_strava_exits_cleanly_on_oauth_failure(capsys):
    """The Strava branch of _reauth must also catch OAuth failures instead
    of letting them escape as a raw traceback."""
    from eufy_sync.cli.maintenance import _reauth

    config = {
        "users": [{
            "name": "default",
            "strava": {"client_id": "12345", "client_secret": "secret"},
        }],
    }

    with patch("eufy_sync.strava_client.authorize_strava",
               side_effect=OSError("Address already in use")), \
         pytest.raises(SystemExit) as exc:
        _reauth(Path("/nonexistent"), config=config, force=True, target="strava")

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Address already in use" in out
    assert "eufy-sync --reauth strava" in out


def test_reauth_confirmation_on_non_tty_defaults_to_no():
    """The prompt's documented default is No ([y/N]). On a non-tty run there
    is no human to answer, so it must skip re-auth rather than proceeding as
    if 'yes' had been typed - that would destroy a valid token unattended."""
    from eufy_sync.cli.maintenance import _reauth

    config = {
        "users": [{
            "name": "default",
            "garmin": {"email": "g@example.com"},
        }],
    }

    mock_auth = MagicMock()
    mock_auth.token_status.return_value = {"state": "valid"}

    with patch("eufy_sync.garmin_auth.GarminAuth", return_value=mock_auth), \
         patch("eufy_sync.config._get_password", return_value="pw"), \
         patch("eufy_sync.cli.maintenance.sys.stdin") as mock_stdin:
        mock_stdin.isatty.return_value = False
        _reauth(Path("/nonexistent"), config=config, force=True)

    mock_auth.force_reauth.assert_not_called()


def test_status_with_no_config_exits_without_wizard(tmp_path, capsys):
    """eufy-sync --status with no config must never launch the setup
    wizard - it should print a plain message and exit 1, matching the
    pattern used by --setup-strava, --select-profile, etc."""
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called for --status with no config")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--status"]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert not config_path.exists()
    out = capsys.readouterr().out
    assert "No config found. Run eufy-sync first to set up." in out
    assert "first time setup" not in out


def test_history_with_no_config_exits_without_wizard(tmp_path, capsys):
    """Same guard as --status must apply to --history."""
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called for --history with no config")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--history"]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert not config_path.exists()
    out = capsys.readouterr().out
    assert "No config found. Run eufy-sync first to set up." in out
    assert "first time setup" not in out


def test_status_with_corrupt_db_exits_cleanly(tmp_path):
    """If SyncState construction raises (locked keychain, corrupt DB file),
    --status must print a plain one-line error and exit 1 - not a raw
    traceback. This is the startup-guard pattern extended to the
    --status/--history handlers, which currently sit outside it."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--status"]
    # The migration is patched out so this test exercises only the corrupt-db
    # path; an earlier version left it running and it wrote fake passwords
    # into the real credentials file (now also blocked by conftest).
    with patch("sys.argv", argv), \
         patch("eufy_sync.credentials._keyring_available", return_value=False), \
         patch("eufy_sync.cli.setup._migrate_config_passwords"), \
         patch("eufy_sync.state.SyncState", side_effect=OSError("disk I/O error")), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1


def test_upgrade_notice_file_is_hermetic(tmp_path):
    """setup.py freezes UPGRADE_NOTICE_FILE from shared.DATA_DIR at import
    time, so patching shared.DATA_DIR alone is not enough; the autouse
    fixture must repoint the constant itself or _show_upgrade_notice writes
    to the real ~/.garmin-sync."""
    from eufy_sync.cli import setup

    assert str(setup.UPGRADE_NOTICE_FILE).startswith(str(tmp_path))


@patch("eufy_sync.platform_support.notify")
def test_headless_first_run_refuses_wizard(mock_notify, tmp_path):
    """A headless run with no config must never call input() - it should
    print guidance, notify, and exit 1 instead of hanging in the wizard."""
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called in headless first-run")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    assert not config_path.exists()
    mock_notify.assert_called()


@patch("eufy_sync.platform_support.notify")
def test_startup_failure_before_harness_notifies_and_exits(mock_notify, tmp_path):
    """A load_config failure (e.g. missing keychain entry -> ValueError) that
    happens before the sync try/except harness must still notify and exit 1,
    not escape as a raw traceback."""
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    config_path.write_text("users:\n  - name: default\n    eufy:\n      email: e@example.com\n")
    db_path = tmp_path / "state.db"

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path)]
    with patch("sys.argv", argv), \
         patch("eufy_sync.cli.setup._migrate_config_passwords"), \
         patch("eufy_sync.cli.setup._show_upgrade_notice"), \
         patch("eufy_sync.config.load_config", side_effect=ValueError("no password found")), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    mock_notify.assert_called()


@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.cli.app.sys.stdin")
def test_dry_run_does_not_notify_and_prints_preview_summary(
    mock_stdin, _keyring, _migrate, _notice, _updates, mock_notify, tmp_path, capsys
):
    """--dry-run must not fire the success notification or claim a real
    'Synced N' summary - it should print an honest preview summary instead."""
    from eufy_sync.cli.app import main

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        assert kwargs.get("dry_run") is True
        return {"garmin": 2}, {}

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--dry-run"]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    mock_notify.assert_not_called()

    out = capsys.readouterr().out
    assert "Synced" not in out
    assert "[DRY RUN] Would sync 2 measurements to Garmin." in out


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_headless_transient_failure_silent_until_threshold(
    _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path
):
    """A scheduled run that only hit a network blip must not fire a 'failed'
    notification. Only the third consecutive network failure escalates."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        raise RuntimeError("[Errno 8] nodename nor servname provided, or not known")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]

    def run_once():
        with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
             patch("sys.argv", argv), \
             pytest.raises(SystemExit) as exc:
            main()
        return exc.value.code

    assert run_once() == 1
    assert run_once() == 1
    mock_notify.assert_not_called()  # first two blips stay silent

    assert run_once() == 1
    assert mock_notify.call_count == 1  # third escalates, once
    title, msg = mock_notify.call_args[0][0], mock_notify.call_args[0][1]
    assert "network" in title.lower()
    assert "waiting" in msg.lower()


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_headless_success_clears_network_streak(
    _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    """A clean run resets the streak so a later isolated blip starts from zero,
    not one short of escalating."""
    from eufy_sync.cli.app import main
    from eufy_sync.cli import shared

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]
    streak_file = shared.DATA_DIR / "network_fail_streak.json"

    def run_with(side):
        with patch("eufy_sync.sync.sync_user", side_effect=side), \
             patch("sys.argv", argv), \
             pytest.raises(SystemExit):
            main()

    boom = RuntimeError("_ssl.c:1063: The handshake operation timed out")
    run_with(boom)
    run_with(boom)
    assert streak_file.exists()  # streak building

    run_with(lambda user, state, **kw: ({"garmin": 1}, {}))  # clean run
    assert not streak_file.exists()  # cleared on success


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_per_target_upload_error_still_reaches_the_classifier(
    _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path
):
    """A dead Garmin session mid-upload is now contained inside sync_user and
    reported through the errors dict instead of raising. The message text must
    survive that trip, or the run ends on the generic 'failed' toast instead of
    the actionable re-login one."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        return {"strava": 2}, {
            "garmin": "Garmin wants an MFA code and no one is here to type it. Run: eufy-sync --reauth garmin",
        }

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    mock_notify.assert_any_call(
        "eufy-sync: re-login needed", "Run: eufy-sync --reauth garmin",
        command="eufy-sync --reauth garmin",
    )


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.cli.app.sys.stdin")
def test_interactive_transient_failure_notifies_immediately(
    mock_stdin, _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path
):
    """The silence is only for unattended runs. An interactive run surfaces a
    network failure right away, as before."""
    from eufy_sync.cli.app import main

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    def fake_sync_user(user, state, **kwargs):
        raise RuntimeError("[Errno 8] nodename nor servname provided, or not known")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path)]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit):
        main()

    mock_notify.assert_called_once()
    assert "failed" in mock_notify.call_args[0][0].lower()


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.cli.app.sys.stdin")
def test_interactive_ambiguous_profile_resolves_and_syncs(
    mock_stdin, _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    from eufy_sync.cli.app import main
    from eufy_sync.eufy_client import AmbiguousProfileError

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    profiles = _ambiguous_profiles()
    seen_customer_ids = []

    def fake_sync_user(user, state, **kwargs):
        seen_customer_ids.append(user.eufy.customer_id)
        if len(seen_customer_ids) == 1:
            raise AmbiguousProfileError(profiles)
        return {"garmin": 1}, {}

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path)]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         patch("builtins.input", return_value="1"), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    # The chosen (human) profile was persisted to config.
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-human"
    # The sync was retried in-process with that customer_id set in memory.
    assert seen_customer_ids == [None, "cid-human"]


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.cli.app.sys.stdin")
def test_noninteractive_ambiguous_profile_bails(
    mock_stdin, _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path, capsys
):
    from eufy_sync.cli.app import main
    from eufy_sync.eufy_client import AmbiguousProfileError

    mock_stdin.isatty.return_value = False  # no human present
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    profiles = _ambiguous_profiles()

    def fake_sync_user(user, state, **kwargs):
        raise AmbiguousProfileError(profiles)

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called with no TTY present")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path)]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "eufy-sync --select-profile" in out
    _notify.assert_any_call(
        "eufy-sync: choose your profile", "Run: eufy-sync --select-profile",
        command="eufy-sync --select-profile",
    )


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_repair_days_reaches_sync_user(
    _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    seen = {}

    def fake_sync_user(user, state, **kwargs):
        seen.update(kwargs)
        return {"garmin": 3}, {}

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--repair-days", "14"]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert seen["repair_days"] == 14


def test_repair_days_with_backfill_days_is_rejected(tmp_path, capsys):
    """The two set the same fetch window from different intents; taking both
    would silently honor one of them."""
    from eufy_sync.cli.app import main

    argv = ["eufy-sync", "--repair-days", "14", "--backfill-days", "30"]
    with patch("sys.argv", argv), pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "--repair-days and --backfill-days cannot be used together" in capsys.readouterr().err


def test_dunder_version_matches_pyproject():
    """The version lives in two places (pyproject.toml for packaging,
    eufy_sync.__version__ for --version and the update checker). 1.7.20
    shipped with the two out of sync, which made every up-to-date install
    nag about a phantom update. This keeps them locked together."""
    import tomllib
    from pathlib import Path

    import eufy_sync

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        declared = tomllib.load(f)["project"]["version"]
    assert eufy_sync.__version__ == declared
