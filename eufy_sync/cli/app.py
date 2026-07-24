"""The eufy-sync command line entry point and sync driver."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from eufy_sync import platform_support
from eufy_sync.cli import doctor, maintenance, profiles, setup, shared, status, updater

logger = logging.getLogger("eufy_sync")


def _target_label(total_counts: dict[str, int]) -> str:
    """Human label for the targets that received data, e.g. "Garmin and Strava"."""
    return " and ".join(n.capitalize() for n in total_counts if total_counts[n] > 0)


def _tally_run(user, counts: dict[str, int], errors: dict[str, str], total_counts: dict[str, int], failures: list) -> None:
    """Fold one sync_user result into the run totals."""
    for target_name, count in counts.items():
        total_counts[target_name] = total_counts.get(target_name, 0) + count
    for target_name, err in errors.items():
        failures.append((f"{user.name}/{target_name}", err))
    logger.info("User %s: synced %s", user.name, counts)


def main() -> None:
    try:
        _main()
    except KeyboardInterrupt:
        # Ctrl+C at any prompt (MFA code, passwords, confirmations) should
        # read as a cancel, not dump a traceback.
        print("\nCancelled.")
        sys.exit(130)


def _main() -> None:
    import argparse

    from eufy_sync import __version__

    parser = argparse.ArgumentParser(
        prog="eufy-sync",
        description="Sync Eufy smart scale data to Garmin Connect and Strava",
    )
    parser.add_argument("--version", "-V", action="version", version=f"eufy-sync {__version__}")
    parser.add_argument("--status", action="store_true", help="Show sync status and token health")
    parser.add_argument("--doctor", action="store_true", help="Check the whole setup and print fixes for anything wrong")
    parser.add_argument("--reauth", nargs="?", const="all", default=None, metavar="TARGET",
                        help="Re-authenticate (optionally: garmin or strava)")
    parser.add_argument("--setup-strava", action="store_true", help="Connect Strava to your account")
    parser.add_argument("--select-profile", action="store_true", help="Choose which Eufy profile to sync")
    parser.add_argument("--update-password", action="store_true", help="Change stored passwords")
    parser.add_argument("--update", action="store_true", help="Update eufy-sync to the latest version")
    parser.add_argument("--history", nargs="?", const=14, type=int, default=None, metavar="N",
                        help="Show recent sync history, last N entries (default: 14)")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync last N days")
    parser.add_argument("--dry-run", action="store_true", help="Preview without uploading")
    parser.add_argument("--headless", action="store_true", help="Never prompt; fail with a reauth message if login is needed (for scheduled runs)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed sync logs")
    parser.add_argument("--install-agent", action="store_true", help="Set up automatic sync every 4 hours")
    parser.add_argument("--uninstall-agent", action="store_true", help="Remove automatic sync")
    parser.add_argument("--uninstall", action="store_true", help="Remove all data, tokens, and automatic sync")
    parser.add_argument("--use-file-store", action="store_true", help="Store credentials in a 0o600 file instead of the keychain (no keychain prompts)")
    parser.add_argument("--use-keychain", action="store_true", help="Move credentials back into the system keychain")
    parser.add_argument("--config", type=Path, default=None, help="Config path (default: ~/.garmin-sync/config.yaml)")
    parser.add_argument("--db", type=Path, default=None, help="Database path (default: ~/.garmin-sync/state.db)")
    args = parser.parse_args()

    config_path = args.config or shared.DEFAULT_CONFIG
    db_path = args.db or shared.DEFAULT_DB

    # Configure logging before any command dispatch. Every path that can reach
    # a Garmin login (setup, --update-password, --reauth, sync) needs the
    # garminconnect logger quieted, or its per-strategy 429 warnings print
    # through logging's last-resort handler and look like errors.
    shared._configure_logging(args.verbose)

    # Handle doctor - must report even on a fresh install, never launch the
    # first-run wizard, and never traceback.
    if args.doctor:
        sys.exit(doctor._run_doctor(config_path, db_path))

    # Handle full uninstall
    if args.uninstall:
        maintenance._uninstall(shared.DATA_DIR, config_path=config_path, db_path=db_path)
        return

    # Handle credential store mode switches
    if args.use_file_store:
        from eufy_sync import credentials
        try:
            credentials.use_file_store()
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)
        print("Credentials moved to a 0o600 file (~/.garmin-sync/credentials.json). No more keychain prompts.")
        sys.exit(0)

    if args.use_keychain:
        from eufy_sync import credentials
        try:
            credentials.use_keychain_store()
        except RuntimeError as e:
            print(str(e))
            sys.exit(1)
        print("Credentials moved into the system keychain.")
        sys.exit(0)

    # Handle Launch Agent install/uninstall. Installing needs a config first:
    # a scheduled agent on an unconfigured machine would just fail (and
    # notify) every few hours.
    if args.install_agent:
        if not config_path.exists():
            print("No config found. Run eufy-sync first to set up.")
            sys.exit(1)
        maintenance._install_launch_agent()
        return

    if args.uninstall_agent:
        maintenance._uninstall_launch_agent()
        return

    # Handle self-update
    if args.update:
        updater._self_update()
        return

    # Handle Strava setup
    if args.setup_strava:
        setup._setup_strava(config_path)
        return

    # Handle profile selection
    if args.select_profile:
        profiles._select_profile(config_path)
        return

    # Handle password update
    if args.update_password:
        maintenance._update_password(config_path)
        return

    # Handle reauth
    if args.reauth is not None:
        target = None if args.reauth == "all" else args.reauth
        maintenance._reauth(config_path, force=True, target=target)
        return

    # --status/--history are read-only inspection commands - on a fresh
    # install they must refuse cleanly rather than dropping the user into
    # the interactive setup wizard (which prints "Running first sync ..."
    # that a --status/--history invocation never actually runs).
    if (args.status or args.history is not None) and not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    # First-run setup if no config exists
    first_run = not config_path.exists()
    if first_run and args.headless:
        msg = "No config found. Run eufy-sync in a terminal to set up."
        print(msg)
        platform_support.notify("eufy-sync", msg)
        sys.exit(1)

    try:
        if first_run:
            setup._first_run_setup(config_path)
        else:
            # Migrate existing plaintext passwords to keychain (one-time)
            setup._migrate_config_passwords(config_path)
            # One-time upgrade notice for users coming from eufy-garmin-sync
            setup._show_upgrade_notice()

        # Load config (passwords resolved from keychain or YAML fallback)
        from eufy_sync.config import AppConfig, load_config
        config = load_config(config_path)
    except SystemExit:
        raise
    except Exception as e:
        msg = f"eufy-sync could not start: {e}"
        print(msg)
        platform_support.notify("eufy-sync failed", str(e)[:200])
        sys.exit(1)

    has_garmin = any(u.garmin for u in config.users)

    # Handle status
    if args.status:
        from eufy_sync.state import SyncState
        try:
            state = SyncState(db_path)
        except Exception as e:
            print(f"Could not read sync state: {e}")
            sys.exit(1)
        status._show_status(state, config.users)
        state.close()
        return

    # Handle history
    if args.history is not None:
        from eufy_sync.state import SyncState
        try:
            state = SyncState(db_path)
        except Exception as e:
            print(f"Could not read sync state: {e}")
            sys.exit(1)
        status._show_history(state, config.users, limit=args.history)
        state.close()
        return

    # Run sync
    from eufy_sync.sync import sync_user
    from eufy_sync.state import SyncState
    from eufy_sync.eufy_client import AmbiguousProfileError

    updater._check_for_updates()

    backfill = args.backfill_days
    if first_run and backfill is None:
        backfill = 7

    try:
        state = SyncState(db_path)
    except Exception as e:
        msg = f"eufy-sync could not start: {e}"
        print(msg)
        platform_support.notify("eufy-sync failed", str(e)[:200])
        sys.exit(1)

    try:
        total_counts: dict[str, int] = {}
        failures = []
        for user in config.users:
            try:
                counts, errors = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                _tally_run(user, counts, errors, total_counts, failures)
            except AmbiguousProfileError as e:
                interactive = not args.headless and sys.stdin.isatty()
                if interactive:
                    # _prompt_profile_choice prints the list and asks for a pick.
                    customer_id = profiles._prompt_profile_choice(e.profiles)
                    profiles._save_customer_id(config_path, customer_id)
                    user.eufy.customer_id = customer_id
                    print("Saved. Syncing your profile now...")
                    try:
                        counts, errors = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                        _tally_run(user, counts, errors, total_counts, failures)
                    except Exception as retry_error:
                        logger.exception("Failed to sync user %s after profile selection", user.name)
                        failures.append((user.name, str(retry_error)))
                else:
                    print("")
                    print("Multiple profiles were found on this Eufy account:")
                    for i, p in enumerate(e.profiles, 1):
                        print(profiles._format_profile(p, i))
                    print("")
                    print("Nothing was synced. Choose your profile with: eufy-sync --select-profile")
                    failures.append((user.name, "multiple Eufy profiles; run eufy-sync --select-profile"))
            except Exception as e:
                logger.exception("Failed to sync user %s", user.name)
                failures.append((user.name, str(e)))

        total = sum(total_counts.values())

        if failures:
            from eufy_sync.cli import failure_notify
            reauth_needed = any("--reauth" in err for _, err in failures)
            eufy_password = any("changed your Eufy password" in err for _, err in failures)
            multiple_profiles = any("multiple Eufy profiles" in err for _, err in failures)
            all_transient = all(failure_notify.is_transient_network_error(err) for _, err in failures)
            if reauth_needed:
                platform_support.notify("eufy-sync: re-login needed", "Run: eufy-sync --reauth garmin", command="eufy-sync --reauth garmin")
                failure_notify.clear_network_failures()
            elif eufy_password:
                platform_support.notify("eufy-sync: Eufy login failed", "Run: eufy-sync --update-password", command="eufy-sync --update-password")
                failure_notify.clear_network_failures()
            elif multiple_profiles:
                platform_support.notify("eufy-sync: choose your profile", "Run: eufy-sync --select-profile", command="eufy-sync --select-profile")
                failure_notify.clear_network_failures()
            elif all_transient and args.headless and not args.dry_run:
                # A scheduled run that only hit network trouble. Stay quiet - the
                # next run retries - unless several have failed in a row, which
                # points at a real outage worth one heads-up.
                count, hours = failure_notify.record_network_failure()
                if failure_notify.should_escalate(count):
                    platform_support.notify(
                        "eufy-sync: network still down",
                        f"No network for ~{round(hours)}h ({count} runs). "
                        "Measurements are waiting and will sync when it is back.",
                    )
            else:
                fail_msg = "; ".join(f"{name}: {err[:80]}" for name, err in failures)
                platform_support.notify("eufy-sync failed", fail_msg)
                failure_notify.clear_network_failures()
            logger.error("Sync failed for: %s", "; ".join(f"{n}: {e[:80]}" for n, e in failures))
        elif not args.dry_run:
            # A clean run means the network is back; let a future outage
            # escalate from scratch.
            from eufy_sync.cli import failure_notify
            failure_notify.clear_network_failures()

        if args.dry_run:
            target_label = _target_label(total_counts)
            if total > 0:
                print(f"[DRY RUN] Would sync {total} measurement{'s' if total != 1 else ''} to {target_label}.")
            else:
                print("[DRY RUN] Would sync 0 measurements. Nothing new to sync.")
            sys.exit(1 if failures else 0)

        if total > 0:
            target_label = _target_label(total_counts)
            platform_support.notify("eufy-sync", f"Synced {total} measurement{'s' if total != 1 else ''} to {target_label}")

        if first_run:
            if failures:
                print("")
                print("First sync failed. Fix the issue above, then run eufy-sync again.")
            else:
                if total > 0:
                    target_label = _target_label(total_counts)
                    print("")
                    print(f"Synced {total} measurements to {target_label}.")
                maintenance._offer_launch_agent()
                print("")
                apps = []
                if has_garmin:
                    apps.append("Garmin Connect")
                if any(u.strava for u in config.users):
                    apps.append("Strava")
                print(f"You're all set! Check the {' and '.join(apps)} app to see your data.")
        elif not args.verbose:
            status._print_summary(total_counts, failures, state, config.users)

        sys.exit(1 if failures else 0)

    finally:
        state.close()


if __name__ == "__main__":
    main()
