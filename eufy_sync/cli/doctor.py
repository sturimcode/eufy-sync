"""eufy-sync --doctor: one command that diagnoses the whole setup and prints
the exact fix for anything wrong.

Every check is wrapped so nothing here can raise past _run_doctor - a
diagnostic tool that crashes is worse than useless. Checks 4-10 run even
when earlier ones warn; checks 2-10 are skipped (with a single explanatory
line) when check 1 (config) fails, since nothing else can load without it.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

from eufy_sync import credentials
from eufy_sync import platform_support
from eufy_sync.cli import updater
from eufy_sync.config import load_config
from eufy_sync.credentials import _keyring_available, active_store_label
from eufy_sync.eufy_client import EufyClient
from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.state import SyncState
from eufy_sync.strava_client import StravaClient

_LABEL_WIDTH = 14


def _line(status: str, label: str, detail: str, fix: str | None = None) -> str:
    text = f"{status:<5} {label:<{_LABEL_WIDTH}} {detail}"
    if fix:
        text += f"   fix: {fix}"
    return text


def _run_doctor(config_path: Path, db_path: Path) -> int:
    lines: list[str] = []
    fail_count = 0
    warn_count = 0

    def report(status: str, label: str, detail: str, fix: str | None = None) -> None:
        nonlocal fail_count, warn_count
        lines.append(_line(status, label, detail, fix))
        if status == "FAIL":
            fail_count += 1
        elif status == "WARN":
            warn_count += 1

    # 1. config
    if not config_path.exists():
        report("FAIL", "config", "no config", "eufy-sync")
        lines.append("skip  remaining checks - no config to load")
        print("\n".join(lines))
        print("")
        print(f"{fail_count} problem(s) found.")
        return 1

    try:
        config = load_config(config_path)
    except Exception as e:
        report("FAIL", "config", str(e))
        lines.append("skip  remaining checks - config failed to load")
        print("\n".join(lines))
        print("")
        print(f"{fail_count} problem(s) found.")
        return 1

    targets = []
    user = config.users[0]
    if user.garmin:
        targets.append("garmin")
    if user.strava:
        targets.append("strava")
    report("PASS", "config", f"valid ({len(config.users)} user, targets: {', '.join(targets)})")

    # 2. profile
    _check_profile(report, user)

    # 3. keychain
    _check_keychain(report)

    # 4. eufy token
    eufy_client = _check_eufy_token(report, user)

    # 5. garmin session
    if user.garmin:
        _check_garmin_session(report, user)

    # 6. strava token
    if user.strava:
        _check_strava_token(report, user)

    # 7. eufy cloud
    _check_eufy_cloud(report, eufy_client)

    # 8. scheduled sync agent (where the platform manages one)
    agent = platform_support.agent_status()
    if agent is not None:
        report(agent["status"], agent["label"], agent["detail"], agent["fix"])

    # 9. state db
    _check_state_db(report, db_path, user)

    # 10. version
    _check_version(report)

    print("\n".join(lines))
    print("")
    if fail_count == 0 and warn_count == 0:
        print("All checks passed.")
        return 0
    if fail_count == 0:
        # Warnings alone exit 0; saying "problems found" here would contradict
        # the exit code and read as a failure.
        print(f"{warn_count} warning(s), nothing blocking.")
        return 0
    print(f"{fail_count} problem(s) found.")
    return 1


def _check_profile(report, user) -> None:
    try:
        if user.eufy.customer_id:
            short = user.eufy.customer_id[-4:]
            report("PASS", "profile", f"selected (...{short})")
        else:
            report(
                "WARN", "profile",
                "not set (fine for single-profile accounts)",
                "eufy-sync --select-profile",
            )
    except Exception as e:
        report("FAIL", "profile", str(e))


def _check_keychain(report) -> None:
    try:
        # A credentials file next to an active keychain is a stray unmarked
        # file being ignored: it holds a stale copy of secrets that nothing
        # reads or updates, so surface it instead of staying silent.
        if credentials._active_backend() == "keychain" and credentials.CRED_FILE.exists():
            report(
                "WARN", "keychain",
                "keychain active; unused credentials file at ~/.garmin-sync/credentials.json",
                "eufy-sync --use-file-store (adopt it) or delete the file",
            )
            return
        report("PASS", "keychain", active_store_label())
    except Exception as e:
        report("PASS", "keychain", f"file store (no keychain prompts) ({e})")


def _check_eufy_token(report, user):
    eufy_client = EufyClient(user.eufy)
    try:
        status = eufy_client.token_status()
        state = status.get("state")
        if state == "valid":
            report("PASS", "eufy token", f"valid, {status['days_remaining']}d remaining")
        elif state == "expired":
            report("WARN", "eufy token", "expired (next sync re-logs-in with the stored password)")
        else:
            report("WARN", "eufy token", "no token (next sync re-logs-in with the stored password)")
    except Exception as e:
        report("FAIL", "eufy token", str(e))
    return eufy_client


def _check_garmin_session(report, user) -> None:
    try:
        auth = GarminAuth(user.garmin.email, user.garmin.password)
        status = auth.token_status()
        if status.get("state") == "valid":
            report("PASS", "garmin session", "valid")
        else:
            report("FAIL", "garmin session", "expired", "eufy-sync --reauth garmin")
    except Exception as e:
        report("FAIL", "garmin session", str(e), "eufy-sync --reauth garmin")


def _check_strava_token(report, user) -> None:
    try:
        client = StravaClient(user.strava)
        status = client.token_status()
        state = status.get("state")
        if state == "valid":
            report("PASS", "strava token", "valid")
        elif state == "refresh_needed":
            report("PASS", "strava token", "valid (refresh pending on next sync)")
        else:
            report("FAIL", "strava token", state or "expired", "eufy-sync --reauth strava")
    except Exception as e:
        report("FAIL", "strava token", str(e), "eufy-sync --reauth strava")


def _check_eufy_cloud(report, eufy_client) -> None:
    if eufy_client is None:
        return
    try:
        eufy_client.authenticate()
        window_start = int(time.time()) - 30 * 86400
        measurements = eufy_client.fetch_measurements(after_timestamp=window_start)
        if not measurements:
            report("WARN", "eufy cloud", "no weigh-ins in the last 30 days")
            return
        newest = max(m.timestamp for m in measurements)
        now = datetime.now(timezone.utc)
        ts = newest if newest.tzinfo else newest.replace(tzinfo=timezone.utc)
        age = now - ts
        days = age.days
        hours = int(age.total_seconds() / 3600)
        if days >= 2:
            report(
                "WARN", "eufy cloud",
                f"last weigh-in {days}d ago; if you weighed since, open the Eufy app",
            )
        elif hours >= 1:
            report("PASS", "eufy cloud", f"last weigh-in {hours}h ago")
        else:
            report("PASS", "eufy cloud", "last weigh-in just now")
    except Exception as e:
        msg = str(e)
        fix = None
        if "--update-password" in msg:
            fix = "eufy-sync --update-password"
        report("FAIL", "eufy cloud", msg, fix)
    finally:
        try:
            eufy_client.close()
        except Exception:
            pass


def _check_state_db(report, db_path: Path, user) -> None:
    try:
        state = SyncState(db_path)
    except Exception as e:
        report("FAIL", "state db", str(e))
        return

    try:
        ts = state.get_latest_sync_timestamp(user.name)
        if ts is None:
            report("PASS", "state db", "never synced")
        else:
            last_sync = datetime.fromtimestamp(ts, tz=timezone.utc)
            ago = datetime.now(timezone.utc) - last_sync
            days = ago.days
            hours = int(ago.total_seconds() / 3600)
            if days > 0:
                report("PASS", "state db", f"last sync {days}d ago")
            else:
                report("PASS", "state db", f"last sync {hours}h ago")
    except Exception as e:
        report("FAIL", "state db", str(e))
    finally:
        try:
            state.close()
        except Exception:
            pass


def _check_version(report) -> None:
    try:
        from eufy_sync import __version__
        latest = updater._latest_pypi_version()
        if latest is None:
            report("WARN", "version", "could not check")
            return
        if latest == __version__:
            report("PASS", "version", f"{__version__} (up to date)")
            return
        report(
            "WARN", "version",
            f"{__version__} installed, {latest} available",
            "eufy-sync --update",
        )
    except Exception as e:
        report("WARN", "version", f"could not check ({e})")
