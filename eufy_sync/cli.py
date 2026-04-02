"""CLI entry point for eufy-sync.

Provides a simple `eufy-sync` command that handles first-run setup,
syncing, status checks, re-authentication, and multi-target support
(Garmin Connect and Strava).
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import yaml

DATA_DIR = Path.home() / ".garmin-sync"
DEFAULT_CONFIG = DATA_DIR / "config.yaml"
DEFAULT_DB = DATA_DIR / "state.db"
LOG_FILE = DATA_DIR / "sync.log"
LAUNCH_AGENT_LABEL = "com.sturimcode.eufy-garmin-sync"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"

UPDATE_CHECK_INTERVAL = 604800  # check once per week


def _notify(title: str, message: str) -> None:
    """Send a macOS notification. Fails silently on other platforms."""
    try:
        safe_title = json.dumps(title)
        safe_msg = json.dumps(message)
        subprocess.run(
            ["osascript", "-e", f'display notification {safe_msg} with title {safe_title}'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _check_for_updates() -> None:
    """Check PyPI for a newer version and notify the user."""
    try:
        cache_file = DATA_DIR / "update_check"
        now = time.time()

        if cache_file.exists():
            last_check = float(cache_file.read_text().strip())
            if now - last_check < UPDATE_CHECK_INTERVAL:
                return

        req = urllib.request.Request(
            "https://pypi.org/pypi/eufy-sync/json",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read(1_000_000))

        latest = data["info"]["version"]

        from eufy_sync import __version__

        def _parse(v: str) -> tuple:
            if not v or len(v) > 64:
                raise ValueError(f"Implausible version string: {v!r}")
            return tuple(int(x) for x in v.split("."))

        latest_parsed = _parse(latest)
        current_parsed = _parse(__version__)

        # Save cache only after both version strings parse successfully
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(str(now))

        if latest_parsed <= current_parsed:
            return

        upgrade_cmd = ("pipx install --force eufy-sync"
                       if shutil.which("pipx")
                       else "pip install --upgrade eufy-sync")

        if sys.stdin.isatty():
            print(f"Update available: v{latest} (you have v{__version__}). Run: {upgrade_cmd}")
        else:
            _notify("eufy-sync", f"Update available: v{latest}. Run: {upgrade_cmd}")

    except Exception:
        pass  # never let update check break a sync


def _write_config(path: Path, config: dict) -> None:
    """Write config with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def _store_passwords_in_keychain(user_name: str, eufy_password: str, garmin_password: str | None = None) -> bool:
    """Store passwords in keychain. Returns True if successful."""
    from eufy_sync.credentials import store_password, _keyring_available
    if not _keyring_available():
        return False
    store_password(f"{user_name}:eufy", eufy_password)
    if garmin_password:
        store_password(f"{user_name}:garmin", garmin_password)
    return True


