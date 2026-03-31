"""CLI entry point for eufy-garmin-sync.

Provides a simple `eufy-sync` command that handles first-run setup,
syncing, status checks, and re-authentication.
"""
from __future__ import annotations

import getpass
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

DATA_DIR = Path.home() / ".garmin-sync"
DEFAULT_CONFIG = DATA_DIR / "config.yaml"
DEFAULT_DB = DATA_DIR / "state.db"
LAUNCH_AGENT_LABEL = "com.sturimcode.eufy-garmin-sync"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"


def _write_config(path: Path, config: dict) -> None:
    """Write config with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def _ensure_chromium() -> None:
    """Install Playwright Chromium if not already present."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.executable_path
    except Exception:
        print("Installing Chromium for Garmin login (one-time)...")
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
        )


def _first_run_setup(config_path: Path) -> None:
    """Interactive setup wizard for first-time users."""
    print("")
    print("  eufy-garmin-sync - first time setup")
    print("  Credentials are stored locally and only sent to Eufy/Garmin over HTTPS.")
    print("")

    eufy_email = input("Eufy email: ").strip()
    if not eufy_email:
        print("Error: Eufy email is required.")
        sys.exit(1)

    eufy_password = getpass.getpass("Eufy password: ")
    if not eufy_password:
        print("Error: Eufy password is required.")
        sys.exit(1)

    garmin_email = input("Garmin email (Enter if same as Eufy): ").strip()
    if not garmin_email:
        garmin_email = eufy_email

    garmin_password = getpass.getpass("Garmin password: ")
    if not garmin_password:
        print("Error: Garmin password is required.")
        sys.exit(1)

    config = {
        "log_level": "INFO",
        "users": [{
            "name": "default",
            "eufy": {"email": eufy_email, "password": eufy_password},
            "garmin": {"email": garmin_email, "password": garmin_password},
        }],
    }

    _write_config(config_path, config)

    print("")
    print("Saved. Running first sync (last 7 days)...")
    print("A browser window will open for Garmin login.")
    print("")


def _update_password(config_path: Path) -> None:
    """Update stored passwords."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    user = config["users"][0]

    print("Press Enter to keep current password.")
    print("")

    eufy_pw = getpass.getpass("New Eufy password: ")
    if eufy_pw:
        user["eufy"]["password"] = eufy_pw

    garmin_pw = getpass.getpass("New Garmin password: ")
    if garmin_pw:
        user["garmin"]["password"] = garmin_pw

    if not eufy_pw and not garmin_pw:
        print("No changes made.")
        return

    _write_config(config_path, config)

    # Clear cached tokens for changed services
    if eufy_pw:
        eufy_token = DATA_DIR / "eufy_token.json"
        if eufy_token.exists():
            eufy_token.unlink()

    if garmin_pw:
        garmin_session = DATA_DIR / "session.json"
        if garmin_session.exists():
            garmin_session.unlink()

    print("Passwords updated.")

    if garmin_pw:
        print("Garmin password changed - re-authenticating...")
        _reauth(config_path, config)


def _reauth(config_path: Path, config: dict | None = None) -> None:
    """Force Garmin re-authentication."""
    if config is None:
        if not config_path.exists():
            print("No config found. Run eufy-sync first to set up.")
            sys.exit(1)
        with open(config_path) as f:
            config = yaml.safe_load(f)

    from eufy_garmin_sync.garmin_auth import GarminAuth

    user = config["users"][0]
    auth = GarminAuth(user["garmin"]["email"], user["garmin"]["password"])
    auth.force_reauth()
    print("Done - Garmin tokens saved.")


def _generate_plist(binary_path: str) -> str:
    """Generate a Launch Agent plist that runs eufy-sync every 4 hours."""
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
    <string>/tmp/eufy-garmin-sync.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/eufy-garmin-sync.log</string>
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

    print(f"Automatic sync installed. Logs: /tmp/eufy-garmin-sync.log")


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


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        prog="eufy-sync",
        description="Sync Eufy smart scale data to Garmin Connect",
    )
    parser.add_argument("--status", action="store_true", help="Show sync status and token health")
    parser.add_argument("--reauth", action="store_true", help="Re-login to Garmin")
    parser.add_argument("--update-password", action="store_true", help="Change stored passwords")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync last N days")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--headless", action="store_true", help="No browser popups (for Launch Agent)")
    parser.add_argument("--install-agent", action="store_true", help="Set up automatic sync (macOS Launch Agent)")
    parser.add_argument("--uninstall-agent", action="store_true", help="Remove the automatic sync Launch Agent")
    parser.add_argument("--config", type=Path, default=None, help="Config path (default: ~/.garmin-sync/config.yaml)")
    parser.add_argument("--db", type=Path, default=None, help="Database path (default: ~/.garmin-sync/state.db)")
    args = parser.parse_args()

    config_path = args.config or DEFAULT_CONFIG
    db_path = args.db or DEFAULT_DB

    # Handle Launch Agent install/uninstall
    if args.install_agent:
        _install_launch_agent()
        return

    if args.uninstall_agent:
        _uninstall_launch_agent()
        return

    # Handle password update
    if args.update_password:
        _update_password(config_path)
        return

    # Handle reauth
    if args.reauth:
        _reauth(config_path)
        return

    # First-run setup if no config exists
    first_run = not config_path.exists()
    if first_run:
        _ensure_chromium()
        _first_run_setup(config_path)

    # Load config
    with open(config_path) as f:
        raw = yaml.safe_load(f)

    from eufy_garmin_sync.config import AppConfig, EufyConfig, GarminConfig, UserConfig

    users = []
    for u in raw["users"]:
        users.append(UserConfig(
            name=u["name"],
            eufy=EufyConfig(email=u["eufy"]["email"], password=u["eufy"]["password"]),
            garmin=GarminConfig(email=u["garmin"]["email"], password=u["garmin"]["password"]),
        ))

    config = AppConfig(
        sync_interval_minutes=raw.get("sync_interval_minutes", 15),
        log_level=raw.get("log_level", "INFO"),
        users=users,
    )

    # Handle status
    if args.status:
        from eufy_garmin_sync.state import SyncState
        from eufy_garmin_sync.sync import show_status
        state = SyncState(db_path)
        show_status(state, config.users)
        state.close()
        return

    # Run sync
    import logging
    from eufy_garmin_sync.sync import _check_for_updates, _notify, sync_user
    from eufy_garmin_sync.state import SyncState

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Suppress noisy HTTP request logs from httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logger = logging.getLogger("eufy_garmin_sync")

    _check_for_updates()

    backfill = args.backfill_days
    if first_run and backfill is None:
        backfill = 7

    state = SyncState(db_path)

    try:
        total = 0
        failures = []
        for user in config.users:
            try:
                count = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                total += count
                logger.info("User %s: synced %d measurements", user.name, count)
            except Exception as e:
                logger.exception("Failed to sync user %s", user.name)
                failures.append((user.name, str(e)))

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
            _notify("eufy-sync", f"Synced {total} measurement{'s' if total != 1 else ''} to Garmin")

        logger.info("Sync complete. Total measurements synced: %d", total)

        if first_run:
            if total > 0:
                print("")
                print(f"Synced {total} measurements to Garmin Connect.")
            _offer_launch_agent()
            print("")
            print("You're all set! Check the Garmin Connect app to see your data.")

        sys.exit(1 if failures else 0)

    finally:
        state.close()


if __name__ == "__main__":
    main()
