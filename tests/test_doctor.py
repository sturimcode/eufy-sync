"""Tests for eufy-sync --doctor.

Every client/token_status/subprocess call is mocked - no network, no real
keychain, no real Launch Agent. The doctor must never let an exception
escape as a traceback; every failure is a printed line.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

from eufy_sync.cli import doctor
from eufy_sync.config import AppConfig, EufyConfig, GarminConfig, StravaConfig, UserConfig


def _write_config(path: Path, *, garmin: bool = True, strava: bool = True, customer_id: str | None = "abc1234567890867f") -> None:
    users = [{
        "name": "default",
        "eufy": {"email": "e@example.com", "password": "pw", **({"customer_id": customer_id} if customer_id else {})},
    }]
    if garmin:
        users[0]["garmin"] = {"email": "g@example.com", "password": "pw"}
    if strava:
        users[0]["strava"] = {"client_id": "123", "client_secret": "secret"}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.dump({"users": users}, f)


def _app_config(*, garmin: bool = True, strava: bool = True, customer_id: str | None = "abc1234567890867f") -> AppConfig:
    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw", customer_id=customer_id),
        garmin=GarminConfig(email="g@example.com", password="pw") if garmin else None,
        strava=StravaConfig(client_id="123", client_secret="secret") if strava else None,
    )
    return AppConfig(sync_interval_minutes=15, users=[user])


class _FakeMeasurement:
    def __init__(self, timestamp):
        self.timestamp = timestamp
        self.weight_kg = 70.0


def _patch_all_pass(tmp_path, monkeypatch):
    """Patch every dependency of _run_doctor to a healthy state."""
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    _write_config(config_path)

    app_config = _app_config()

    monkeypatch.setattr(doctor, "load_config", MagicMock(return_value=app_config))
    monkeypatch.setattr(doctor, "_keyring_available", MagicMock(return_value=True))

    eufy_client = MagicMock()
    eufy_client.token_status.return_value = {"state": "valid", "days_remaining": 21}
    recent = datetime.now(timezone.utc) - timedelta(hours=5)
    eufy_client.fetch_measurements.return_value = [_FakeMeasurement(recent)]
    monkeypatch.setattr(doctor, "EufyClient", MagicMock(return_value=eufy_client))

    garmin_auth = MagicMock()
    garmin_auth.token_status.return_value = {"state": "valid", "days_remaining": None}
    monkeypatch.setattr(doctor, "GarminAuth", MagicMock(return_value=garmin_auth))

    strava_client = MagicMock()
    strava_client.token_status.return_value = {"state": "valid", "days_remaining": None, "hours_remaining": 5}
    monkeypatch.setattr(doctor, "StravaClient", MagicMock(return_value=strava_client))

    monkeypatch.setattr(doctor.platform, "system", MagicMock(return_value="Darwin"))
    wrapper = tmp_path / "run-sync.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(doctor.shared, "LAUNCH_AGENT_PATH", tmp_path / "agent.plist")
    plist = f"""<?xml version="1.0"?>