def _ensure_chromium() -> None:
    """Install Playwright Chromium if not already present."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if not Path(path).exists():
                raise FileNotFoundError(path)
    except Exception:
        print("Installing Chromium for Garmin login (one-time)...")
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
        )
        if result.returncode != 0:
            print("Error: Failed to install Chromium. Try running manually:")
            print("  playwright install chromium")
            sys.exit(1)


def _first_run_setup(config_path: Path) -> None:
    """Interactive setup wizard for first-time users."""
    print("")
    print("  eufy-sync - first time setup")
    print("  Credentials are stored in your system keychain (macOS Keychain / Secret Service).")
    print("")

    eufy_email = input("Eufy email: ").strip()
    if not eufy_email:
        print("Error: Eufy email is required.")
        sys.exit(1)

    eufy_password = getpass.getpass("Eufy password: ")
    if not eufy_password:
        print("Error: Eufy password is required.")
        sys.exit(1)

    # Garmin setup (optional)
    print("")
    print("Sync targets (configure at least one):")
    print("")
    garmin_answer = input("Connect Garmin? [Y/n] ").strip()
    garmin_email = None
    garmin_password = None
    if not garmin_answer.lower().startswith("n"):
        garmin_email = input("Garmin email (Enter if same as Eufy): ").strip()
        if not garmin_email:
            garmin_email = eufy_email
        garmin_password = getpass.getpass("Garmin password: ")
        if not garmin_password:
            print("Error: Garmin password is required.")
            sys.exit(1)

    # Strava setup (optional)
    print("")
    strava_answer = input("Connect Strava? [y/N] ").strip()
    strava_config = None
    if strava_answer.lower().startswith("y"):
        strava_config = _prompt_strava_credentials()

    if not garmin_email and not strava_config:
        print("Error: You must configure at least one sync target (Garmin or Strava).")
        sys.exit(1)

    user_name = "default"

    # Store passwords in keychain
    keychain_ok = _store_passwords_in_keychain(user_name, eufy_password, garmin_password)

    # Config YAML stores only emails (no passwords) when keychain is available
    user_config: dict = {
        "name": user_name,
        "eufy": {"email": eufy_email},
    }
    if garmin_email:
        user_config["garmin"] = {"email": garmin_email}
    if strava_config:
        user_config["strava"] = strava_config
    if not keychain_ok:
        # Fallback: store passwords in config file (with 0o600 permissions)
        user_config["eufy"]["password"] = eufy_password
        if garmin_password:
            user_config["garmin"]["password"] = garmin_password
        print("Warning: keychain not available, passwords stored in config file.")
    else:
        print("Passwords saved to system keychain.")

    config = {"users": [user_config]}
    _write_config(config_path, config)

    print("")
    targets = []
    if garmin_email:
        targets.append("Garmin")
    if strava_config:
        targets.append("Strava")
    print(f"Saved. Running first sync to {' and '.join(targets)} (last 7 days)...")
    if garmin_email:
        print("A browser window will open for Garmin login.")
    print("")

    # Run Strava OAuth if configured
    if strava_config:
        from eufy_sync.config import StravaConfig
        from eufy_sync.strava_client import authorize_strava
        authorize_strava(StravaConfig(
            client_id=strava_config["client_id"],
            client_secret=strava_config["client_secret"],
        ))


def _prompt_strava_credentials() -> dict:
    """Prompt user for Strava API app credentials."""
    print("")
    print("  To connect Strava, you need a Strava API application.")
    print("  Create one at: https://www.strava.com/settings/api")
    print("  Set 'Authorization Callback Domain' to: localhost")
    print("")
    client_id = input("Strava Client ID: ").strip()
    if not client_id:
        print("Error: Client ID is required.")
        sys.exit(1)
    client_secret = input("Strava Client Secret: ").strip()
    if not client_secret:
        print("Error: Client Secret is required.")
        sys.exit(1)
    return {"client_id": client_id, "client_secret": client_secret}


def _setup_strava(config_path: Path) -> None:
    """Add or update Strava configuration."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    strava_config = _prompt_strava_credentials()

    user = config["users"][0]
    user["strava"] = strava_config
    _write_config(config_path, config)

    print("Strava credentials saved. Starting authorization...")

    from eufy_sync.config import StravaConfig
    from eufy_sync.strava_client import authorize_strava
    authorize_strava(StravaConfig(
        client_id=strava_config["client_id"],
        client_secret=strava_config["client_secret"],
    ))
    print("Strava connected! Future syncs will update both targets.")


