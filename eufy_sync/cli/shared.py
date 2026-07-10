"""Path/agent constants and small helpers shared across the cli package."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

import yaml

DATA_DIR = Path.home() / ".garmin-sync"
DEFAULT_CONFIG = DATA_DIR / "config.yaml"
DEFAULT_DB = DATA_DIR / "state.db"
LOG_FILE = DATA_DIR / "sync.log"
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

UPDATE_CHECK_INTERVAL = 604800  # check once per week


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


def _write_config(path: Path, config: dict) -> None:
    """Write config with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def _configure_logging(verbose: bool) -> None:
    """Set up logging. On normal runs, quiet chatty library loggers; under
    --verbose, show full detail. The garminconnect library logs a warning for
    each login strategy that hits a 429 before a later strategy succeeds, which
    looks alarming on an otherwise successful login."""
    import logging
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format=fmt)
        logging.getLogger("garminconnect").setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING, format=fmt)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("garminconnect").setLevel(logging.ERROR)
