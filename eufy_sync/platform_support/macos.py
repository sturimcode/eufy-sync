"""macOS implementation: osascript/terminal-notifier notifications and the
Launch Agent that runs the scheduled sync.

This is a move of the code that lived in cli/shared.py, cli/maintenance.py,
and cli/doctor.py; the behavior is unchanged. The public entry points
(notify, install_agent, uninstall_agent, offer_agent, agent_status,
agent_installed, purge_agent) are what the platform_support package dispatches
to; the leading-underscore helpers are the original functions, moved intact.
"""
from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from eufy_sync.cli import shared

LAUNCH_AGENT_LABEL = "com.sturimcode.eufy-garmin-sync"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

# The Launch Agent points at this wrapper script, not the pipx/uv binary,
# so the registered executable's bytes stay stable across updates (macOS
# only re-announces "can run in the background" when they change). macOS
# labels the background-item notification by this filename, so it is named
# recognizably rather than something opaque. LEGACY_LAUNCH_WRAPPER_NAME is
# the pre-1.7.17 name, cleaned up on the next --install-agent.
LAUNCH_WRAPPER_NAME = "eufy-sync-agent"
LEGACY_LAUNCH_WRAPPER_NAME = "run-sync.sh"


def _find_terminal_notifier() -> str | None:
    """Locate terminal-notifier (optional Homebrew tool). Checked beyond
    PATH because launchd runs with a minimal PATH that excludes Homebrew."""
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier"):
        if os.path.exists(candidate):
            return candidate
    return None


def _notify(title: str, message: str, command: str | None = None) -> None:
    """Send a macOS notification. Fails silently on other platforms.

    When a fix command is given and terminal-notifier is installed,
    clicking the notification opens a Terminal window running the command,
    so the user lands in the interactive prompts instead of Script Editor
    (plain osascript notifications belong to Script Editor and clicking
    them just launches it).
    """
    try:
        if command:
            notifier = _find_terminal_notifier()
            if notifier:
                do_script = f'tell application "Terminal" to do script "{command}"'
                activate = 'tell application "Terminal" to activate'
                subprocess.run(
                    [
                        notifier,
                        "-title", title,
                        "-message", message,
                        "-execute", f"osascript -e {shlex.quote(do_script)} -e {shlex.quote(activate)}",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                return
        safe_title = json.dumps(title)
        safe_msg = json.dumps(message)
        subprocess.run(
            ["osascript", "-e", f'display notification {safe_msg} with title {safe_title}'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _write_run_script(binary_path: str) -> Path:
    """Write the stable wrapper script the Launch Agent runs.

    macOS re-announces "can run in the background" whenever a registered
    background item's executable changes identity, and pipx/uv replace the
    binary on every update. The agent therefore points at this script, whose
    bytes never change across updates, so the announcement fires once, not
    once per release. Skipping the rewrite when content is unchanged is what
    keeps the file's identity stable. The filename is what macOS shows in that
    announcement, so it is a recognizable "eufy-sync-agent", not an opaque one.
    """
    script_path = shared.DATA_DIR / LAUNCH_WRAPPER_NAME
    content = f'#!/bin/sh\nexec "{binary_path}" --headless\n'
    if script_path.exists() and script_path.read_text() == content:
        return script_path
    script_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    script_path.write_text(content)
    script_path.chmod(0o755)
    return script_path


def _generate_plist(program_path: str) -> str:
    """Generate a Launch Agent plist that runs the given program every 4 hours."""
    log_path = str(shared.LOG_FILE)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{program_path}</string>
    </array>

    <key>StartInterval</key>
    <integer>14400</integer>

    <key>RunAtLoad</key>
    <true/>

    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
</dict>
</plist>
"""


def _install_launch_agent() -> None:
    """Install the macOS Launch Agent for automatic sync."""
    if platform.system() != "Darwin":
        print("Auto-sync is only supported on macOS.")
        return

    binary = shutil.which("eufy-sync")
    if not binary:
        print("Warning: could not find eufy-sync on PATH. Skipping auto-sync setup.")
        return

    already_installed = LAUNCH_AGENT_PATH.exists()

    # Ensure the log directory exists with restricted permissions
    shared.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    wrapper = _write_run_script(binary)

    # Drop the pre-1.7.17 wrapper name so it does not linger as an orphan next
    # to the new one.
    legacy_wrapper = shared.DATA_DIR / LEGACY_LAUNCH_WRAPPER_NAME
    if legacy_wrapper.exists():
        legacy_wrapper.unlink()

    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.write_text(_generate_plist(str(wrapper)))

    # Unload first in case an old version is loaded
    subprocess.run(
        ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "load", str(LAUNCH_AGENT_PATH)],
        capture_output=True,
    )

    if already_installed:
        print(f"Launch Agent already installed (reloaded). Logs: {shared.LOG_FILE}")
    else:
        print(f"Automatic sync installed. Logs: {shared.LOG_FILE}")


def _offer_launch_agent() -> None:
    """Offer to install a macOS Launch Agent after first-run setup."""
    if platform.system() != "Darwin":
        return
    if not sys.stdin.isatty():
        return

    print("")
    answer = input("Set up automatic sync every 4 hours? [y/N] ").strip()
    if not answer.lower().startswith("y"):
        return

    _install_launch_agent()


def _uninstall_launch_agent() -> None:
    """Remove the macOS Launch Agent."""
    if not LAUNCH_AGENT_PATH.exists():
        print("No Launch Agent installed.")
        return

    subprocess.run(
        ["launchctl", "unload", str(LAUNCH_AGENT_PATH)],
        capture_output=True,
    )
    LAUNCH_AGENT_PATH.unlink()
    print("Launch Agent removed. Auto-sync disabled.")


def agent_status() -> dict | None:
    """Report the Launch Agent's health for --doctor.

    Reshaped from the old doctor._check_launch_agent: it returns the same
    status/detail/fix strings as a dict instead of calling report().
    """
    try:
        if not LAUNCH_AGENT_PATH.exists():
            return {"status": "WARN", "label": "launch agent", "detail": "not installed", "fix": "eufy-sync --install-agent"}

        content = LAUNCH_AGENT_PATH.read_text()
        wrapper_name = LAUNCH_WRAPPER_NAME
        if wrapper_name not in content:
            return {
                "status": "WARN",
                "label": "launch agent",
                "detail": "outdated registration (re-announces on every update)",
                "fix": "eufy-sync --install-agent",
            }

        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if LAUNCH_AGENT_LABEL not in (result.stdout or ""):
            return {"status": "WARN", "label": "launch agent", "detail": "installed but not loaded", "fix": "eufy-sync --install-agent"}

        return {"status": "PASS", "label": "launch agent", "detail": "loaded, runs every 4h", "fix": None}
    except Exception as e:
        return {"status": "WARN", "label": "launch agent", "detail": f"could not check ({e})", "fix": "eufy-sync --install-agent"}


def agent_installed() -> bool:
    """Whether the Launch Agent plist is present on disk."""
    return LAUNCH_AGENT_PATH.exists()


def purge_agent() -> None:
    """Unload and delete the Launch Agent without printing. Used by the full
    --uninstall, which reports its own summary."""
    if LAUNCH_AGENT_PATH.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], capture_output=True)
        LAUNCH_AGENT_PATH.unlink()


def notify(title: str, message: str, command: str | None = None) -> None:
    _notify(title, message, command)


def install_agent() -> None:
    _install_launch_agent()


def uninstall_agent() -> None:
    _uninstall_launch_agent()


def offer_agent() -> None:
    _offer_launch_agent()
