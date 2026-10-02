"""Connecting, configuring, reporting on, and disconnecting Intervals.icu.
The client is mocked throughout; no test reaches the real API."""
from __future__ import annotations

import getpass
import warnings
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eufy_sync import credentials
from eufy_sync.cli.maintenance import _disconnect_intervals
from eufy_sync.cli.setup import _migrate_config_passwords, _setup_intervals
from eufy_sync.config import EufyConfig, IntervalsConfig, UserConfig, load_config
from eufy_sync.reporting import update_counts_summary


def _config(path, **extra):
    user = {"name": "default", "eufy": {"email": "e@example.com"}, "garmin": {"email": "g@example.com"}}
    user.update(extra)
    path.write_text(yaml.safe_dump({"users": [user]}))


def _client(athlete_id="i12345"):
    client = MagicMock()
    client.athlete_id.return_value = athlete_id
    return client


# --- --setup-intervals --------------------------------------------------------


def test_setup_checks_the_key_then_saves_it_outside_the_yaml(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _config(path)
    client = _client()

    with patch("eufy_sync.intervals_client.IntervalsClient", return_value=client) as cls, \
         patch("getpass.getpass", return_value="  the-key  "), \
         patch("builtins.input", side_effect=AssertionError("the athlete id is looked up, not asked")):
        _setup_intervals(path)

    config = cls.call_args.args[0]
    assert config == IntervalsConfig(athlete_id="0", api_key="the-key")
    client.athlete_id.assert_called_once_with()
    client.close.assert_called_once_with()
    saved = yaml.safe_load(path.read_text())["users"][0]
    assert saved["intervals"] == {"athlete_id": "i12345"}
    assert saved["garmin"] == {"email": "g@example.com"}
    assert "the-key" not in path.read_text()
    assert credentials.get_password("default:intervals") == "the-key"
    out = capsys.readouterr().out
    assert "athlete i12345" in out
    assert "Intervals.icu connected" in out


def test_setup_replaces_a_key_and_drops_one_written_in_the_yaml(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path, intervals={"athlete_id": "i1", "api_key": "old-plain-key"})
    credentials.store_password("default:intervals", "old-key")

    with patch("eufy_sync.intervals_client.IntervalsClient", return_value=_client("i1")), \
         patch("getpass.getpass", return_value="new-key"):
        _setup_intervals(path)

    assert yaml.safe_load(path.read_text())["users"][0]["intervals"] == {"athlete_id": "i1"}
    assert credentials.get_password("default:intervals") == "new-key"


def test_failed_check_saves_nothing_and_keeps_the_working_key(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _config(path, intervals={"athlete_id": "i1"})
    credentials.store_password("default:intervals", "working-key")
    before = path.read_text()
    client = _client()
    client.athlete_id.side_effect = RuntimeError(
        "Intervals.icu rejected the API key (HTTP 401). Run: eufy-sync --setup-intervals"
    )

    with patch("eufy_sync.intervals_client.IntervalsClient", return_value=client), \
         patch("getpass.getpass", return_value="typo-key"), \
         pytest.raises(SystemExit) as exc:
        _setup_intervals(path)

    assert exc.value.code == 1
    assert path.read_text() == before
    assert credentials.get_password("default:intervals") == "working-key"
    client.close.assert_called_once_with()
    out = capsys.readouterr().out
    assert "Nothing was enabled. Retry with: eufy-sync --setup-intervals" in out


def test_setup_requires_a_key(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path)
    with patch("eufy_sync.intervals_client.IntervalsClient", side_effect=AssertionError("no check without a key")), \
         patch("getpass.getpass", return_value="   "), \
         pytest.raises(SystemExit):
        _setup_intervals(path)
    assert "intervals" not in yaml.safe_load(path.read_text())["users"][0]


def test_setup_needs_an_existing_config(tmp_path, capsys):
    with pytest.raises(SystemExit):
        _setup_intervals(tmp_path / "missing.yaml")
    assert "Run eufy-sync first" in capsys.readouterr().out


def test_setup_refuses_a_key_prompt_that_would_echo(tmp_path):
    path = tmp_path / "config.yaml"
    _config(path)

    def unsafe_prompt(_):
        warnings.warn("Password may echo", getpass.GetPassWarning, stacklevel=2)
        raise AssertionError("must stop before reading an exposed key")

    with patch("getpass.getpass", side_effect=unsafe_prompt), pytest.raises(SystemExit):
        _setup_intervals(path)


# --- --disconnect-intervals -----------------------------------------------------


def test_disconnect_removes_only_intervals(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _config(path, intervals={"athlete_id": "i1"})
    credentials.store_password("default:intervals", "the-key")
    credentials.store_password("default:garmin", "garmin-pw")

    _disconnect_intervals(path)

    user = yaml.safe_load(path.read_text())["users"][0]
    assert "intervals" not in user
    assert user["garmin"] == {"email": "g@example.com"}
    assert credentials.get_password("default:intervals") is None
    assert credentials.get_password("default:garmin") == "garmin-pw"
    assert "Intervals.icu disconnected" in capsys.readouterr().out


def test_disconnect_when_not_configured_changes_nothing(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    _config(path)
    before = path.read_text()
    _disconnect_intervals(path)
    assert path.read_text() == before
    assert "not configured" in capsys.readouterr().out


# --- first run ------------------------------------------------------------------


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_first_run_can_choose_intervals_alone(_keyring, tmp_path, capsys):
    from eufy_sync.cli.setup import _first_run_setup

    path = tmp_path / "config.yaml"
    answers = ["e@example.com", "n", "n", "n", "y"]

    with patch("builtins.input", side_effect=answers), \
         patch("getpass.getpass", side_effect=["eufy-pw", "the-key"]), \
         patch("eufy_sync.intervals_client.IntervalsClient", return_value=_client()), \
         patch("eufy_sync.eufy_client.EufyClient", side_effect=RuntimeError("offline")):
        _first_run_setup(path)

    user = yaml.safe_load(path.read_text())["users"][0]
    assert user["intervals"] == {"athlete_id": "i12345"}
    assert "garmin" not in user
    assert "the-key" not in path.read_text()
    assert credentials.get_password("default:intervals") == "the-key"
    assert credentials.get_password("default:eufy") == "eufy-pw"
    assert "Running first sync to Intervals.icu" in capsys.readouterr().out


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_first_run_failed_intervals_check_writes_nothing(_keyring, tmp_path, capsys):
    from eufy_sync.cli.setup import _first_run_setup

    path = tmp_path / "config.yaml"
    client = _client()
    client.athlete_id.side_effect = RuntimeError("Intervals.icu rejected the API key (HTTP 403)")

    with patch("builtins.input", side_effect=["e@example.com", "y", "n", "n", "y", "g@example.com"]), \
         patch("getpass.getpass", side_effect=["eufy-pw", "garmin-pw", "bad-key"]), \
         patch("eufy_sync.intervals_client.IntervalsClient", return_value=client), \
         pytest.raises(SystemExit):
        _first_run_setup(path)

    assert not path.exists()
    assert credentials.get_password("default:eufy") is None
    assert credentials.get_password("default:intervals") is None
    assert "Retry with: eufy-sync\n" in capsys.readouterr().out


# --- config and migration -------------------------------------------------------


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_config_reads_the_key_from_the_credential_store(_keyring, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"users": [{
        "name": "default", "eufy": {"email": "e@example.com"}, "intervals": {"athlete_id": "i12345"},
    }]}))
    credentials.store_password("default:eufy", "eufy-pw")
    credentials.store_password("default:intervals", "the-key")

    user = load_config(path).users[0]

    assert user.intervals == IntervalsConfig(athlete_id="i12345", api_key="the-key")
    assert user.garmin is None


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_config_without_a_saved_key_points_at_setup(_keyring, tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"users": [{
        "name": "default", "eufy": {"email": "e@example.com"}, "intervals": {"athlete_id": "i12345"},
    }]}))
    credentials.store_password("default:eufy", "eufy-pw")

    with pytest.raises(ValueError, match="--setup-intervals"):
        load_config(path, migrate=False)


