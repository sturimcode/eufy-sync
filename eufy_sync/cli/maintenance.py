"""Password updates, re-auth, and the scheduled-sync agent lifecycle."""
from __future__ import annotations

import getpass
import shutil
import sys
from pathlib import Path

import yaml

from eufy_sync import install, platform_support
from eufy_sync.cli import shared
from eufy_sync.prompt import PROMPT_TIMEOUT_SECONDS, input_with_timeout


def _update_password(config_path: Path) -> None:
    """Update stored passwords, each one only after it logs in."""
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

    from eufy_sync.credentials import store_password

    # Each new password logs in before it is stored. The login saves its own
    # token on success; a failure (a typo, a cancelled MFA prompt, no network)
    # leaves that service's stored password and token as they were, so a
    # mistake here cannot end a session that was still working.
    changes = [
        (label, password, verify)
        for label, password, verify in (
            ("Eufy", eufy_pw, _verify_eufy_password),
            ("Garmin", garmin_pw, _verify_garmin_password),
            ("Zwift", zwift_pw, _verify_zwift_password),
        )
        if password
    ]
    updated = []
    for label, password, verify in changes:
        print(f"Checking the new {label} password...")
        try:
            verify(user, password)
        except Exception as e:
            print(f"{label} login with the new password failed: {e}")
            print(f"The stored {label} password and login were left unchanged.")
            if updated:
                print(f"Already updated: {' and '.join(updated)}.")
            print("Retry with: eufy-sync --update-password")
            sys.exit(1)
        store_password(f"{user_name}:{label.lower()}", password)
        updated.append(label)

    print(f"{' and '.join(updated)} password{'s' if len(updated) > 1 else ''} updated.")


def _verify_eufy_password(user: dict, password: str) -> None:
    """Log in to Eufy with password; the token is replaced only on success."""
    from eufy_sync.config import EufyConfig
    from eufy_sync.eufy_client import EufyClient

    client = EufyClient(
        EufyConfig(email=user["eufy"]["email"], password=password),
        token_path=shared.DATA_DIR / "eufy_token.json",
    )
    try:
        # Skip the cached token on purpose: it says nothing about the password.
        client._fresh_login()
    finally:
        client.close()


def _verify_garmin_password(user: dict, password: str) -> None:
    """Log in to Garmin with password, prompting for MFA if Garmin asks.
    force_reauth replaces the stored token only after the login succeeds."""
    from eufy_sync.garmin_auth import GarminAuth

    auth = GarminAuth(user["garmin"]["email"], password, session_path=shared.DATA_DIR / "session.json")
    auth.force_reauth()
    print("Done - Garmin tokens saved.")


