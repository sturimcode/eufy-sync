"""Shared test fixtures."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _mute_notifications(monkeypatch):
    """Stub the macOS notifier for every test.

    _notify shells out to osascript, so an unmocked call from any code path
    fires a real notification on the machine running the suite. Tests that
    assert on notifications patch eufy_sync.cli._notify themselves; that
    patch layers over this stub and restores it on exit.
    """
    monkeypatch.setattr("eufy_sync.cli._notify", MagicMock())
