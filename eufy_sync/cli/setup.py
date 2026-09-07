"""First-run setup wizard, Strava connection, and one-time config migrations."""
from __future__ import annotations

import getpass
import re
import sys
import warnings
from contextlib import suppress
from pathlib import Path

import yaml

from eufy_sync.cli import profiles, shared


def _store_passwords(
    user_name: str,
    eufy_password: str,
    garmin_password: str | None = None,
) -> None:
    """Store passwords in the credential store (keychain vault or file)."""
    from eufy_sync.credentials import store_password
    store_password(f"{user_name}:eufy", eufy_password)
    if garmin_password:
        store_password(f"{user_name}:garmin", garmin_password)


def _first_run_setup(config_path: Path) -> None:
    """Interactive setup wizard for first-time users."""
    print("")
    print("  eufy-sync - first time setup")
    print("  Credentials are stored in your system keychain, or a 0o600 file if none is available.")
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

    # Passwords always go to the credential store (keychain vault, or a
    # 0o600 file when there is no keychain) - never into config.yaml.
    _store_passwords(user_name, eufy_password, garmin_password)

    from eufy_sync.credentials import active_store_label, store_password
    if strava_config:
        store_password(f"{user_name}:strava", strava_config["client_secret"])
    print(f"Passwords saved to the {active_store_label()}.")

    # Config YAML stores only emails and the public client id, never secrets.
    user_config: dict = {
        "name": user_name,
        "eufy": {"email": eufy_email},
    }
    if garmin_email:
        user_config["garmin"] = {"email": garmin_email}
    if strava_config:
        user_config["strava"] = {"client_id": strava_config["client_id"]}

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
    user_name = user.get("name", "default")

    from eufy_sync.credentials import store_password
    store_password(f"{user_name}:strava", strava_config["client_secret"])

    # Only the public client id belongs in the YAML. Drop a secret an earlier
    # version wrote there, or it would linger in plaintext beside the new one.
    user["strava"] = {"client_id": strava_config["client_id"]}
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


def _zwift_setup_password(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            print("A hidden password prompt is unavailable. Retry setup in an interactive terminal.")
            sys.exit(1)


def _setup_zwift(config_path: Path) -> None:
    """Validate Zwift credentials, then enable experimental weight sync."""
    fresh_install = not config_path.exists()
    eufy_password = None
    if fresh_install:
        if not sys.stdin.isatty():
            print("First-time Zwift setup requires an interactive terminal.")
            sys.exit(1)
        print("")
        print("  eufy-sync - first time setup with Zwift")
        eufy_email = input("Eufy email: ").strip()
        if not eufy_email:
            print("Error: Eufy email is required.")
            sys.exit(1)
        eufy_password = _zwift_setup_password("Eufy password: ")
        if not eufy_password:
            print("Error: Eufy password is required.")
            sys.exit(1)
        config = {"users": [{"name": "default", "eufy": {"email": eufy_email}}]}
    else:
        with open(config_path) as f:
            config = yaml.safe_load(f)

    user = config["users"][0]
    user_name = user.get("name", "default")

    from eufy_sync import credentials
    vault = credentials._load_vault()
    probe = vault.get("tokens", {}).get("zwift_probe")
    password_account = f"{user_name}:zwift"
    existing = user.get("zwift") or {}
    existing_email = existing.get("email")
    existing_password = vault.get("passwords", {}).get(password_account)
    probe_is_current_user = (
        isinstance(probe, dict)
        and probe.get("user_name") == user_name
        and probe.get("password_account") == password_account
        and (not existing_email or probe.get("email") == existing_email)
    )
    probe_email = probe.get("email") if probe_is_current_user else None
    probe_password = vault.get("passwords", {}).get(password_account) if probe_is_current_user else None

    print("")
    print("  Experimental Zwift weight sync")
    print("  This updates your current weight in Zwift. It does not upload body composition or weight history.")
    print("  Credentials are stored in your existing credential vault and are never written to config.yaml.")
    print("")

    manual_credentials = False
    if existing_email and existing_password:
        email = existing_email
        password = existing_password
        print(f"Using the configured Zwift account for {email}.")
    elif probe_email and probe_password:
        email = probe_email
        password = probe_password
        print(f"Using the validated Zwift account for {email}.")
    else:
        if not sys.stdin.isatty():
            print("No validated Zwift credentials were found. Run --setup-zwift in an interactive terminal.")
            sys.exit(1)
        email = input("Zwift email: ").strip()
        if not email:
            print("Error: Zwift email is required.")
            sys.exit(1)
        password = _zwift_setup_password("Zwift password: ")
        if not password:
            print("Error: Zwift password is required.")
            sys.exit(1)
        manual_credentials = True

    from eufy_sync.config import ZwiftConfig
    from eufy_sync.zwift_client import ZwiftClient

    client = ZwiftClient(ZwiftConfig(email=email, password=password))
    try:
        client.authenticate(force=manual_credentials)
        client.check_connection()
    except Exception as e:
        print(f"Zwift connection failed: {e}")
        print("Nothing was enabled. Retry with: eufy-sync --setup-zwift")
        sys.exit(1)
    finally:
        with suppress(Exception):
            client.close()

    # Authentication succeeded, so this is now the production account. Keep
    # its password under the stable per-user key that config.py resolves.
    credentials.store_password(f"{user_name}:zwift", password)
    user["zwift"] = {"email": email}

    if fresh_install:
        credentials.store_password(f"{user_name}:eufy", eufy_password)
        try:
            from eufy_sync.config import EufyConfig
            from eufy_sync.eufy_client import EufyClient
            eufy = EufyClient(EufyConfig(email=user["eufy"]["email"], password=eufy_password))
            try:
                eufy.authenticate()
                profiles_list = eufy.list_profiles()
            finally:
                eufy.close()
            if len(profiles_list) > 1:
                user["eufy"]["customer_id"] = profiles._prompt_profile_choice(profiles_list)
        except Exception as e:
            print(f"Note: could not check Eufy profiles right now ({e}).")

    shared._write_config(config_path, config)
    print("Zwift connected. Experimental sync will update current weight only.")


def _migrate_config_passwords(config_path: Path) -> None:
    """One-time migration: move passwords from config.yaml to the credential store."""
    from eufy_sync.credentials import store_password

    if not config_path.exists():
        return

    with open(config_path) as f:
        config = yaml.safe_load(f)

    changed = False
    for user in config.get("users", []):
        name = user.get("name", "default")
        for service in ["eufy", "garmin", "zwift"]:
            # A hand-edited `eufy:` with nothing under it parses to None, not to
            # a mapping, and the default in .get only applies to a missing key.
            # Reading .get("password") off that None crashed every command that
            # runs the migration, which is all of them.
            pw = (user.get(service) or {}).get("password")
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

        # The Strava API client secret is a secret like any other, but it sat
        # in the YAML until now. Same env-reference rule: a ${VAR} placeholder
        # is a deliberate indirection, and storing it literally would win over
        # the YAML forever and break the setup.
        secret = (user.get("strava") or {}).get("client_secret")
        if secret and not re.fullmatch(r"\$\{\w+\}", secret):
            store_password(f"{name}:strava", secret)
            del user["strava"]["client_secret"]
            changed = True

    if changed:
        shared._write_config(config_path, config)
        print("Migrated passwords from config file to the credential store.")


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