def _verify_zwift_password(user: dict, password: str) -> None:
    """Log in to Zwift with password. A forced login reads the profile before
    it saves anything, so the cached token is replaced only on success."""
    from eufy_sync.config import ZwiftConfig
    from eufy_sync.zwift_client import ZwiftClient

    client = ZwiftClient(ZwiftConfig(email=user["zwift"]["email"], password=password))
    try:
        client.authenticate(force=True)
    finally:
        client.close()
    print("Done - Zwift token saved.")


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
                if not sys.stdin.isatty():
                    # Honor the documented default (No) when there's no one
                    # to answer the prompt, instead of silently proceeding
                    # as "yes" and destroying a valid token.
                    print("Garmin re-auth skipped (already connected; run interactively to force).")
                    do_garmin = False
                else:
                    print("Garmin is already connected. Re-authenticate anyway? [y/N] ", end="", flush=True)
                    answer = input_with_timeout("", PROMPT_TIMEOUT_SECONDS)
                    if answer is None:
                        # This prompt is reached from a failure toast, so the
                        # window can sit open with nobody reading it. Waiting
                        # forever once kept the process (and its lock on the
                        # tool venv) alive for hours; the default is No anyway.
                        print("")
                        print("No answer after 5 minutes; keeping the current Garmin login.")
                        do_garmin = False
                    elif not answer.strip().lower().startswith("y"):
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
        from eufy_sync.config import StravaConfig, _get_strava_secret
        from eufy_sync.strava_client import authorize_strava
        strava_cfg = StravaConfig(
            client_id=str(user["strava"]["client_id"]),
            client_secret=_get_strava_secret(user_name, user["strava"].get("client_secret")),
        )
        try:
            authorize_strava(strava_cfg)
        except (RuntimeError, OSError) as e:
            print(str(e))
            print("Retry with: eufy-sync --reauth strava")
            sys.exit(1)
        print("Done - Strava tokens saved.")

    if do_zwift:
        from eufy_sync.config import ZwiftConfig, _get_password
        from eufy_sync.zwift_client import ZwiftClient

        zwift_email = user["zwift"]["email"]
        zwift_cfg = ZwiftConfig(
            email=zwift_email,
            password=_get_password(user_name, "zwift", zwift_email, user["zwift"].get("password")),
        )
        client = ZwiftClient(zwift_cfg)
        try:
            client.authenticate(force=True)
            client.check_connection()
        except Exception as e:
            print(f"Zwift re-authentication failed: {e}")
            print("Retry with: eufy-sync --reauth zwift")
            sys.exit(1)
        finally:
            client.close()
        print("Done - Zwift token saved.")


def _disconnect_zwift(config_path: Path) -> None:
    """Remove only Zwift configuration and credentials."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    user = config["users"][0]
    if "zwift" not in user:
        print("Zwift is not configured.")
        return

    user_name = user.get("name", "default")
    del user["zwift"]
    shared._write_config(config_path, config)

    from eufy_sync.credentials import delete_password, delete_token
    delete_password(f"{user_name}:zwift")
    delete_token("zwift")
    delete_token("zwift_probe")
    print("Zwift disconnected. Other sync targets are unchanged.")


def _disconnect_intervals(config_path: Path) -> None:
    """Remove only the Intervals.icu configuration and its API key."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)
    with open(config_path) as f:
        config = yaml.safe_load(f)
    user = config["users"][0]
    if "intervals" not in user:
        print("Intervals.icu is not configured.")
        return

    user_name = user.get("name", "default")
    del user["intervals"]
    shared._write_config(config_path, config)

    from eufy_sync.credentials import delete_password
    delete_password(f"{user_name}:intervals")
    print("Intervals.icu disconnected. Other sync targets are unchanged.")


def _install_launch_agent() -> None:
    """Install the scheduled-sync agent for the current platform."""
    platform_support.install_agent()


def _offer_launch_agent() -> None:
    """Offer to install the scheduled-sync agent after first-run setup."""
    platform_support.offer_agent()


def _uninstall_launch_agent() -> None:
    """Remove the scheduled-sync agent for the current platform."""
    platform_support.uninstall_agent()