@patch("eufy_sync.credentials._keyring_available", return_value=False)
@pytest.mark.parametrize("section", [{}, None, {"athlete_id": " "}])
def test_config_without_an_athlete_id_points_at_setup(_keyring, tmp_path, section):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"users": [{
        "name": "default", "eufy": {"email": "e@example.com"}, "intervals": section,
    }]}))
    credentials.store_password("default:eufy", "eufy-pw")
    credentials.store_password("default:intervals", "the-key")

    with pytest.raises(ValueError, match="athlete_id.*--setup-intervals"):
        load_config(path)


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_config_accepts_an_env_reference_for_the_key(_keyring, tmp_path, monkeypatch):
    monkeypatch.setenv("INTERVALS_KEY", "env-key")
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"users": [{
        "name": "default", "eufy": {"email": "e@example.com"},
        "intervals": {"athlete_id": "i12345", "api_key": "${INTERVALS_KEY}"},
    }]}))
    credentials.store_password("default:eufy", "eufy-pw")

    assert load_config(path).users[0].intervals.api_key == "env-key"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_migration_moves_a_plain_key_and_leaves_an_env_reference(_keyring, tmp_path):
    plain = tmp_path / "plain.yaml"
    _config(plain, intervals={"athlete_id": "i1", "api_key": "plain-key"})
    _migrate_config_passwords(plain)
    assert yaml.safe_load(plain.read_text())["users"][0]["intervals"] == {"athlete_id": "i1"}
    assert credentials.get_password("default:intervals") == "plain-key"

    env = tmp_path / "env.yaml"
    _config(env, intervals={"athlete_id": "i1", "api_key": "${INTERVALS_KEY}"})
    _migrate_config_passwords(env)
    assert yaml.safe_load(env.read_text())["users"][0]["intervals"]["api_key"] == "${INTERVALS_KEY}"


