"""The single-instance lock that keeps a manual sync off the scheduled one."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from eufy_sync.cli import lock, shared
from eufy_sync.cli.shared import _write_config


def test_lock_file_lands_in_the_data_dir_and_is_created_on_demand():
    """The data dir may not exist yet on a first scheduled run, so taking the
    lock has to create it the same way the config and state writers do."""
    assert not shared.DATA_DIR.exists()

    with lock.single_instance() as acquired:
        assert acquired is True
        assert lock.lock_path() == shared.DATA_DIR / "sync.lock"
        assert lock.lock_path().exists()


def test_second_run_is_told_the_lock_is_held():
    """Two live handles on the same file: the second must not get the lock.
    This is the overlap the 4-hourly task creates when a manual run is already
    going."""
    with lock.single_instance() as first:
        assert first is True
        with lock.single_instance() as second:
            assert second is False


def test_lock_is_released_when_the_block_ends():
    with lock.single_instance() as first:
        assert first is True

    with lock.single_instance() as again:
        assert again is True


def test_lock_is_released_when_the_block_raises():
    """A sync that dies (or calls sys.exit) must not leave the next run
    locked out. The OS would drop it on process exit anyway; this covers the
    same-process case."""
    with pytest.raises(RuntimeError):
        with lock.single_instance() as first:
            assert first is True
            raise RuntimeError("sync blew up")

    with lock.single_instance() as again:
        assert again is True


def test_unusable_lock_file_runs_unlocked():
    """The lock is a courtesy. A data dir we cannot write to must not become a
    new way for the sync to refuse to start."""
    with patch("eufy_sync.cli.lock.os.open", side_effect=OSError("read-only")):
        with lock.single_instance() as acquired:
            assert acquired is True
            # Nothing is holding anything, so a second run is not blocked.
            with lock.single_instance() as second:
                assert second is True


def _write_synced_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    return config_path


@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_sync_skips_and_exits_zero_when_a_run_is_in_progress(
    _keyring, _migrate, _notice, mock_updates, mock_notify, tmp_path, capsys
):
    """A scheduled run that collides with a manual one prints one line and
    stops. It must exit 0 and notify nothing - an overlap is not a failure,
    and a failure toast every four hours is exactly what this avoids."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]

    def boom(*a, **kw):
        raise AssertionError("sync must not run while another run holds the lock")

    with lock.single_instance() as held:
        assert held is True
        with patch("eufy_sync.sync.sync_user", side_effect=boom), \
             patch("sys.argv", argv):
            main()  # returns instead of raising SystemExit: exit code 0

    out = capsys.readouterr().out
    assert "another eufy-sync run is in progress" in out.lower()
    mock_notify.assert_not_called()
    # The skip happens before any other sync-path work.
    mock_updates.assert_not_called()


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_sync_runs_and_releases_the_lock_for_the_next_run(
    _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    """An uncontended run takes the lock, syncs, and leaves it free."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]

    with patch("eufy_sync.sync.sync_user", return_value=({"garmin": 1}, {})), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    with lock.single_instance() as acquired:
        assert acquired is True