def _uninstall(data_dir: Path, config_path: Path | None = None, db_path: Path | None = None) -> bool:
    """Remove all eufy-sync data: Launch Agent, config, tokens, state DB.

    config_path/db_path default to the standard files under data_dir, but a
    custom --config/--db location (outside data_dir) is also deleted so
    --uninstall does not leave those files behind.

    Returns True when the data was removed, False when the user cancelled.
    """
    if not sys.stdin.isatty():
        print("Error: --uninstall requires an interactive terminal.")
        sys.exit(1)

    print("This will remove:")
    print(f"  - All saved credentials and tokens in {data_dir}/")
    print("  - Keychain entries for eufy-sync")
    print("  - Sync history database")
    if platform_support.agent_installed():
        print("  - Automatic sync")
    print("")

    answer = input("Are you sure? [y/N] ").strip()
    if not answer.lower().startswith("y"):
        print("Cancelled.")
        return False

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

    # Stop and remove the scheduled-sync agent, where the platform manages one.
    if platform_support.agent_installed():
        platform_support.purge_agent()

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

    from eufy_sync.cli.lock import LOCK_NAME
    from eufy_sync.credentials import (
        VAULT_LOCK_NAME,
        _keyring_available,
        delete_password,
        delete_token,
        vault_lock,
    )

    # One vault lock across clearing the vault and deleting credentials.json,
    # so no credential write can land in between.
    with vault_lock():
        # Clear the keychain vault. On a file-backend machine this gate skips the
        # deletes, which is safe only because credentials.json lives inside data_dir
        # and is erased by the rmtree below; keep them together if CRED_FILE ever
        # moves outside ~/.garmin-sync.
        if _keyring_available():
            # Best-effort: a locked keychain makes the vault read raise, and a
            # half-finished uninstall that leaves the data dir behind (the rmtree
            # is below) plus a raw traceback is worse than skipping this. The
            # rmtree still erases a file-backed vault under data_dir.
            try:
                for name in user_names:
                    # "strava" here is the API app's client secret, not an account
                    # password; it moved into the vault alongside the other two.
                    for suffix in ["eufy", "garmin", "strava", "zwift", "intervals"]:
                        delete_password(f"{name}:{suffix}")
                delete_token("eufy")
                delete_token("garmin")
                delete_token("strava")
                delete_token("zwift")
                delete_token("zwift_probe")
            except Exception:
                print("Note: could not clear keychain entries (the keychain may be locked).")

        # Remove data directory. A kept DB at a custom --db path lives outside
        # data_dir, so only the default location needs the selective sweep.
        # Both lock files are skipped: --uninstall holds them, and a lock file
        # deleted mid-sweep lets another process create and lock a fresh one
        # while credentials are still being removed. The vault lock file goes
        # below, after everything else; the caller handles the sync lock file
        # (see lock.unlink_while_held and _remove_lock_files).
        preserve_default_db = keep_db and db_path == default_db_path and db_path.exists()
        if data_dir.exists():
            keep = {LOCK_NAME, VAULT_LOCK_NAME}
            if preserve_default_db:
                keep.add("state.db")
            for item in data_dir.iterdir():
                if item.name in keep:
                    continue
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()

        # A custom --config/--db path lives outside data_dir, so it survives the
        # sweep above and must be removed explicitly.
        if config_path != default_config_path and config_path.exists():
            config_path.unlink()
        if db_path != default_db_path and not keep_db and db_path.exists():
            db_path.unlink()

        # Last step under the vault lock. On POSIX the lock file is deleted
        # while still held, so no other process can lock it between release
        # and delete. Windows refuses to delete an open file, so there
        # _remove_lock_files deletes it after release.
        if sys.platform != "win32":
            (data_dir / VAULT_LOCK_NAME).unlink(missing_ok=True)
        _remove_dir_if_empty(data_dir)

    print("")
    if keep_db:
        print(f"Removed all eufy-sync data (sync history kept in {db_path}).")
    else:
        print("Removed all eufy-sync data.")

    print(f"To remove the package itself, run: {install.uninstall_command()}")
    return True


def _remove_dir_if_empty(path: Path) -> None:
    try:
        path.rmdir()
    except OSError:
        pass


def _remove_lock_files(data_dir: Path) -> None:
    """Finish --uninstall once its locks are released.

    On Windows the lock files could not be deleted while open, so they go
    now; another process that has one open blocks the delete, which keeps it
    safe. On POSIX they were already deleted while still held, and deleting
    now could remove a lock file another process has just created and
    locked. Either way the data dir goes too when nothing else is left."""
    from eufy_sync.cli.lock import LOCK_NAME
    from eufy_sync.credentials import VAULT_LOCK_NAME
    if sys.platform == "win32":
        for name in (LOCK_NAME, VAULT_LOCK_NAME):
            try:
                (data_dir / name).unlink(missing_ok=True)
            except OSError:
                pass
    _remove_dir_if_empty(data_dir)
