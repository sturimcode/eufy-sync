"""_notify click actions: with terminal-notifier installed, notifications
that carry a fix command open Terminal and run it when clicked; everything
else keeps the plain osascript path."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from eufy_sync.cli import shared

# conftest's autouse _mute_notifications fixture replaces shared._notify with
# a MagicMock so tests never fire real notifications. These tests exercise the
# real function, captured here at import time, with subprocess.run mocked out.
_real_notify = shared._notify


def _run_notify(which_return, command=None):
    run = MagicMock()
    with patch("eufy_sync.cli.shared.shutil.which", return_value=which_return), \
         patch("eufy_sync.cli.shared.os.path.exists", return_value=False), \
         patch("eufy_sync.cli.shared.subprocess.run", run):
        _real_notify("eufy-sync: re-login needed", "Run: eufy-sync --reauth garmin", command=command)
    return run


def test_command_with_terminal_notifier_attaches_click_action():
    run = _run_notify("/opt/homebrew/bin/terminal-notifier", command="eufy-sync --reauth garmin")

    argv = run.call_args.args[0]
    assert argv[0] == "/opt/homebrew/bin/terminal-notifier"
    assert "-execute" in argv
    execute = argv[argv.index("-execute") + 1]
    assert "eufy-sync --reauth garmin" in execute
    assert "Terminal" in execute


def test_command_without_terminal_notifier_falls_back_to_osascript():
    run = _run_notify(None, command="eufy-sync --reauth garmin")

    argv = run.call_args.args[0]
    assert argv[0] == "osascript"
    assert "display notification" in argv[2]


def test_plain_notification_ignores_terminal_notifier():
    run = _run_notify("/opt/homebrew/bin/terminal-notifier", command=None)

    argv = run.call_args.args[0]
    assert argv[0] == "osascript"


def test_homebrew_path_is_checked_when_which_misses():
    """launchd runs with a minimal PATH that excludes Homebrew, so the
    lookup must also try the standard install locations directly."""
    run = MagicMock()
    with patch("eufy_sync.cli.shared.shutil.which", return_value=None), \
         patch("eufy_sync.cli.shared.os.path.exists",
               side_effect=lambda p: p == "/opt/homebrew/bin/terminal-notifier"), \
         patch("eufy_sync.cli.shared.subprocess.run", run):
        _real_notify("t", "m", command="eufy-sync --update")

    argv = run.call_args.args[0]
    assert argv[0] == "/opt/homebrew/bin/terminal-notifier"


def test_notify_still_fails_silently():
    with patch("eufy_sync.cli.shared.shutil.which", side_effect=RuntimeError("boom")):
        _real_notify("t", "m", command="eufy-sync --update")  # must not raise
