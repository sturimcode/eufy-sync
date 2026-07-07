"""Password updates, re-auth, and the macOS Launch Agent lifecycle."""
from __future__ import annotations

import getpass
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

from eufy_sync.cli import shared


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
    garmin_pw = getpass.getpass("New Garmin password: ") if "garmin" in user else ""

    if not eufy_pw and not garmin_pw:
        print("No changes made.")
        return

    from eufy_sync.credentials import store_password, delete_token

    if eufy_pw:
        store_password(f"{user_name}:eufy", eufy_pw)

    if garmin_pw:
        store_password(f"{user_name}:garmin", garmin_pw)

    # Clear cached tokens for changed services
    if eufy_pw:
        delete_token("eufy")
        eufy_token = shared.DATA_DIR / "eufy_token.json"
        if eufy_token.exists():
            eufy_token.unlink()

    if garmin_pw:
        delete_token("garmin")
        garmin_session = shared.DATA_DIR / "session.json"
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
                if not sys.stdin.isatty():
                    # Honor the documented default (No) when there's no one
                    # to answer the prompt, instead of silently proceeding
                    # as "yes" and destroying a valid token.
                    print("Garmin re-auth skipped (already connected; run interactively to force).")
                    do_garmin = False
                else:
                    print("Garmin is already connected. Re-authenticate anyway? [y/N] ", end="")
                    answer = input().strip()
                    if not answer.lower().startswith("y"):
                        print("Garmin re-auth skipped.")
                        do_garmin = False

        if do_garmin:
            from eufy_sync.sync import PermanentSyncError
            try:
                auth.force_reauth()
                print("Done - Garmin tokens saved.")
            except PermanentSyncError as e:
                print(str(e))
                sys.exit(1)

    if do_strava:
        from eufy_sync.config import StravaConfig
        from eufy_sync.strava_client import authorize_strava
        strava_cfg = StravaConfig(
            client_id=str(user["strava"]["client_id"]),
            client_secret=user["strava"]["client_secret"],
        )
        try:
            authorize_strava(strava_cfg)
        except (RuntimeError, OSError) as e:
            print(str(e))
            print("Retry with: eufy-sync --reauth strava")
            sys.exit(1)
        print("Done - Strava tokens saved.")


def _write_run_script(binary_path: str) -> Path:
    """Write the stable wrapper script the Launch Agent runs.

    macOS re-announces "can run in the background" whenever a registered
    background item's executable changes identity, and pipx/uv replace the
    binary on every update. The agent therefore points at this script, whose
    bytes never change across updates, so the announcement fires once, not
    once per release. Skipping the rewrite when content is unchanged is what
    keeps the file's identity stable.
    """
    script_path = shared.DATA_DIR / "run-sync.sh"
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
    <string>{shared.LAUNCH_AGENT_LABEL}</string>

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

    already_installed = shared.LAUNCH_AGENT_PATH.exists()

    # Ensure the log directory exists with restricted permissions
    shared.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)

    wrapper = _write_run_script(binary)
    shared.LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    shared.LAUNCH_AGENT_PATH.write_text(_generate_plist(str(wrapper)))

    # Unload first in case an old version is loaded
    subprocess.run(
        ["launchctl", "unload", str(shared.LAUNCH_AGENT_PATH)],
        capture_output=True,
    )
    subprocess.run(
        ["launchctl", "load", str(shared.LAUNCH_AGENT_PATH)],
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
    if not shared.LAUNCH_AGENT_PATH.exists():
        print("No Launch Agent installed.")
        return

    subprocess.run(
        ["launchctl", "unload", str(shared.LAUNCH_AGENT_PATH)],
        capture_output=True,
    )
    shared.LAUNCH_AGENT_PATH.unlink()
    print("Launch Agent removed. Auto-sync disabled.")


def _uninstall(data_dir: Path, config_path: Path | None = None, db_path: Path | None = None) -> None:
    """Remove all eufy-sync data: Launch Agent, config, tokens, state DB.

    config_path/db_path default to the standard files under data_dir, but a
    custom --config/--db location (outside data_dir) is also deleted so
    --uninstall does not leave those files behind.
    """
    if not sys.stdin.isatty():
        print("Error: --uninstall requires an interactive terminal.")
        sys.exit(1)

    print("This will remove:")
    print(f"  - All saved credentials and tokens in {data_dir}/")
    print(f"  - Keychain entries for eufy-sync")
    print(f"  - Sync history database")
    if shared.LAUNCH_AGENT_PATH.exists():
        print(f"  - Automatic sync Launch Agent")
    print("")

    answer = input("Are you sure? [y/N] ").strip()
    if not answer.lower().startswith("y"):
        print("Cancelled.")
        return

    default_config_path = data_dir / "config.yaml"
    default_db_path = data_dir / "state.db"
    config_path = config_path or default_config_path
    db_path = db_path or default_db_path

    # Offer to keep state DB so reinstalls don't duplicate measurements
    keep_db = False
    if db_path.exists():
        print("")
        keep_answer = input("Keep sync history? Prevents duplicates if you reinstall later. [Y/n] ").strip()
        keep_db = not keep_answer.lower().startswith("n")

    # Stop and remove Launch Agent
    if shared.LAUNCH_AGENT_PATH.exists():
        subprocess.run(["launchctl", "unload", str(shared.LAUNCH_AGENT_PATH)], capture_output=True)
        shared.LAUNCH_AGENT_PATH.unlink()

    # Clear keychain entries for every user named in the config
    user_names = ["default"]
    if config_path.exists():
        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            names = [u.get("name", "default") for u in raw.get("users", [])]
            if names:
                user_names = names
        except Exception:
            pass

    # Clear the keychain vault. On a file-backend machine this gate skips the
    # deletes, which is safe only because credentials.json lives inside data_dir
    # and is erased by the rmtree below; keep them together if CRED_FILE ever
    # moves outside ~/.garmin-sync.
    from eufy_sync.credentials import delete_password, delete_token, _keyring_available
    if _keyring_available():
        # Best-effort: a locked keychain makes the vault read raise, and a
        # half-finished uninstall that leaves the data dir behind (the rmtree
        # is below) plus a raw traceback is worse than skipping this. The
        # rmtree still erases a file-backed vault under data_dir.
        try:
            for name in user_names:
                for suffix in ["eufy", "garmin"]:
                    delete_password(f"{name}:{suffix}")
            delete_token("eufy")
            delete_token("garmin")
            delete_token("strava")
        except Exception:
            print("Note: could not clear keychain entries (the keychain may be locked).")

    # Remove data directory (preserving DB if requested)
    if data_dir.exists():
        if keep_db and db_path.exists() and db_path == default_db_path:
            # Remove everything except state.db
            for item in data_dir.iterdir():
                if item.name != "state.db":
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
        else:
            shutil.rmtree(data_dir)

    # A custom --config/--db path lives outside data_dir, so it survives the
    # rmtree above and must be removed explicitly.
    if config_path != default_config_path and config_path.exists():
        config_path.unlink()
    if db_path != default_db_path and not keep_db and db_path.exists():
        db_path.unlink()

    print("")
    if keep_db:
        print(f"Removed all eufy-sync data (sync history kept in {db_path}).")
    else:
        print("Removed all eufy-sync data.")

    if "/uv/tools/" in sys.executable:
        removal_cmd = "uv tool uninstall eufy-sync"
    elif shutil.which("pipx"):
        removal_cmd = "pipx uninstall eufy-sync"
    else:
        removal_cmd = "pip uninstall eufy-sync"
    print(f"To remove the package itself, run: {removal_cmd}")
