"""Sync status, history, and the end-of-run summary line."""
from __future__ import annotations

from datetime import datetime, timezone


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
                print("Garmin auth: not connected - run: eufy-sync --reauth garmin")

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
