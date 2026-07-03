"""First-run setup wizard, Strava connection, and one-time config migrations."""
from __future__ import annotations

import getpass
import re
import sys
from pathlib import Path

import yaml

from eufy_sync.cli import profiles, shared


def _store_passwords_in_keychain(
    user_name: str,
    eufy_password: str,
    garmin_password: str | None = None,
) -> bool:
    """Store passwords in keychain. Returns True if successful."""
    from eufy_sync.credentials import store_password, _keyring_available
    if not _keyring_available():
        return False
    store_password(f"{user_name}:eufy", eufy_password)
    if garmin_password:
        store_password(f"{user_name}:garmin", garmin_password)
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

    # On a shared account, pick the right person before the first sync.
    try:
        from eufy_sync.config import EufyConfig
        from eufy_sync.eufy_client import EufyClient
        probe = EufyClient(EufyConfig(email=eufy_email, password=eufy_password))
        try:
            probe.authenticate()
            profiles_list = probe.list_profiles()
        finally:
            probe.close()
        if len(profiles_list) > 1:
            user_config["eufy"]["customer_id"] = profiles._prompt_profile_choice(profiles_list)
    except Exception as e:
        # Non-fatal: if this fails, the first sync safely stops and prompts.
        print(f"Note: could not check Eufy profiles right now ({e}).")

    config = {"users": [user_config]}
    shared._write_config(config_path, config)

    print("")
    targets = []
    if garmin_email:
        targets.append("Garmin")
    if strava_config:
        targets.append("Strava")
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
    shared._write_config(config_path, config)

    print("Strava credentials saved. Starting authorization...")

    from eufy_sync.config import StravaConfig
    from eufy_sync.strava_client import authorize_strava
    try:
        authorize_strava(StravaConfig(
            client_id=strava_config["client_id"],
            client_secret=strava_config["client_secret"],
        ))
    except (RuntimeError, OSError) as e:
        print(str(e))
        print("Retry with: eufy-sync --setup-strava")
        sys.exit(1)
    print("Strava connected! Future syncs will update both targets.")


def _migrate_config_passwords(config_path: Path) -> None:
    """One-time migration: move passwords from config.yaml to keychain."""
    from eufy_sync.credentials import store_password, _keyring_available

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
                if re.fullmatch(r"\$\{\w+\}", pw):
                    # A deliberate ${VAR} env-var reference, not a literal
                    # secret - leave it in the YAML for config.py to
                    # interpolate. Storing the literal placeholder string to
                    # the keychain would permanently break the setup, since
                    # the keychain always wins over the YAML afterward.
                    continue
                key = f"{name}:{service}"
                # Always store the YAML value, even over a stale keychain
                # entry - the file edit is the newer intent. Otherwise a
                # corrected password in the file is silently discarded.
                store_password(key, pw)
                del user[service]["password"]
                changed = True

    if changed:
        shared._write_config(config_path, config)
        print("Migrated passwords from config file to system keychain.")


UPGRADE_NOTICE_FILE = shared.DATA_DIR / ".strava_notice_shown"


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
    config_path = shared.DATA_DIR / "config.yaml"
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
