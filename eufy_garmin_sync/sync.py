from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from eufy_garmin_sync.config import UserConfig, load_config
from eufy_garmin_sync.eufy_client import EufyClient
from eufy_garmin_sync.garmin_auth import GarminAuth
from eufy_garmin_sync.garmin_client import GarminClient
from eufy_garmin_sync.state import SyncState
from eufy_garmin_sync.transform import transform

logger = logging.getLogger("eufy_garmin_sync")

UPDATE_CHECK_INTERVAL = 604800  # check once per week


def _check_for_updates() -> None:
    """Print a message if a newer version is available on GitHub."""
    try:
        cache_file = Path.home() / ".garmin-sync" / "update_check"
        now = time.time()

        # Only check once per day
        if cache_file.exists():
            last_check = float(cache_file.read_text().strip())
            if now - last_check < UPDATE_CHECK_INTERVAL:
                return

        # Fetch latest remote commit
        project_dir = Path(__file__).parent.parent
        result = subprocess.run(
            ["git", "-C", str(project_dir), "fetch", "origin", "main", "--quiet"],
            capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            return

        local = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        remote = subprocess.run(
            ["git", "-C", str(project_dir), "rev-parse", "origin/main"],
            capture_output=True, text=True, timeout=5,
        ).stdout.strip()

        # Save check timestamp
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(str(now))

        if local != remote:
            if sys.stdin.isatty():
                answer = input("Update available. Install now? [y/N] ").strip()
                if answer.lower() == "y":
                    project_dir = Path(__file__).parent.parent
                    subprocess.run(
                        [str(project_dir / "setup.sh"), "--update"],
                        cwd=str(project_dir),
                    )
            else:
                _notify("eufy-garmin-sync", "Update available. Run: ./setup.sh --update")

    except Exception:
        pass  # never let update check break a sync


MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds


def _retry(fn, description: str):
    """Call fn() with exponential backoff. Returns the result or raises."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("%s failed (attempt %d/%d): %s. Retrying in %ds...",
                           description, attempt + 1, MAX_RETRIES, e, delay)
            time.sleep(delay)


def _notify(title: str, message: str) -> None:
    """Send a macOS notification. Fails silently on other platforms."""
    try:
        # Sanitize to prevent AppleScript injection from error messages
        import json as _json
        safe_title = _json.dumps(title)
        safe_msg = _json.dumps(message)
        subprocess.run(
            ["osascript", "-e", f'display notification {safe_msg} with title {safe_title}'],
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def sync_user(user: UserConfig, state: SyncState, backfill_days: int | None = None, headless: bool = False, dry_run: bool = False) -> int:
    """Sync one user's Eufy data to Garmin. Returns count of measurements synced."""
    eufy = EufyClient(user.eufy)
    garmin = GarminClient(user.garmin)

    try:
        logger.info("Syncing user: %s", user.name)
        eufy.authenticate()
        garmin.authenticate(allow_browser=not headless)

        # Determine how far back to fetch
        after_timestamp: int | None = None
        if backfill_days:
            after_timestamp = int(time.time()) - (backfill_days * 86400)
        else:
            after_timestamp = state.get_latest_sync_timestamp(user.name)
            if after_timestamp is None:
                # First run - default to last 7 days
                after_timestamp = int(time.time()) - (7 * 86400)
                logger.info("First run for %s, defaulting to 7-day backfill", user.name)

        measurements = _retry(
            lambda: eufy.fetch_measurements(after_timestamp=after_timestamp),
            "Eufy fetch",
        )
        logger.info("Found %d measurements for %s", len(measurements), user.name)

        synced_count = 0
        for m in measurements:
            if state.is_synced(user.name, m.measurement_id):
                logger.debug("Already synced: %s", m.measurement_id)
                continue

            body_comp = transform(m)
            if body_comp is None:
                logger.warning("Skipping invalid measurement: %s (%.1f kg)", m.measurement_id, m.weight_kg)
                continue

            if dry_run:
                logger.info("[DRY RUN] Would sync: %.1f kg at %s", m.weight_kg, m.timestamp)
                synced_count += 1
                continue

            # Check Garmin for existing entry on this date (prevents duplicates across machines)
            if garmin.has_weight_on_date(m.timestamp):
                logger.debug("Garmin already has data for %s, skipping", m.timestamp.date())
                state.record_sync(
                    user_name=user.name,
                    measurement_id=m.measurement_id,
                    measurement_timestamp=m.timestamp.isoformat(),
                    weight_kg=m.weight_kg,
                    synced_at=datetime.now(timezone.utc).isoformat(),
                    garmin_response='{"skipped": "already_in_garmin"}',
                )
                continue

            result = _retry(
                lambda: garmin.upload_body_composition(body_comp),
                f"Garmin upload ({m.measurement_id})",
            )

            state.record_sync(
                user_name=user.name,
                measurement_id=m.measurement_id,
                measurement_timestamp=m.timestamp.isoformat(),
                weight_kg=m.weight_kg,
                synced_at=datetime.now(timezone.utc).isoformat(),
                garmin_response=json.dumps(result) if result else None,
            )
            synced_count += 1
            logger.info("Synced measurement %s: %.1f kg", m.measurement_id, m.weight_kg)

            # Small delay between uploads to avoid Garmin rate limiting
            time.sleep(1)

        return synced_count

    finally:
        eufy.close()
        garmin.close()


def show_status(state: SyncState, config_users: list) -> None:
    """Print sync status for all users."""
    for user in config_users:
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

        # Garmin token health
        auth = GarminAuth(user.garmin.email, user.garmin.password)
        session = auth._load_session()
        if session:
            di = session.di_token
            if di.refresh_is_expired:
                print("Garmin auth: EXPIRED - browser re-login needed")
            elif di.is_expired:
                print("Garmin auth: access token expired, will refresh on next sync")
            else:
                days_left = int((di.refresh_expires_at - time.time()) / 86400)
                print(f"Garmin auth: valid ({days_left} days until re-login needed)")
        else:
            print("Garmin auth: no saved session - first run will open browser")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Eufy scale data to Garmin Connect")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to config file")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync measurements from the last N days")
    parser.add_argument("--db", type=Path, default=Path("state.db"), help="Path to state database")
    parser.add_argument("--status", action="store_true", help="Show sync status and token health")
    parser.add_argument("--reauth", action="store_true", help="Force Garmin browser re-login")
    parser.add_argument("--headless", action="store_true", help="Disallow browser popups (used by Launch Agent)")
    parser.add_argument("--dry-run", action="store_true", help="Preview what would sync without uploading")
    args = parser.parse_args()

    config = load_config(args.config)

    if args.status:
        state = SyncState(args.db)
        show_status(state, config.users)
        state.close()
        return

    if args.reauth:
        for user in config.users:
            print(f"Re-authenticating Garmin for {user.name}...")
            auth = GarminAuth(user.garmin.email, user.garmin.password)
            auth.force_reauth()
            print("Done - tokens saved.")
        return

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _check_for_updates()

    state = SyncState(args.db)

    try:
        total = 0
        failures = []
        for user in config.users:
            try:
                count = sync_user(user, state, backfill_days=args.backfill_days, headless=args.headless, dry_run=args.dry_run)
                total += count
                logger.info("User %s: synced %d measurements", user.name, count)
            except Exception as e:
                logger.exception("Failed to sync user %s", user.name)
                failures.append((user.name, str(e)))

        if failures:
            # Check failure type and send appropriate notification
            reauth_needed = any("re-authenticate" in err for _, err in failures)
            eufy_password = any("changed your Eufy password" in err for _, err in failures)
            if reauth_needed:
                _notify(
                    "eufy-sync: re-login needed",
                    "Garmin session expired. Run: eufy-sync --reauth",
                )
            elif eufy_password:
                _notify(
                    "eufy-sync: Eufy login failed",
                    "Password may have changed. Run: eufy-sync --update-password",
                )
            else:
                fail_msg = "; ".join(f"{name}: {err[:80]}" for name, err in failures)
                _notify("eufy-garmin-sync failed", fail_msg)
            logger.error("Sync failed for: %s", "; ".join(f"{n}: {e[:80]}" for n, e in failures))

        if total > 0:
            _notify("eufy-garmin-sync", f"Synced {total} measurement{'s' if total != 1 else ''} to Garmin")

        logger.info("Sync complete. Total measurements synced: %d", total)
        sys.exit(1 if failures else 0)

    finally:
        state.close()


if __name__ == "__main__":
    main()