# --- status, summary, notifications ---------------------------------------------


def _intervals_user():
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        intervals=IntervalsConfig(athlete_id="i12345", api_key="the-key"),
    )


def test_status_shows_the_intervals_line_without_the_key(tmp_path, capsys):
    from eufy_sync.cli.status import _show_status
    from eufy_sync.state import SyncState

    state = SyncState(tmp_path / "s.db")
    now = datetime.now(timezone.utc).isoformat()
    state.record_upload_failure("default", "intervals", "a", now, 80.0, now)
    with patch("eufy_sync.eufy_client.EufyClient") as eufy:
        eufy.return_value.token_status.return_value = {"state": "valid", "days_remaining": 10}
        _show_status(state, [_intervals_user()])

    out = capsys.readouterr().out
    assert "Intervals.icu auth: API key saved for athlete i12345" in out
    assert "1 upload waiting to retry (Intervals.icu)" in out
    assert "the-key" not in out
    state.close()


def test_no_op_summary_mentions_intervals(tmp_path, capsys):
    from eufy_sync.cli.status import _print_summary
    from eufy_sync.state import SyncState

    state = SyncState(tmp_path / "s.db")
    with patch("eufy_sync.eufy_client.EufyClient") as eufy:
        eufy.return_value.token_status.return_value = {"state": "valid", "days_remaining": 10}
        _print_summary({"intervals": 0}, [], state, [_intervals_user()])

    assert "Intervals.icu key saved" in capsys.readouterr().out
    state.close()


def test_counts_and_labels_use_the_service_name():
    from eufy_sync.cli.app import _target_label

    assert update_counts_summary({"garmin": 1, "intervals": 2}) == "Syncs completed: Garmin 1, Intervals.icu 2."
    assert _target_label({"garmin": 1, "strava": 1, "intervals": 1}) == "Garmin, Strava and Intervals.icu"


def test_history_column_fits_the_intervals_name(tmp_path, capsys):
    from eufy_sync.cli.status import _show_history
    from eufy_sync.state import SyncState

    state = SyncState(tmp_path / "s.db")
    ts = datetime(2026, 9, 30, 12, tzinfo=timezone.utc).isoformat()
    state.record_sync("default", "m1", ts, 80.0, ts, target="garmin")
    state.record_sync("default", "m1", ts, 80.0, ts, target="intervals")
    _show_history(state, [_intervals_user()])

    header, _, row = capsys.readouterr().out.splitlines()[:3]
    assert header.index("Intervals") == row.rindex("✓")
    state.close()


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_rejected_key_notification_names_the_setup_command(
    _keyring, _migrate, _notice, _updates, mock_notify, _summary, tmp_path,
):
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    _config(config_path, intervals={"athlete_id": "i1"})
    credentials.store_password("default:eufy", "pw")
    credentials.store_password("default:garmin", "pw")
    credentials.store_password("default:intervals", "the-key")
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(tmp_path / "s.db"), "--headless"]
    errors = {"intervals": "Intervals.icu rejected the API key (HTTP 401). Run: eufy-sync --setup-intervals"}

    with patch("eufy_sync.sync.sync_user", return_value=({"garmin": 1, "intervals": 0}, errors)), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    mock_notify.assert_called_once()
    title, body = mock_notify.call_args.args
    assert title == "eufy-sync: Intervals.icu key rejected"
    assert body == "Synced to Garmin. Run: eufy-sync --setup-intervals"
    assert mock_notify.call_args.kwargs["command"] == "eufy-sync --setup-intervals"


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_target_flag_accepts_intervals(_keyring, _migrate, _notice, _updates, _summary, tmp_path):
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    _config(config_path, intervals={"athlete_id": "i1"})
    credentials.store_password("default:eufy", "pw")
    credentials.store_password("default:garmin", "pw")
    credentials.store_password("default:intervals", "the-key")
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(tmp_path / "s.db"), "--target", "intervals"]

    with patch("eufy_sync.sync.sync_user", return_value=({"intervals": 1}, {})) as sync_user, \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert sync_user.call_args.kwargs["target"] == "intervals"


def test_uninstall_removes_the_intervals_key(tmp_path):
    from eufy_sync.cli.maintenance import _uninstall

    data_dir = tmp_path / "data"
    data_dir.mkdir()

    with patch("sys.stdin.isatty", return_value=True), \
         patch("builtins.input", return_value="y"), \
         patch("eufy_sync.credentials._keyring_available", return_value=True), \
         patch("eufy_sync.credentials.delete_password") as delete_password, \
         patch("eufy_sync.credentials.delete_token"), \
         patch("eufy_sync.platform_support.agent_installed", return_value=False):
        assert _uninstall(data_dir) is True

    delete_password.assert_any_call("default:intervals")
