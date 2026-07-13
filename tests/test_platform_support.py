"""The platform_support package: implementation selection and the generic
no-op layer, plus a spot-check that the macOS agent status is a faithful move
of the old doctor launch-agent check.

The full macOS behavior (notifications, plist bytes, launchctl invocations) is
covered by test_notify.py and test_cli.py against the macos module directly.
"""
from __future__ import annotations

from unittest.mock import patch

from eufy_sync import platform_support
from eufy_sync.platform_support import generic, macos


def test_select_picks_macos_on_darwin():
    with patch("eufy_sync.platform_support.platform.system", return_value="Darwin"):
        assert platform_support._select() is macos


def test_select_picks_generic_on_linux():
    with patch("eufy_sync.platform_support.platform.system", return_value="Linux"):
        assert platform_support._select() is generic


def test_generic_notify_swallows_everything_and_runs_no_subprocess():
    with patch("subprocess.run") as run:
        assert generic.notify("t", "m", command="eufy-sync --update") is None
        run.assert_not_called()


def test_generic_agent_status_is_none():
    assert generic.agent_status() is None


def test_macos_agent_status_warns_when_not_installed(tmp_path):
    with patch.object(macos, "LAUNCH_AGENT_PATH", tmp_path / "missing.plist"):
        status = macos.agent_status()

    assert status["status"] == "WARN"
    assert status["label"] == "launch agent"
    assert status["detail"] == "not installed"
    assert status["fix"] == "eufy-sync --install-agent"