<plist><dict>
<key>ProgramArguments</key>
<array><string>{wrapper}</string></array>
</dict></plist>"""
    doctor.shared.LAUNCH_AGENT_PATH.write_text(plist)
    monkeypatch.setattr(
        doctor.subprocess, "run",
        MagicMock(return_value=MagicMock(stdout=doctor.shared.LAUNCH_AGENT_LABEL, returncode=0)),
    )

    state = MagicMock()
    recent_sync = time.time() - 3600 * 2
    state.get_latest_sync_timestamp.return_value = int(recent_sync)
    monkeypatch.setattr(doctor, "SyncState", MagicMock(return_value=state))

    monkeypatch.setattr(doctor.updater, "_latest_pypi_version", MagicMock(return_value=_current_version()))

    return config_path, db_path


def _current_version() -> str:
    from eufy_sync import __version__
    return __version__


def test_all_pass_exits_zero_and_prints_summary(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "All checks passed." in out
    assert "FAIL" not in out


def test_garmin_no_session_fails_with_exact_fix_and_exit_1(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.GarminAuth.return_value.token_status.return_value = {"state": "no_session", "days_remaining": None}

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "garmin session" in out
    assert "fix: eufy-sync --reauth garmin" in out


def test_no_config_fails_and_skips_remaining_checks_without_prompting(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"  # does not exist
    db_path = tmp_path / "state.db"

    mock_input = MagicMock(side_effect=AssertionError("input() must never be called by --doctor"))
    monkeypatch.setattr("builtins.input", mock_input)

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "config" in out
    assert "no config" in out.lower()
    # Remaining checks explicitly skipped, not silently omitted
    assert "skip" in out.lower()
    mock_input.assert_not_called()


def test_eufy_cloud_exception_prints_fail_line_not_traceback(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.EufyClient.return_value.fetch_measurements.side_effect = RuntimeError("boom: connection reset")

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "eufy cloud" in out
    assert "boom: connection reset" in out
    assert "Traceback" not in out


def test_eufy_cloud_password_error_suggests_update_password(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.EufyClient.return_value.fetch_measurements.side_effect = RuntimeError(
        "If you changed your Eufy password, run: eufy-sync --update-password"
    )

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "eufy cloud" in out
    assert "fix: eufy-sync --update-password" in out


def test_stale_weighin_warns_with_open_app_hint(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    stale = datetime.now(timezone.utc) - timedelta(days=3)
    doctor.EufyClient.return_value.fetch_measurements.return_value = [_FakeMeasurement(stale)]

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 0  # WARN alone does not fail the run
    assert "WARN" in out
    assert "eufy cloud" in out
    assert "open the Eufy app" in out


def test_launch_agent_pointing_at_raw_binary_warns_install_agent(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    plist = """<?xml version="1.0"?>
<plist><dict>
<key>ProgramArguments</key>
<array><string>/Users/x/.local/bin/eufy-sync</string></array>
</dict></plist>"""
    doctor.shared.LAUNCH_AGENT_PATH.write_text(plist)

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "WARN" in out
    assert "launch agent" in out
    assert "outdated registration" in out
    assert "fix: eufy-sync --install-agent" in out


def test_version_newer_available_warns_with_update_fix(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.updater._latest_pypi_version = MagicMock(return_value="99.0.0")

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "WARN" in out
    assert "version" in out
    assert "99.0.0" in out
    assert "fix: eufy-sync --update" in out


def test_warnings_alone_still_exit_zero(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    # Several WARN-only conditions at once.
    stale = datetime.now(timezone.utc) - timedelta(days=5)
    doctor.EufyClient.return_value.fetch_measurements.return_value = [_FakeMeasurement(stale)]
    doctor.updater._latest_pypi_version = MagicMock(return_value="99.0.0")

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 0
    assert "FAIL" not in out
    assert out.count("WARN") >= 2
    # A warning-only run exits 0, so the summary must not claim problems.
    assert "warning(s), nothing blocking" in out
    assert "problem(s) found" not in out


def test_eufy_cloud_check_authenticates_before_fetching(tmp_path, monkeypatch, capsys):
    """Caught live: the mocked client hid that fetch_measurements requires a
    prior authenticate(); the real client raises without it. Pin the order."""
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    calls = []
    client = doctor.EufyClient.return_value
    client.authenticate.side_effect = lambda: calls.append("authenticate")
    original_fetch = client.fetch_measurements.return_value

    def fetch(**kwargs):
        if "authenticate" not in calls:
            raise RuntimeError("Must authenticate before fetching measurements")
        return original_fetch

    client.fetch_measurements.side_effect = fetch

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "Must authenticate" not in out
    assert "PASS  eufy cloud" in out
    assert code == 0


def test_doctor_dispatches_before_wizard_via_main(tmp_path, capsys):
    """--doctor through the real entry point: a no-config run must report and
    exit 1 without ever entering the first-run wizard. Guards the dispatch
    ORDER in app.main(), which the direct _run_doctor tests cannot see."""
    from eufy_sync.cli.app import main

    def boom_input(*a, **k):
        raise AssertionError("wizard input() must not be called for --doctor")

    argv = ["eufy-sync", "--doctor",
            "--config", str(tmp_path / "missing" / "config.yaml"),
            "--db", str(tmp_path / "state.db")]
    with patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "FAIL" in out and "config" in out
    assert "first time setup" not in out


def test_eufy_token_expired_is_warn_not_fail(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.EufyClient.return_value.token_status.return_value = {"state": "expired", "days_remaining": 0}

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "eufy token" in out
    assert code == 0


def test_strava_expired_fails_with_reauth_fix(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.StravaClient.return_value.token_status.return_value = {"state": "expired", "days_remaining": 0}

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "strava token" in out
    assert "fix: eufy-sync --reauth strava" in out


def test_profile_unset_warns_with_select_profile_fix(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    single_profile_config = _app_config(customer_id=None)
    doctor.load_config.return_value = single_profile_config

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "profile" in out
    assert "fix: eufy-sync --select-profile" in out
    assert code == 0


def test_keychain_fallback_never_fails(tmp_path, monkeypatch, capsys):
    """With no keychain backend, credentials fall back to the 0o600 file
    store automatically - this is a normal, working configuration, not a
    problem, so it must report PASS, never FAIL or WARN."""
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    monkeypatch.setattr(doctor, "active_store_label", MagicMock(return_value="file (~/.garmin-sync/credentials.json)"))

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "PASS" in out
    assert "keychain" in out
    assert "file" in out
    # keychain must never contribute a FAIL or WARN line of its own
    assert "FAIL  keychain" not in out
    assert "WARN  keychain" not in out
    assert code == 0


def test_launch_agent_not_installed_warns(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.shared.LAUNCH_AGENT_PATH.unlink()

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "launch agent" in out
    assert "not installed" in out
    assert "fix: eufy-sync --install-agent" in out
    assert code == 0


def test_launch_agent_not_loaded_warns(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.subprocess.run = MagicMock(return_value=MagicMock(stdout="", returncode=0))

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "launch agent" in out
    assert "installed but not loaded" in out
    assert "fix: eufy-sync --install-agent" in out
    assert code == 0


def test_launch_agent_skipped_on_non_macos(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.platform.system = MagicMock(return_value="Linux")

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "launch agent" not in out
    assert code == 0


def test_state_db_never_synced(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.SyncState.return_value.get_latest_sync_timestamp.return_value = None

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "never synced" in out
    assert code == 0


def test_state_db_open_failure_fails(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.SyncState = MagicMock(side_effect=RuntimeError("disk full"))

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "state db" in out
    assert "disk full" in out


def test_version_check_unreachable_warns(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.updater._latest_pypi_version = MagicMock(return_value=None)

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "WARN" in out
    assert "could not check" in out
    assert code == 0


def test_config_parse_error_fails_with_message(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    _write_config(config_path)
    monkeypatch.setattr(doctor, "load_config", MagicMock(side_effect=ValueError("bad yaml: line 3")))

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert code == 1
    assert "FAIL" in out
    assert "bad yaml: line 3" in out


def test_garmin_and_strava_skipped_when_not_configured(tmp_path, monkeypatch, capsys):
    config_path, db_path = _patch_all_pass(tmp_path, monkeypatch)
    doctor.load_config.return_value = _app_config(garmin=False, strava=False)

    code = doctor._run_doctor(config_path, db_path)

    out = capsys.readouterr().out
    assert "garmin session" not in out
    assert "strava token" not in out
    assert code == 0
