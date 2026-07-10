"""PyPI version checks and self-update."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
import urllib.request

from eufy_sync import platform_support
from eufy_sync.cli import shared


def _latest_pypi_version() -> str | None:
    """Return the latest eufy-sync version on PyPI, or None if unreachable."""
    try:
        req = urllib.request.Request(
            "https://pypi.org/pypi/eufy-sync/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read(1_000_000))
        return data["info"]["version"]
    except Exception:
        return None


def _check_for_updates() -> None:
    """Check PyPI for a newer version and point the user at eufy-sync --update."""
    try:
        cache_file = shared.DATA_DIR / "update_check"
        now = time.time()

        if cache_file.exists():
            last_check = float(cache_file.read_text().strip())
            if now - last_check < shared.UPDATE_CHECK_INTERVAL:
                return

        latest = _latest_pypi_version()
        if latest is None:
            return

        from eufy_sync import __version__

        def _parse(v: str) -> tuple:
            # Compare numeric prefix only - tolerates suffixes like "1.7.2rc1" or "1.7.2.dev0".
            if not v or len(v) > 64:
                raise ValueError(f"Implausible version string: {v!r}")
            match = re.match(r"^\d+(?:\.\d+)*", v)
            if not match:
                raise ValueError(f"Implausible version string: {v!r}")
            return tuple(int(x) for x in match.group(0).split("."))

        latest_parsed = _parse(latest)
        current_parsed = _parse(__version__)

        # Save cache only after both version strings parse successfully
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(str(now))

        if latest_parsed <= current_parsed:
            return

        if sys.stdin.isatty():
            print(f"Update available: v{latest} (you have v{__version__}). Run: eufy-sync --update")
        else:
            platform_support.notify("eufy-sync", f"Update available: v{latest}. Run: eufy-sync --update", command="eufy-sync --update")

    except Exception:
        pass  # never let update check break a sync


def _self_update() -> None:
    """Upgrade eufy-sync in place to the latest PyPI release.

    Pins the exact version so a stale package-index cache cannot report
    "already up to date" against a release that just landed.
    """
    from eufy_sync import __version__

    latest = _latest_pypi_version()
    if latest is None:
        print("Could not reach PyPI. Check your connection and try again.")
        return
    if latest == __version__:
        print(f"Already on the latest version (v{__version__}).")
        return
    if not re.match(r"^\d+(?:\.\d+)*", latest):
        print(f"Unexpected version from PyPI ({latest!r}); update manually with pipx.")
        return

    if "/uv/tools/" in sys.executable and shutil.which("uv"):
        # Installed with `uv tool install`. Its venvs carry no pip, and pipx
        # would create a second copy, so update through uv itself.
        cmd = ["uv", "tool", "install", "--force", f"eufy-sync=={latest}"]
    elif shutil.which("pipx"):
        cmd = ["pipx", "install", "--force", f"eufy-sync=={latest}"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", f"eufy-sync=={latest}"]

    print(f"Updating eufy-sync from v{__version__} to v{latest}...")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print(f"Updated to v{latest}.")
    else:
        print("Update failed. Run manually: " + " ".join(cmd))
