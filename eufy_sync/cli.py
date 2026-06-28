"""CLI entry point for eufy-sync.

Handles first-run setup, syncing, status checks, re-authentication,
and multi-target support (Garmin Connect and Strava).
"""
from __future__ import annotations

import getpass
import json
import os
import platform
import re
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
        cache_file = DATA_DIR / "update_check"
        now = time.time()

        if cache_file.exists():
            last_check = float(cache_file.read_text().strip())
            if now - last_check < UPDATE_CHECK_INTERVAL:
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
            _notify("eufy-sync", f"Update available: v{latest}. Run: eufy-sync --update")

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

    if shutil.which("pipx"):
        cmd = ["pipx", "install", "--force", f"eufy-sync=={latest}"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", f"eufy-sync=={latest}"]

    print(f"Updating eufy-sync from v{__version__} to v{latest}...")
    result = subprocess.run(cmd)
    if result.returncode == 0:
        print(f"Updated to v{latest}.")
    else:
        print("Update failed. Run manually: " + " ".join(cmd))


def _write_config(path: Path, config: dict) -> None:
    """Write config with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def _store_passwords_in_keychain(
    user_name: str,
    eufy_password: str,
    garmin_password: str | None = None,
    zwift_password: str | None = None,
) -> bool:
    """Store passwords in keychain. Returns True if successful."""
    from eufy_sync.credentials import store_password, _keyring_available
    if not _keyring_available():
        return False
    store_password(f"{user_name}:eufy", eufy_password)
    if garmin_password:
        store_password(f"{user_name}:garmin", garmin_password)
    if zwift_password:
        store_password(f"{user_name}:zwift", zwift_password)
    return True


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

    # Zwift setup (optional, unofficial)
    print("")
    zwift_answer = input("Connect Zwift? [y/N] ").strip()
    zwift_email = None
    zwift_password = None
    if zwift_answer.lower().startswith("y"):
        print("")
        print("  Note: Zwift has no official API. eufy-sync uses a")
        print("  community-reverse-engineered endpoint and may break with any Zwift update.")
        print("")
        zwift_email = input("Zwift email (Enter if same as Eufy): ").strip()
        if not zwift_email:
            zwift_email = eufy_email
        zwift_password = getpass.getpass("Zwift password: ")
        if not zwift_password:
            print("Error: Zwift password is required.")
            sys.exit(1)

    if not garmin_email and not strava_config and not zwift_email:
        print("Error: You must configure at least one sync target (Garmin, Strava, or Zwift).")
        sys.exit(1)

    user_name = "default"

    # Store passwords in keychain
    keychain_ok = _store_passwords_in_keychain(user_name, eufy_password, garmin_password, zwift_password)

    # Config YAML stores only emails (no passwords) when keychain is available
    user_config: dict = {
        "name": user_name,
        "eufy": {"email": eufy_email},
    }
    if garmin_email:
        user_config["garmin"] = {"email": garmin_email}
    if strava_config:
        user_config["strava"] = strava_config
    if zwift_email:
        user_config["zwift"] = {"email": zwift_email}
    if not keychain_ok:
        # Fallback: store passwords in config file (with 0o600 permissions)
        user_config["eufy"]["password"] = eufy_password
        if garmin_password:
            user_config["garmin"]["password"] = garmin_password
        if zwift_password:
            user_config["zwift"]["password"] = zwift_password
        print("Warning: keychain not available, passwords stored in config file.")
    else:
        print("Passwords saved to system keychain.")

    # On a shared account, pick the right person before the first sync.
    try:
        from eufy_sync.config import EufyConfig
        from eufy_sync.eufy_client import EufyClient
        probe = EufyClient(EufyConfig(email=eufy_email, password=eufy_password))
        try:
            probe.authenticate()
            profiles = probe.list_profiles()
        finally:
            probe.close()
        if len(profiles) > 1:
            user_config["eufy"]["customer_id"] = _prompt_profile_choice(profiles)
    except Exception as e:
        # Non-fatal: if this fails, the first sync safely stops and prompts.
        print(f"Note: could not check Eufy profiles right now ({e}).")

    config = {"users": [user_config]}
    _write_config(config_path, config)

    print("")
    targets = []
    if garmin_email:
        targets.append("Garmin")
    if strava_config:
        targets.append("Strava")
    if zwift_email:
        targets.append("Zwift")
    print(f"Saved. Running first sync to {' and '.join(targets)} (last 7 days)...")
    if garmin_email:
        print("Logging in to Garmin (a browser may open if the direct login is rate-limited).")
    print("")

    # Run Strava OAuth if configured
    if strava_config:
        try:
            from eufy_sync.config import StravaConfig
            from eufy_sync.strava_client import authorize_strava
            authorize_strava(StravaConfig(
                client_id=strava_config["client_id"],
                client_secret=strava_config["client_secret"],
            ))
        except Exception as e:
            print(f"Warning: Strava authorization failed: {e}")
            print("You can retry later with: eufy-sync --setup-strava")


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


def _setup_zwift(config_path: Path) -> None:
    """Add or update Zwift configuration."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    print("")
    print("  Note: Zwift has no official API. eufy-sync uses a")
    print("  community-reverse-engineered endpoint and may break with any Zwift update.")
    print("")

    user = config["users"][0]
    user_name = user.get("name", "default")
    eufy_email = user["eufy"]["email"]

    zwift_email = input("Zwift email (Enter if same as Eufy): ").strip() or eufy_email
    zwift_password = getpass.getpass("Zwift password: ")
    if not zwift_password:
        print("Error: Zwift password is required.")
        sys.exit(1)

    from eufy_sync.credentials import store_password, _keyring_available
    if _keyring_available():
        store_password(f"{user_name}:zwift", zwift_password)
        user["zwift"] = {"email": zwift_email}
    else:
        user["zwift"] = {"email": zwift_email, "password": zwift_password}

    _write_config(config_path, config)
    print("Zwift connected. Future syncs will update your Zwift profile weight.")


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


def _format_profile(profile: "EufyProfile", index: int) -> str:
    lb = profile.last_weight_kg * 2.20462
    when = profile.last_measured.strftime("%Y-%m-%d")
    label = profile.name or f"profile ...{profile.customer_id[-4:]}"
    return f"  {index}. {label}  -  {profile.last_weight_kg:.1f} kg ({lb:.1f} lb), last weigh-in {when}"


def _prompt_profile_choice(profiles: list) -> str:
    """Print the profiles and return the customer_id the user picks."""
    print("")
    print("Multiple profiles were found on this Eufy account:")
    for i, p in enumerate(profiles, 1):
        print(_format_profile(p, i))
    print("")
    while True:
        choice = input(f"Which profile is yours? [1-{len(profiles)}] ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1].customer_id
        print("Enter a number from the list.")


def _save_customer_id(config_path: Path, customer_id: str) -> None:
    """Persist the chosen Eufy customer_id into the config file (single user)."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    user = config["users"][0]
    user.setdefault("eufy", {})
    user["eufy"]["customer_id"] = customer_id
    _write_config(config_path, config)


def _select_profile(config_path: Path) -> None:
    """Choose which Eufy profile to sync, for an existing install."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    from eufy_sync.config import load_config
    from eufy_sync.eufy_client import EufyClient

    cfg = load_config(config_path)
    eufy = EufyClient(cfg.users[0].eufy)
    try:
        eufy.authenticate()
        profiles = eufy.list_profiles()
    except Exception as e:
        print(f"Could not reach Eufy: {e}")
        sys.exit(1)
    finally:
        eufy.close()

    if not profiles:
        print("No profiles found yet. Weigh in and open the Eufy app, then try again.")
        return

    if len(profiles) == 1:
        _save_customer_id(config_path, profiles[0].customer_id)
        print("Only one profile found on this account; saved it as yours.")
        return

    _save_customer_id(config_path, _prompt_profile_choice(profiles))
    print("Saved. Future syncs will use only your profile.")


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
    zwift_pw = getpass.getpass("New Zwift password: ") if "zwift" in user else ""

    if not eufy_pw and not garmin_pw and not zwift_pw:
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

    if zwift_pw:
        if keychain_ok:
            store_password(f"{user_name}:zwift", zwift_pw)
        else:
            user["zwift"]["password"] = zwift_pw

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

    if zwift_pw:
        if keychain_ok:
            delete_token("zwift")
        zwift_token = DATA_DIR / "zwift_token.json"
        if zwift_token.exists():
            zwift_token.unlink()

    changed = []
    if eufy_pw:
        changed.append("Eufy")
    if garmin_pw:
        changed.append("Garmin")
    if zwift_pw:
        changed.append("Zwift")
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
    do_zwift = (target is None or target == "zwift") and "zwift" in user

    if target and not do_garmin and not do_strava and not do_zwift:
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
                print("Garmin is already connected. Re-authenticate anyway? [y/N] ", end="")
                if sys.stdin.isatty():
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
        authorize_strava(strava_cfg)
        print("Done - Strava tokens saved.")

    if do_zwift:
        from eufy_sync.config import ZwiftConfig, _get_password
        from eufy_sync.credentials import _keyring_available, delete_token
        from eufy_sync.zwift_client import ZwiftClient
        zwift_email = user["zwift"]["email"]
        zwift_pw = _get_password(user_name, "zwift", zwift_email, user["zwift"].get("password"))
        # Force fresh password-grant login by clearing cached tokens
        if _keyring_available():
            delete_token("zwift")
        token_file = DATA_DIR / "zwift_token.json"
        if token_file.exists():
            token_file.unlink()
        client = ZwiftClient(ZwiftConfig(email=zwift_email, password=zwift_pw))
        client.authenticate()
        client.close()
        print("Done - Zwift tokens saved.")


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

    # Clear keychain entries for every user named in the config
    user_names = ["default"]
    config_path = data_dir / "config.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f) or {}
            names = [u.get("name", "default") for u in raw.get("users", [])]
            if names:
                user_names = names
        except Exception:
            pass

    from eufy_sync.credentials import delete_password, delete_token, _keyring_available
    if _keyring_available():
        for name in user_names:
            for suffix in ["eufy", "garmin", "zwift"]:
                delete_password(f"{name}:{suffix}")
        delete_token("eufy")
        delete_token("garmin")
        delete_token("strava")
        delete_token("zwift")

    # Remove data directory (preserving DB if requested)
    if data_dir.exists():
        if keep_db and db_path.exists():
            # Remove everything except state.db
            for item in data_dir.iterdir():
                if item.name != "state.db":
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
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
        if status["state"] == "valid":
            parts.append("Garmin connected")
        else:
            parts.append("Garmin not connected")

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

    if user.zwift:
        from eufy_sync.zwift_client import ZwiftClient
        zwift_status = ZwiftClient(user.zwift).token_status()
        if zwift_status["state"] == "no_session":
            parts.append("Zwift: not authorized")
        elif zwift_status["state"] == "expired":
            parts.append("Zwift token EXPIRED")
        elif zwift_status["state"] == "refresh_needed":
            parts.append("Zwift token refresh pending")
        else:
            parts.append("Zwift connected")

    print(" | ".join(parts))
    print(
        "If you weighed in recently and it isn't here, open the Eufy app so it "
        "uploads to the cloud, then run eufy-sync again."
    )


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
            if status["state"] == "valid":
                print("Garmin auth: valid (auto-refreshes; re-login only if it expires)")
            else:
                print("Garmin auth: not connected - run: eufy-sync --reauth")

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

        # Zwift token health (unofficial)
        if user.zwift:
            from eufy_sync.zwift_client import ZwiftClient
            zwift_status = ZwiftClient(user.zwift).token_status()
            if zwift_status["state"] == "no_session":
                print("Zwift auth: not authorized - first sync will log in")
            elif zwift_status["state"] == "expired":
                print("Zwift auth: EXPIRED - re-authorize with --reauth zwift")
            elif zwift_status["state"] == "refresh_needed":
                print("Zwift auth: access token expired, will refresh on next sync")
            else:
                print("Zwift auth: valid (refresh token active)")


def _show_history(state, users: list, limit: int = 14) -> None:
    """Print recent sync history as a table."""
    KG_TO_LB = 2.20462

    for user in users:
        history = state.get_history(user.name, limit=limit)
        if not history:
            print("No sync history yet.")
            return

        # Determine which targets are in the data
        all_targets = set()
        for entry in history:
            all_targets.update(entry["targets"])
        target_cols = sorted(all_targets)

        # Header
        header = f"{'Date':<12} {'Weight':<22}"
        for t in target_cols:
            header += f" {t.capitalize():<8}"
        print(header)
        print("-" * len(header))

        # Rows
        for entry in history:
            ts = entry["timestamp"]
            date_str = ts[:10] if len(ts) >= 10 else ts
            kg = entry["weight_kg"]
            lb = kg * KG_TO_LB
            weight_str = f"{kg:.2f} kg ({lb:.1f} lb)"

            row = f"{date_str:<12} {weight_str:<22}"
            for t in target_cols:
                mark = "✓" if t in entry["targets"] else "-"
                row += f" {mark:<8}"
            print(row)

        print("")
        print("Weight from Eufy cloud API. May differ from your scale display by up to ~0.5 lb.")


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
    # Don't show if Strava is already configured
    config_path = DATA_DIR / "config.yaml"
    if config_path.exists():
        try:
            with open(config_path) as f:
                raw = yaml.safe_load(f)
            if any("strava" in u for u in raw.get("users", [])):
                return
        except Exception:
            pass
    if sys.stdin.isatty():
        print("New in eufy-sync: Strava support! Run eufy-sync --setup-strava to connect.")
        print("")


def main() -> None:
    import argparse

    from eufy_sync import __version__

    parser = argparse.ArgumentParser(
        prog="eufy-sync",
        description="Sync Eufy smart scale data to Garmin Connect, Strava, and Zwift",
    )
    parser.add_argument("--version", "-V", action="version", version=f"eufy-sync {__version__}")
    parser.add_argument("--status", action="store_true", help="Show sync status and token health")
    parser.add_argument("--reauth", nargs="?", const="all", default=None, metavar="TARGET",
                        help="Re-authenticate (optionally: garmin, strava, or zwift)")
    parser.add_argument("--setup-strava", action="store_true", help="Connect Strava to your account")
    parser.add_argument("--setup-zwift", action="store_true", help="Connect Zwift to your account")
    parser.add_argument("--select-profile", action="store_true", help="Choose which Eufy profile to sync")
    parser.add_argument("--update-password", action="store_true", help="Change stored passwords")
    parser.add_argument("--update", action="store_true", help="Update eufy-sync to the latest version")
    parser.add_argument("--history", nargs="?", const=14, type=int, default=None, metavar="N",
                        help="Show recent sync history, last N entries (default: 14)")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync last N days")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--headless", action="store_true", help="Never prompt; fail with a reauth message if login is needed (for Launch Agent)")
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

    # Handle self-update
    if args.update:
        _self_update()
        return

    # Handle Strava setup
    if args.setup_strava:
        _setup_strava(config_path)
        return

    # Handle Zwift setup
    if args.setup_zwift:
        _setup_zwift(config_path)
        return

    # Handle profile selection
    if args.select_profile:
        _select_profile(config_path)
        return

    # Handle password update
    if args.update_password:
        _update_password(config_path)
        return

    # Handle reauth
    if args.reauth is not None:
        _configure_logging(args.verbose)
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

    has_garmin = any(u.garmin for u in config.users)

    # Handle status
    if args.status:
        from eufy_sync.state import SyncState
        state = SyncState(db_path)
        _show_status(state, config.users)
        state.close()
        return

    # Handle history
    if args.history is not None:
        from eufy_sync.state import SyncState
        state = SyncState(db_path)
        _show_history(state, config.users, limit=args.history)
        state.close()
        return

    # Run sync
    import logging
    from eufy_sync.sync import sync_user
    from eufy_sync.state import SyncState
    from eufy_sync.eufy_client import AmbiguousProfileError

    _configure_logging(args.verbose)
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
                counts, errors = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                for target_name, count in counts.items():
                    total_counts[target_name] = total_counts.get(target_name, 0) + count
                for target_name, err in errors.items():
                    failures.append((f"{user.name}/{target_name}", err))
                logger.info("User %s: synced %s, errors %s", user.name, counts, errors)
            except AmbiguousProfileError as e:
                interactive = not args.headless and sys.stdin.isatty()
                if interactive:
                    # _prompt_profile_choice prints the list and asks for a pick.
                    customer_id = _prompt_profile_choice(e.profiles)
                    _save_customer_id(config_path, customer_id)
                    user.eufy.customer_id = customer_id
                    print("Saved. Syncing your profile now...")
                    try:
                        counts, errors = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                        for target_name, count in counts.items():
                            total_counts[target_name] = total_counts.get(target_name, 0) + count
                        for target_name, err in errors.items():
                            failures.append((f"{user.name}/{target_name}", err))
                        logger.info("User %s: synced %s, errors %s", user.name, counts, errors)
                    except Exception as retry_error:
                        logger.exception("Failed to sync user %s after profile selection", user.name)
                        failures.append((user.name, str(retry_error)))
                else:
                    print("")
                    print("Multiple profiles were found on this Eufy account:")
                    for i, p in enumerate(e.profiles, 1):
                        print(_format_profile(p, i))
                    print("")
                    print("Nothing was synced. Choose your profile with: eufy-sync --select-profile")
                    failures.append((user.name, "multiple Eufy profiles; run eufy-sync --select-profile"))
            except Exception as e:
                logger.exception("Failed to sync user %s", user.name)
                failures.append((user.name, str(e)))

        total = sum(total_counts.values())

        if failures:
            reauth_needed = any("re-authenticate" in err for _, err in failures)
            eufy_password = any("changed your Eufy password" in err for _, err in failures)
            multiple_profiles = any("multiple Eufy profiles" in err for _, err in failures)
            if reauth_needed:
                _notify("eufy-sync: re-login needed", "Run: eufy-sync --reauth")
            elif eufy_password:
                _notify("eufy-sync: Eufy login failed", "Run: eufy-sync --update-password")
            elif multiple_profiles:
                _notify("eufy-sync: choose your profile", "Run: eufy-sync --select-profile")
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
