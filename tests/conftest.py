"""Shared test fixtures."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _hermetic_machine(tmp_path, monkeypatch):
    """Keep every test off the real machine.

    Redirects CRED_FILE and the shared data-dir paths into tmp_path and
    replaces the keyring read/write/delete functions with an in-memory store,
    so no test can touch ~/.garmin-sync or the real keychain even if it
    forgets to redirect them itself (one already did, and wrote fake
    passwords into the real credentials file). Tests that install their own
    fakes (fake_keyring, patch("keyring.get_password"), ...) layer over this
    and keep working.
    """
    data_dir = tmp_path / ".garmin-sync"
    monkeypatch.setattr("eufy_sync.credentials.CRED_FILE", data_dir / "credentials.json")
    monkeypatch.setattr("eufy_sync.cli.shared.DATA_DIR", data_dir)
    monkeypatch.setattr("eufy_sync.cli.shared.DEFAULT_CONFIG", data_dir / "config.yaml")
    monkeypatch.setattr("eufy_sync.cli.shared.DEFAULT_DB", data_dir / "state.db")
    monkeypatch.setattr("eufy_sync.cli.shared.LOG_FILE", data_dir / "sync.log")
    monkeypatch.setattr(
        "eufy_sync.platform_support.macos.LAUNCH_AGENT_PATH",
        tmp_path / "LaunchAgents" / "com.sturimcode.eufy-garmin-sync.plist",
    )
    # setup.py captures shared.DATA_DIR at import time into this module-level
    # constant, so patching shared.DATA_DIR alone leaves it pointed at the
    # real ~/.garmin-sync.
    monkeypatch.setattr(
        "eufy_sync.cli.setup.UPGRADE_NOTICE_FILE", data_dir / ".strava_notice_shown"
    )

    store: dict[tuple[str, str], str] = {}

    def set_password(service, account, password):
        store[(service, account)] = password

    def get_password(service, account):
        return store.get((service, account))

    def delete_password(service, account):
        import keyring
        try:
            del store[(service, account)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found") from None

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.get_password", get_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)


@pytest.fixture(autouse=True)
def _mute_notifications(monkeypatch):
    """Stub the notifier for every test.

    notify shells out to osascript on macOS, so an unmocked call from any code
    path fires a real notification on the machine running the suite. Tests that
    assert on notifications patch eufy_sync.platform_support.notify themselves;
    that patch layers over this stub and restores it on exit.
    """
    monkeypatch.setattr("eufy_sync.platform_support.notify", MagicMock())
