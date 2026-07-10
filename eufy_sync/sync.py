from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

import httpx

from garminconnect import GarminConnectTooManyRequestsError

from eufy_sync.config import UserConfig
from eufy_sync.eufy_client import AmbiguousProfileError, EufyClient
from eufy_sync.state import SyncState
from eufy_sync.transform import transform

logger = logging.getLogger("eufy_sync")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds


class PermanentSyncError(RuntimeError):
    """Raised for failures that retries can't fix (bad password, revoked token)."""


def _is_permanent(exc: BaseException) -> bool:
    # GarminConnectTooManyRequestsError: a 429 on login/refresh won't clear by
    # retrying seconds later, and retrying makes the IP rate limit worse, so
    # fail fast and let the next scheduled run try on a cooled-down limit.
    if isinstance(exc, (PermanentSyncError, AmbiguousProfileError, GarminConnectTooManyRequestsError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # httpx 4xx is a client error and won't recover; a 429 (rate-limited)
        # stays retryable here. The Garmin login 429 is handled above.
        return 400 <= status < 500 and status != 429
    return False


def _retry(fn, description: str):
    """Call fn() with exponential backoff. Returns the result or raises."""
    for attempt in range(MAX_RETRIES):
        try:
            return fn()
        except Exception as e:
            if _is_permanent(e):
                raise
            if attempt == MAX_RETRIES - 1:
                raise
            delay = RETRY_BASE_DELAY * (2 ** attempt)
            logger.warning("%s failed (attempt %d/%d): %s. Retrying in %ds...",
                           description, attempt + 1, MAX_RETRIES, e, delay)
            time.sleep(delay)


def sync_user(user: UserConfig, state: SyncState, backfill_days: int | None = None, headless: bool = False, dry_run: bool = False) -> tuple[dict[str, int], dict[str, str]]:
    """Sync one user's Eufy data to configured targets.

    Returns (counts, errors): counts maps target name to the number of
    measurements synced; errors maps target name to a failure message for
    any target whose authenticate() call failed. The two dicts are disjoint
    over target names - a target with an error was dropped from the run and
    has no count.
    """
    eufy = EufyClient(user.eufy)

    all_targets: list[tuple[str, object]] = []
    if user.garmin:
        from eufy_sync.garmin_client import GarminClient
        all_targets.append(("garmin", GarminClient(user.garmin)))
    if user.strava:
        from eufy_sync.strava_client import StravaClient
        all_targets.append(("strava", StravaClient(user.strava)))

    targets: list[tuple[str, object]] = []

    try:
        logger.info("Syncing user: %s", user.name)
        eufy.authenticate()

        errors: dict[str, str] = {}
        first_exception: BaseException | None = None
        for target_name, client in all_targets:
            try:
                if target_name == "garmin":
                    client.authenticate(allow_interactive=not headless)
                else:
                    client.authenticate()
                targets.append((target_name, client))
            except Exception as e:
                logger.error("Authentication failed for %s/%s: %s", user.name, target_name, e)
                errors[target_name] = str(e)
                if first_exception is None:
                    first_exception = e

        if not targets:
            # Every target failed auth - whole-user failure. Re-raise the
            # first exception as-is (not wrapped) so callers that check the
            # original type (AmbiguousProfileError, PermanentSyncError,
            # GarminConnectTooManyRequestsError) still work.
            raise first_exception

        # Determine how far back to fetch
        after_timestamp: int | None = None
        if backfill_days:
            after_timestamp = int(time.time()) - (backfill_days * 86400)
        else:
            # Check if any target is newly added (no syncs yet)
            new_target = any(
                not state.has_any_syncs(user.name, name) for name, _ in targets
            )
            if new_target:
                # New target added - backfill 7 days so existing measurements sync
                after_timestamp = int(time.time()) - (7 * 86400)
                logger.info("New sync target detected for %s, backfilling 7 days", user.name)
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
        # Strava only stores a single current weight; iterating oldest→newest
        # ensures the final PUT leaves the newest value on the profile.
        measurements.sort(key=lambda m: m.timestamp)
        logger.info("Found %d measurements for %s", len(measurements), user.name)

        counts = {name: 0 for name, _ in targets}
        for m in measurements:
            body_comp = transform(m)
            if body_comp is None:
                logger.warning("Skipping invalid measurement: %s (%.1f kg)", m.measurement_id, m.weight_kg)
                continue

            for target_name, client in targets:
                synced_already = state.is_synced(user.name, m.measurement_id, target_name)

                # Issue #48: a full record can arrive for a weigh-in we
                # already synced weight-only from the raw Wi-Fi endpoint.
                # Depending on how Eufy timestamps the two, the ids may match
                # (full record would be skipped, stranding the user on
                # weight-only) or differ (full record would duplicate the
                # day). Either way, replace the weight-only Garmin entry.
                upgrade_row = None
                if target_name == "garmin" and not m.weight_only:
                    local_date = m.timestamp.astimezone().date()
                    candidates = state.weight_only_syncs_on_date(user.name, "garmin", local_date)
                    if synced_already:
                        upgrade_row = next(
                            (r for r in candidates if r["measurement_id"] == m.measurement_id), None
                        )
                        if upgrade_row is None:
                            logger.debug("Already synced to %s: %s", target_name, m.measurement_id)
                            continue
                    elif candidates:
                        upgrade_row = min(
                            candidates,
                            key=lambda r: abs(datetime.fromisoformat(r["measurement_timestamp"]) - m.timestamp),
                        )
                elif synced_already:
                    logger.debug("Already synced to %s: %s", target_name, m.measurement_id)
                    continue

                if dry_run:
                    print(f"[DRY RUN] Would sync to {target_name}: {m.weight_kg:.1f} kg at {m.timestamp}")
                    counts[target_name] += 1
                    continue

                # Garmin-specific: check for existing entry on this date, but
                # only when WE have not already synced something for that
                # local date ourselves. Otherwise this guard cannot tell "our
                # own earlier upload today" from "another source has this
                # date", and a corrected same-day re-weigh would be
                # permanently skipped. Garmin accepts multiple same-day
                # entries and de-dupes by timestamp, so it is safe to upload
                # again here.
                if (
                    target_name == "garmin"
                    and not state.has_synced_on_date(user.name, "garmin", m.timestamp.astimezone().date())
                    and client.has_weight_on_date(m.timestamp)
                ):
                    logger.debug("Garmin already has data for %s, skipping", m.timestamp.date())
                    state.record_sync(
                        user_name=user.name,
                        measurement_id=m.measurement_id,
                        measurement_timestamp=m.timestamp.isoformat(),
                        weight_kg=m.weight_kg,
                        synced_at=datetime.now(timezone.utc).isoformat(),
                        target="garmin",
                        response='{"skipped": "already_in_garmin"}',
                    )
                    continue

                if target_name == "garmin":
                    if upgrade_row is not None:
                        # Fail-open (returns False, never raises): if the old
                        # entry can't be removed, upload anyway - the worst
                        # case is the duplicate this fix exists to prevent,
                        # while the body comp still arrives.
                        client.delete_weight_entry(
                            datetime.fromisoformat(upgrade_row["measurement_timestamp"]),
                            upgrade_row["weight_kg"],
                        )
                    result = _retry(
                        lambda: client.upload_body_composition(body_comp),
                        f"Garmin upload ({m.measurement_id})",
                    )
                    response_str = json.dumps(result) if result else None
                else:
                    result = _retry(
                        lambda: client.update_weight(m.weight_kg),
                        f"Strava upload ({m.measurement_id})",
                    )
                    response_str = json.dumps(result) if result else None

                if not synced_already:
                    state.record_sync(
                        user_name=user.name,
                        measurement_id=m.measurement_id,
                        measurement_timestamp=m.timestamp.isoformat(),
                        weight_kg=m.weight_kg,
                        synced_at=datetime.now(timezone.utc).isoformat(),
                        target=target_name,
                        response=response_str,
                        weight_only=m.weight_only,
                    )
                if upgrade_row is not None:
                    state.mark_upgraded(user.name, upgrade_row["measurement_id"], "garmin")
                    logger.info("Upgraded weight-only entry to full body comp for %s", m.timestamp.date())
                counts[target_name] += 1
                lb = m.weight_kg * 2.20462
                detail = "full body comp" if target_name == "garmin" else "weight only"
                logger.info("Synced %.2f kg (%.1f lb) → %s (%s)", m.weight_kg, lb, target_name.capitalize(), detail)

                # Small delay between uploads to avoid rate limiting
                time.sleep(1 if target_name == "garmin" else 0.5)

        return counts, errors

    finally:
        eufy.close()
        for _, client in all_targets:
            client.close()
