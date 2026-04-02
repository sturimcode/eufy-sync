from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

from eufy_sync.cli import _print_summary


def _mock_state(last_sync_ts: int | None = None):
    state = MagicMock()
    state.get_latest_sync_timestamp.return_value = last_sync_ts
    return state


def _mock_user(name: str = "default", has_garmin: bool = True, has_strava: bool = False):
    user = MagicMock()
    user.name = name
    if has_garmin:
        user.garmin.email = "test@example.com"
        user.garmin.password = "pw"
    else:
        user.garmin = None
    if has_strava:
        user.strava.client_id = "12345"
        user.strava.client_secret = "secret"
    else:
        user.strava = None
    return user


def _patch_token_status(return_value):
    """Patch GarminAuth.token_status at the class level."""
    return patch(
        "eufy_sync.garmin_auth.GarminAuth.token_status",
        return_value=return_value,
    )


def _patch_eufy_token_status(return_value=None):
    """Patch EufyClient.token_status to avoid hitting real tokens."""
    if return_value is None:
        return_value = {"state": "valid", "days_remaining": 25}
    return patch(
        "eufy_sync.eufy_client.EufyClient.token_status",
        return_value=return_value,
    )


def test_summary_no_new_measurements(capsys):
    last_sync = int(time.time()) - 3600 * 5  # 5 hours ago
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "valid", "days_remaining": 200}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "No new measurements" in output
    assert "last sync: 5h ago" in output
    assert "Garmin token valid 200d" in output


def test_summary_no_new_measurements_days_ago(capsys):
    last_sync = int(time.time()) - 86400 * 3  # 3 days ago
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "valid", "days_remaining": 100}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "last sync: 3d ago" in output


def test_summary_synced_garmin_only(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({"garmin": 3}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Synced 3 measurements to Garmin Connect." == output


def test_summary_synced_strava_only(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=False, has_strava=True)

    _print_summary({"strava": 2}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Synced 2 measurements to Strava." == output


def test_summary_synced_both_targets(capsys):
    state = _mock_state()
    user = _mock_user(has_garmin=True, has_strava=True)

    _print_summary({"garmin": 3, "strava": 3}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Synced 6 measurements" in output
    assert "Garmin: 3" in output
    assert "Strava: 3" in output


def test_summary_synced_singular(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({"garmin": 1}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Synced 1 measurement to Garmin Connect." == output


def test_summary_failure(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary({}, [("default", "connection timeout")], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Sync failed for: default" in output
    assert "--verbose" in output


def test_summary_refresh_needed(capsys):
    last_sync = int(time.time()) - 3600
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "refresh_needed", "days_remaining": 300}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "token refresh pending" in output
    assert "300d" in output


def test_summary_expired_token(capsys):
    last_sync = int(time.time()) - 3600
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "expired", "days_remaining": 0}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Garmin token EXPIRED" in output


def test_summary_no_session(capsys):
    state = _mock_state(None)  # never synced
    user = _mock_user()

    with _patch_eufy_token_status(), _patch_token_status({"state": "no_session", "days_remaining": None}):
        _print_summary({}, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "No new measurements" in output
    assert "Eufy token valid" in output