def _update_password(config_path: Path) -> None:
    """Update stored passwords."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    user = config["users"][0]
    user_name = user.get("name", "default")

    print("Press Enter to keep current password.")
    print("")

    eufy_pw = getpass.getpass("New Eufy password: ")
    garmin_pw = getpass.getpass("New Garmin password: ")

    if not eufy_pw and not garmin_pw:
        print("No changes made.")
        return

    from eufy_sync.credentials import store_password, delete_token, _keyring_available
    keychain_ok = _keyring_available()

    if eufy_pw:
        if keychain_ok:
            store_password(f"{user_name}:eufy", eufy_pw)
        else:
            user["eufy"]["password"] = eufy_pw

    if garmin_pw:
        if keychain_ok:
            store_password(f"{user_name}:garmin", garmin_pw)
        else:
            user["garmin"]["password"] = garmin_pw

    if not keychain_ok:
        _write_config(config_path, config)

    # Clear cached tokens for changed services
    if eufy_pw:
        if keychain_ok:
            delete_token("eufy")
        eufy_token = DATA_DIR / "eufy_token.json"
        if eufy_token.exists():
            eufy_token.unlink()

    if garmin_pw:
        if keychain_ok:
            delete_token("garmin")
        garmin_session = DATA_DIR / "session.json"
        if garmin_session.exists():
            garmin_session.unlink()

    changed = []
    if eufy_pw:
        changed.append("Eufy")
    if garmin_pw:
        changed.append("Garmin")
    print(f"{' and '.join(changed)} password{'s' if len(changed) > 1 else ''} updated.")

    if garmin_pw:
        print("Garmin password changed - re-authenticating...")
        _reauth(config_path, config)


def _reauth(config_path: Path, config: dict | None = None, force: bool = False, target: str | None = None) -> None:
    """Force re-authentication for a specific target or all targets."""
    if config is None:
        if not config_path.exists():
            print("No config found. Run eufy-sync first to set up.")
            sys.exit(1)
        with open(config_path) as f:
            config = yaml.safe_load(f)

    user = config["users"][0]
    user_name = user.get("name", "default")

    do_garmin = (target is None or target == "garmin") and "garmin" in user
    do_strava = (target is None or target == "strava") and "strava" in user

    if target and not do_garmin and not do_strava:
        print(f"Target '{target}' is not configured. Check your config.")
        return

    if do_garmin:
        from eufy_sync.config import _get_password
        from eufy_sync.garmin_auth import GarminAuth

        garmin_email = user["garmin"]["email"]
        garmin_pw = _get_password(user_name, "garmin", garmin_email, user["garmin"].get("password"))
        auth = GarminAuth(garmin_email, garmin_pw)

        if force:
            status = auth.token_status()
            if status["state"] == "valid":
                print(f"Garmin tokens are still valid ({status['days_remaining']}d remaining). Re-authenticate anyway? [y/N] ", end="")
                if sys.stdin.isatty():
                    answer = input().strip()
                    if not answer.lower().startswith("y"):
                        print("Garmin re-auth skipped.")
                        do_garmin = False

        if do_garmin:
            _ensure_chromium()
            auth.force_reauth()
            print("Done - Garmin tokens saved.")

    if do_strava:
        from eufy_sync.config import StravaConfig
        from eufy_sync.strava_client import authorize_strava
        strava_cfg = StravaConfig(
            client_id=str(user["strava"]["client_id"]),
            client_secret=user["strava"]["client_secret"],
        )
        authorize_strava(strava_cfg)
        print("Done - Strava tokens saved.")


def _generate_plist(binary_path: str) -> str:
    """Generate a Launch Agent plist that runs eufy-sync every 4 hours."""
    log_path = str(LOG_FILE)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{binary_path}</string>
        <string>--headless</string>
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
    DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PATH.write_text(_generate_plist(binary))

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
        print(f"Launch Agent already installed (reloaded). Logs: {LOG_FILE}")
    else:
        print(f"Automatic sync installed. Logs: {LOG_FILE}")


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


def _uninstall(data_dir: Path) -> None:
    """Remove all eufy-sync data: Launch Agent, config, tokens, state DB."""
    if not sys.stdin.isatty():
        print("Error: --uninstall requires an interactive terminal.")
        sys.exit(1)

    print("This will remove:")
    print(f"  - All saved credentials and tokens in {data_dir}/")
    print(f"  - Keychain entries for eufy-sync")
    print(f"  - Sync history database")
    if LAUNCH_AGENT_PATH.exists():
        print(f"  - Automatic sync Launch Agent")
    print("")

    answer = input("Are you sure? [y/N] ").strip()
    if not answer.lower().startswith("y"):
        print("Cancelled.")
        return

    # Offer to keep state DB so reinstalls don't duplicate measurements
    keep_db = False
    db_path = data_dir / "state.db"
    if db_path.exists():
        print("")
        keep_answer = input("Keep sync history? Prevents duplicates if you reinstall later. [Y/n] ").strip()
        keep_db = not keep_answer.lower().startswith("n")

    # Stop and remove Launch Agent
    if LAUNCH_AGENT_PATH.exists():
        subprocess.run(["launchctl", "unload", str(LAUNCH_AGENT_PATH)], capture_output=True)
        LAUNCH_AGENT_PATH.unlink()

    # Clear keychain entries
    from eufy_sync.credentials import delete_password, delete_token, _keyring_available
    if _keyring_available():
        for suffix in ["eufy", "garmin"]:
            delete_password(f"default:{suffix}")
        delete_token("eufy")
        delete_token("garmin")
        delete_token("strava")

    # Remove data directory (preserving DB if requested)
    if data_dir.exists():
        if keep_db and db_path.exists():
            # Remove everything except state.db
            for item in data_dir.iterdir():
                if item.name != "state.db":
                    item.unlink()
        else:
            shutil.rmtree(data_dir)

    print("")
    if keep_db:
        print(f"Removed all eufy-sync data (sync history kept in {db_path}).")
    else:
        print("Removed all eufy-sync data.")
    print("To remove the package itself, run: pipx uninstall eufy-sync")


def _print_summary(total_counts: dict[str, int], failures: list, state, users: list) -> None:
    """Print a single-line sync summary."""
    if failures:
        fail_names = ", ".join(name for name, _ in failures)
        print(f"Sync failed for: {fail_names}. Run with --verbose for details.")
        return

    total = sum(total_counts.values())
    if total > 0:
        target_names = list(total_counts.keys())
        if len(target_names) == 1:
            name = "Garmin Connect" if target_names[0] == "garmin" else "Strava"
            print(f"Synced {total} measurement{'s' if total != 1 else ''} to {name}.")
        else:
            parts = [f"{n.capitalize()}: {c}" for n, c in total_counts.items()]
            print(f"Synced {total} measurement{'s' if total != 1 else ''} ({', '.join(parts)}).")
        return

    # No-op sync - build an informative one-liner
    parts = ["No new measurements"]

    user = users[0]
    ts = state.get_latest_sync_timestamp(user.name)
    if ts:
        last_sync = datetime.fromtimestamp(ts, tz=timezone.utc)
        ago = datetime.now(timezone.utc) - last_sync
        days = ago.days
        hours = int(ago.total_seconds() / 3600) % 24
        if days > 0:
            parts.append(f"last sync: {days}d ago")
        else:
            parts.append(f"last sync: {hours}h ago")

    from eufy_sync.eufy_client import EufyClient
    eufy_status = EufyClient(user.eufy).token_status()
    if eufy_status["state"] == "expired":
        parts.append("Eufy token EXPIRED (will re-login on next sync)")
    elif eufy_status["state"] == "no_token":
        parts.append("Eufy: no token")
    elif eufy_status["days_remaining"] is not None:
        parts.append(f"Eufy token valid {eufy_status['days_remaining']}d")

    if user.garmin:
        from eufy_sync.garmin_auth import GarminAuth
        status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
        if status["state"] == "expired":
            parts.append("Garmin token EXPIRED")
        elif status["state"] == "refresh_needed":
            parts.append(f"token refresh pending ({status['days_remaining']}d until re-login)")
        elif status["days_remaining"] is not None:
            parts.append(f"Garmin token valid {status['days_remaining']}d")

    if user.strava:
        from eufy_sync.strava_client import StravaClient
        strava_status = StravaClient(user.strava).token_status()
        if strava_status["state"] == "expired":
            parts.append("Strava token EXPIRED")
        elif strava_status["state"] == "no_session":
            parts.append("Strava: not authorized")
        elif strava_status["state"] == "refresh_needed":
            parts.append("Strava token refresh pending")
        else:
            parts.append("Strava connected")

    print(" | ".join(parts))


def _show_status(state, users: list) -> None:
    """Print detailed sync status for all users."""
    for user in users:
        print(f"\n{'=' * 40}")
        print(f"User: {user.name}")
        print(f"{'=' * 40}")

        # Last sync info
        ts = state.get_latest_sync_timestamp(user.name)
        if ts:
            last_sync = datetime.fromtimestamp(ts, tz=timezone.utc)
            ago = datetime.now(timezone.utc) - last_sync
            hours = int(ago.total_seconds() / 3600)
            print(f"Last synced measurement: {last_sync.strftime('%Y-%m-%d %H:%M UTC')} ({hours}h ago)")
        else:
            print("Last synced measurement: never")

        # Eufy token health
        from eufy_sync.eufy_client import EufyClient
        eufy_status = EufyClient(user.eufy).token_status()
        if eufy_status["state"] == "expired":
            print("Eufy auth: token expired (will re-login automatically on next sync)")
        elif eufy_status["state"] == "no_token":
            print("Eufy auth: no saved token - will login on next sync")
        else:
            print(f"Eufy auth: valid ({eufy_status['days_remaining']} days remaining)")

        # Garmin token health
        if user.garmin:
            from eufy_sync.garmin_auth import GarminAuth
            status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
            if status["state"] == "expired":
                print("Garmin auth: EXPIRED - browser re-login needed")
            elif status["state"] == "refresh_needed":
                print(f"Garmin auth: access token expired, will refresh on next sync ({status['days_remaining']}d until re-login)")
            elif status["state"] == "valid":
                print(f"Garmin auth: valid ({status['days_remaining']} days until re-login needed)")
            else:
                print("Garmin auth: no saved session - first run will open browser")

        # Strava token health
        if user.strava:
            from eufy_sync.strava_client import StravaClient
            strava_status = StravaClient(user.strava).token_status()
            if strava_status["state"] == "expired":
                print("Strava auth: EXPIRED - re-authorize with --reauth strava")
            elif strava_status["state"] == "no_session":
                print("Strava auth: not authorized - run --setup-strava")
            elif strava_status["state"] == "refresh_needed":
                print("Strava auth: access token expired, will refresh on next sync")
            else:
                print("Strava auth: valid (refresh token active)")


def _migrate_config_passwords(config_path: Path) -> None:
    """One-time migration: move passwords from config.yaml to keychain."""
    from eufy_sync.credentials import store_password, get_password, _keyring_available

    if not _keyring_available():
        return
    if not config_path.exists():
        return

    with open(config_path) as f:
        config = yaml.safe_load(f)

    changed = False
    for user in config.get("users", []):
        name = user.get("name", "default")
        for service in ["eufy", "garmin"]:
            pw = user.get(service, {}).get("password")
            if pw:
                key = f"{name}:{service}"
                if not get_password(key):
                    store_password(key, pw)
                del user[service]["password"]
                changed = True

    if changed:
        _write_config(config_path, config)
        print("Migrated passwords from config file to system keychain.")


UPGRADE_NOTICE_FILE = DATA_DIR / ".strava_notice_shown"


def _show_upgrade_notice() -> None:
    """One-time notice for users upgrading to eufy-sync with Strava support."""
    if UPGRADE_NOTICE_FILE.exists():
        return
    try:
        UPGRADE_NOTICE_FILE.parent.mkdir(parents=True, exist_ok=True)
        UPGRADE_NOTICE_FILE.write_text("")
    except Exception:
        return
    if sys.stdin.isatty():
        print("New in eufy-sync: Strava support! Run eufy-sync --setup-strava to connect.")
        print("")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="eufy-sync",
        description="Sync Eufy smart scale data to Garmin Connect and Strava",
    )
    parser.add_argument("--status", action="store_true", help="Show sync status and token health")
    parser.add_argument("--reauth", nargs="?", const="all", default=None, metavar="TARGET",
                        help="Re-authenticate (optionally: garmin or strava)")
    parser.add_argument("--setup-strava", action="store_true", help="Connect Strava to your account")
    parser.add_argument("--update-password", action="store_true", help="Change stored passwords")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync last N days")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--headless", action="store_true", help="No browser popups (for Launch Agent)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed sync logs")
    parser.add_argument("--install-agent", action="store_true", help="Set up automatic sync (macOS Launch Agent)")
    parser.add_argument("--uninstall-agent", action="store_true", help="Remove the automatic sync Launch Agent")
    parser.add_argument("--uninstall", action="store_true", help="Remove all data, tokens, and Launch Agent")
    parser.add_argument("--config", type=Path, default=None, help="Config path (default: ~/.garmin-sync/config.yaml)")
    parser.add_argument("--db", type=Path, default=None, help="Database path (default: ~/.garmin-sync/state.db)")
    args = parser.parse_args()

    config_path = args.config or DEFAULT_CONFIG
    db_path = args.db or DEFAULT_DB

    # Handle full uninstall
    if args.uninstall:
        _uninstall(DATA_DIR)
        return

    # Handle Launch Agent install/uninstall
    if args.install_agent:
        _install_launch_agent()
        return

    if args.uninstall_agent:
        _uninstall_launch_agent()
        return

    # Handle Strava setup
    if args.setup_strava:
        _setup_strava(config_path)
        return

    # Handle password update
    if args.update_password:
        _update_password(config_path)
        return

    # Handle reauth
    if args.reauth is not None:
        target = None if args.reauth == "all" else args.reauth
        _reauth(config_path, force=True, target=target)
        return

    # First-run setup if no config exists
    first_run = not config_path.exists()
    if first_run:
        _first_run_setup(config_path)
    else:
        # Migrate existing plaintext passwords to keychain (one-time)
        _migrate_config_passwords(config_path)
        # One-time upgrade notice for users coming from eufy-garmin-sync
        _show_upgrade_notice()

    # Load config (passwords resolved from keychain or YAML fallback)
    from eufy_sync.config import AppConfig, load_config
    config = load_config(config_path)

    # Only install Chromium if Garmin is configured
    has_garmin = any(u.garmin for u in config.users)
    if has_garmin:
        _ensure_chromium()

    # Handle status
    if args.status:
        from eufy_sync.state import SyncState
        state = SyncState(db_path)
        _show_status(state, config.users)
        state.close()
        return

    # Run sync
    import logging
    from eufy_sync.sync import sync_user
    from eufy_sync.state import SyncState

    log_level = "DEBUG" if args.verbose else "WARNING"
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    logger = logging.getLogger("eufy_sync")

    _check_for_updates()

    backfill = args.backfill_days
    if first_run and backfill is None:
        backfill = 7

    state = SyncState(db_path)

    try:
        total_counts: dict[str, int] = {}
        failures = []
        for user in config.users:
            try:
                counts = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                for target_name, count in counts.items():
                    total_counts[target_name] = total_counts.get(target_name, 0) + count
                logger.info("User %s: synced %s", user.name, counts)
            except Exception as e:
                logger.exception("Failed to sync user %s", user.name)
                failures.append((user.name, str(e)))

        total = sum(total_counts.values())

        if failures:
            reauth_needed = any("re-authenticate" in err for _, err in failures)
            eufy_password = any("changed your Eufy password" in err for _, err in failures)
            if reauth_needed:
                _notify("eufy-sync: re-login needed", "Run: eufy-sync --reauth")
            elif eufy_password:
                _notify("eufy-sync: Eufy login failed", "Run: eufy-sync --update-password")
            else:
                fail_msg = "; ".join(f"{name}: {err[:80]}" for name, err in failures)
                _notify("eufy-sync failed", fail_msg)
            logger.error("Sync failed for: %s", "; ".join(f"{n}: {e[:80]}" for n, e in failures))

        if total > 0:
            target_label = " and ".join(n.capitalize() for n in total_counts if total_counts[n] > 0)
            _notify("eufy-sync", f"Synced {total} measurement{'s' if total != 1 else ''} to {target_label}")

        if first_run:
            if failures:
                print("")
                print("First sync failed. Fix the issue above, then run eufy-sync again.")
            else:
                if total > 0:
                    target_label = " and ".join(n.capitalize() for n in total_counts if total_counts[n] > 0)
                    print("")
                    print(f"Synced {total} measurements to {target_label}.")
                _offer_launch_agent()
                print("")
                apps = []
                if has_garmin:
                    apps.append("Garmin Connect")
                if any(u.strava for u in config.users):
                    apps.append("Strava")
                print(f"You're all set! Check the {' and '.join(apps)} app to see your data.")
        elif not args.verbose:
            _print_summary(total_counts, failures, state, config.users)

        sys.exit(1 if failures else 0)

    finally:
        state.close()


if __name__ == "__main__":
    main()
