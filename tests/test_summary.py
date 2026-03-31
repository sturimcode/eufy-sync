from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

from eufy_garmin_sync.cli import _print_summary


def _mock_state(last_sync_ts: int | None = None):
    state = MagicMock()
    state.get_latest_sync_timestamp.return_value = last_sync_ts
    return state


def _mock_user(name: str = "default"):
    user = MagicMock()
    user.name = name
    user.garmin.email = "test@example.com"
    user.garmin.password = "pw"
    return user


def _patch_token_status(return_value):
    """Patch GarminAuth.token_status at the class level."""
    return patch(
        "eufy_garmin_sync.garmin_auth.GarminAuth.token_status",
        return_value=return_value,
    )


def test_summary_no_new_measurements(capsys):
    last_sync = int(time.time()) - 3600 * 5  # 5 hours ago
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_token_status({"state": "valid", "days_remaining": 200}):
        _print_summary(0, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "No new measurements" in output
    assert "last sync: 5h ago" in output
    assert "token valid 200d" in output


def test_summary_no_new_measurements_days_ago(capsys):
    last_sync = int(time.time()) - 86400 * 3  # 3 days ago
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_token_status({"state": "valid", "days_remaining": 100}):
        _print_summary(0, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "last sync: 3d ago" in output


def test_summary_synced_measurements(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary(3, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Synced 3 measurements to Garmin Connect." == output


def test_summary_synced_singular(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary(1, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Synced 1 measurement to Garmin Connect." == output


def test_summary_failure(capsys):
    state = _mock_state()
    user = _mock_user()

    _print_summary(0, [("default", "connection timeout")], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Sync failed for: default" in output
    assert "--verbose" in output


def test_summary_refresh_needed(capsys):
    last_sync = int(time.time()) - 3600
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_token_status({"state": "refresh_needed", "days_remaining": 300}):
        _print_summary(0, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "token refresh pending" in output
    assert "300d" in output
    assert "token valid" not in output


def test_summary_expired_token(capsys):
    last_sync = int(time.time()) - 3600
    state = _mock_state(last_sync)
    user = _mock_user()

    with _patch_token_status({"state": "expired", "days_remaining": 0}):
        _print_summary(0, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert "Garmin token EXPIRED" in output


def test_summary_no_session(capsys):
    state = _mock_state(None)  # never synced
    user = _mock_user()

    with _patch_token_status({"state": "no_session", "days_remaining": None}):
        _print_summary(0, [], state, [user])

    output = capsys.readouterr().out.strip()
    assert output == "No new measurements"
