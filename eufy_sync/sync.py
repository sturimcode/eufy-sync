from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict
from datetime import datetime, timezone

import httpx
from garminconnect import GarminConnectTooManyRequestsError

from eufy_sync import state as state_module
from eufy_sync.config import UserConfig
from eufy_sync.eufy_client import AmbiguousProfileError, EufyClient, EufyMeasurement
from eufy_sync.state import SyncState
from eufy_sync.transform import transform

logger = logging.getLogger("eufy_sync")

MAX_RETRIES = 3
RETRY_BASE_DELAY = 5  # seconds
# A different id can still be the processed version of a raw reading, but
# only a unique match close in both time and weight is safe to replace.
UPGRADE_MAX_SECONDS = 120
UPGRADE_MAX_WEIGHT_KG = 0.1


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


def sync_user(user: UserConfig, state: SyncState, backfill_days: int | None = None, headless: bool = False, dry_run: bool = False, repair_days: int | None = None) -> tuple[dict[str, int], dict[str, str]]:
    """Sync one user's Eufy data to configured targets.

    Returns (counts, errors): counts maps target name to the number of
    measurements synced; errors maps target name to a failure message for
    any target that failed to authenticate or whose upload gave up. Either
    failure drops that target from the rest of the run, so the other target
    keeps syncing. A target that uploaded some measurements before its upload
    failed appears in both dicts: the count is what actually landed.

    repair_days re-syncs the last N days even for measurements our state
    already calls synced (issue #58): the target can have lost data we
    recorded as delivered, and only the target knows that.
    """
    repair = repair_days is not None
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

        # Determine how far back to fetch: from the OLDEST per-target cursor,
        # not a shared one. If one target was down (auth failing) while
        # another kept syncing, a shared cursor would advance past the outage
        # window and the recovered target would silently never receive those
        # measurements. Re-fetched ones are cheap: the up-to-date target
        # skips them via is_synced. A target with no syncs yet (first run or
        # newly added) backfills 7 days.
        if repair:
            after_timestamp = int(time.time()) - (repair_days * 86400)
            logger.info("Repair: re-syncing the last %d days regardless of recorded state", repair_days)
        elif backfill_days:
            after_timestamp = int(time.time()) - (backfill_days * 86400)
        else:
            default_cursor = int(time.time()) - (7 * 86400)
            cursors = []
            for name, _ in targets:
                ts = state.get_latest_sync_timestamp(user.name, name)
                if ts is None:
                    logger.info("No prior syncs to %s for %s, backfilling 7 days", name, user.name)
                if name == "garmin":
                    pending_ts = state.get_oldest_weight_only_timestamp(user.name, name)
                    if pending_ts is not None:
                        # Processed data can arrive after newer weigh-ins have
                        # advanced the cursor. Include small timestamp shifts.
                        ts = min(ts, pending_ts - UPGRADE_MAX_SECONDS) if ts is not None else pending_ts - UPGRADE_MAX_SECONDS
                cursors.append(ts if ts is not None else default_cursor)
            after_timestamp = min(cursors)

        pending = {}
        pending_previous_ids = {}
        if any(name == "garmin" for name, _ in targets):
            for row in state.get_pending_upgrades(user.name):
                payload = row["measurement"]
                payload["timestamp"] = datetime.fromisoformat(payload["timestamp"])
                m = EufyMeasurement(**payload)
                if user.eufy.customer_id and m.customer_id != user.eufy.customer_id:
                    continue
                pending[m.measurement_id] = m
                pending_previous_ids[m.measurement_id] = row["previous_id"]

        try:
            eufy.authenticate()
            fetched = _retry(
                lambda: eufy.fetch_measurements(after_timestamp=after_timestamp),
                "Eufy fetch",
            )
        except AmbiguousProfileError:
            raise
        except Exception as e:
            if not pending:
                raise
            logger.warning("Eufy unavailable; recovering saved Garmin replacements: %s", e)
            errors["eufy"] = str(e)
            fetched = []
        # Fresh processed data wins over the saved copy. A raw record with
        # the same id must never replace the full data we need to recover.
        measurements_by_id = dict(pending)
        for m in fetched:
            if m.measurement_id not in pending or not m.weight_only:
                measurements_by_id[m.measurement_id] = m
        measurements = list(measurements_by_id.values())
        # Garmin receives history in order; Strava takes the newest valid
        # measurement from this sorted batch.
        measurements.sort(key=lambda m: m.timestamp)
        logger.info("Found %d measurements for %s", len(measurements), user.name)

        # Strava stores one current value, so older values in the same batch
        # would just consume requests before being immediately overwritten.
        fresh_ids = {m.measurement_id for m in fetched}
        valid = [m for m in measurements if m.measurement_id in fresh_ids and transform(m) is not None]
        strava_latest_id = valid[-1].measurement_id if valid else None

        # Backfill can return older, unsynced readings alongside a newest
        # reading that dedup will skip, or omit the newest reading entirely.
        # Never move Strava's current weight behind its recorded progress.
        strava_latest_timestamp = (
            state.get_latest_sync_timestamp(user.name, "strava")
            if any(name == "strava" for name, _ in targets) else None
        )

        counts = {name: 0 for name, _ in targets}
        for m in measurements:
            body_comp = transform(m)
            if body_comp is None:
                logger.warning("Skipping invalid measurement: %s (%.1f kg)", m.measurement_id, m.weight_kg)
                continue

            # Snapshot: a failing upload rebuilds `targets` mid-loop.
            for target_name, client in list(targets):
                if (
                    target_name == "strava"
                    and strava_latest_timestamp is not None
                    and m.timestamp.timestamp() < strava_latest_timestamp
                ):
                    logger.debug("Skipping Strava history older than its current weight: %s", m.measurement_id)
                    continue
                if target_name == "strava" and m.measurement_id != strava_latest_id:
                    continue

                # Still consulted in repair mode: it decides whether the sync
                # is recorded below, since re-uploading a known id must not
                # insert a second row (UNIQUE on user/measurement/target).
                synced_already = state.is_synced(user.name, m.measurement_id, target_name)
                previous_id = pending_previous_ids.get(m.measurement_id)
                if target_name == "garmin" and synced_already and previous_id and previous_id != m.measurement_id:
                    # The new row proves the replacement uploaded before a
                    # crash interrupted local bookkeeping. Finish locally.
                    if not dry_run:
                        state.mark_upgraded(user.name, previous_id, "garmin")
                        state.clear_pending_upgrade(user.name, m.measurement_id)
                    continue

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
                    if m.measurement_id in pending_previous_ids:
                        candidates = [r for r in candidates if r["measurement_id"] == pending_previous_ids[m.measurement_id]]
                    if synced_already:
                        upgrade_row = next(
                            (r for r in candidates if r["measurement_id"] == m.measurement_id), None
                        )
                        if upgrade_row is None and not repair:
                            if m.measurement_id in pending and not dry_run:
                                state.clear_pending_upgrade(user.name, m.measurement_id)
                            logger.debug("Already synced to %s: %s", target_name, m.measurement_id)
                            continue
                    elif candidates:
                        matches = [
                            r for r in candidates
                            if abs((datetime.fromisoformat(r["measurement_timestamp"]) - m.timestamp).total_seconds())
                            <= UPGRADE_MAX_SECONDS
                            and abs(r["weight_kg"] - m.weight_kg) <= UPGRADE_MAX_WEIGHT_KG
                        ]
                        if len(matches) == 1:
                            upgrade_row = matches[0]
                elif synced_already and not repair:
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
                    if not synced_already:
                        state.record_sync(
                            user_name=user.name,
                            measurement_id=m.measurement_id,
                            measurement_timestamp=m.timestamp.isoformat(),
                            weight_kg=m.weight_kg,
                            synced_at=datetime.now(timezone.utc).isoformat(),
                            target="garmin",
                            response=state_module.SKIPPED_IN_GARMIN_RESPONSE,
                        )
                    continue

                # An upload that _retry gives up on (permanent, or retries
                # exhausted) kills this target only. A dead Garmin session
                # used to abort sync_user and take Strava down with it, even
                # though Strava was fine. Same containment the auth loop above
                # already has: record the message, drop the target, keep going.
                try:
                    if target_name == "garmin":
                        if upgrade_row is not None:
                            payload = asdict(m)
                            payload["timestamp"] = m.timestamp.isoformat()
                            state.save_pending_upgrade(user.name, upgrade_row["measurement_id"], payload)
                            # Fail-open (returns False, never raises): if the old
                            # entry can't be removed, upload anyway - the worst
                            # case is the duplicate this fix exists to prevent,
                            # while the body comp still arrives.
                            client.delete_weight_entry(
                                datetime.fromisoformat(upgrade_row["measurement_timestamp"]),
                                upgrade_row["weight_kg"],
                            )
                        # _retry calls the lambda before this iteration ends,
                        # so the loop variables it closes over are the right ones.
                        result = _retry(
                            lambda: client.upload_body_composition(body_comp),  # noqa: B023
                            f"Garmin upload ({m.measurement_id})",
                        )
                    else:
                        result = _retry(
                            lambda: client.update_weight(m.weight_kg),  # noqa: B023
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
                    if target_name == "garmin" and (upgrade_row is not None or m.measurement_id in pending):
                        state.clear_pending_upgrade(user.name, m.measurement_id)
                except Exception as e:
                    logger.error("Upload to %s failed for %s: %s", target_name, user.name, e)
                    # str(e) carries the actionable text the CLI keys its
                    # notification off (e.g. the "--reauth" hint), so it must
                    # reach the caller unwrapped.
                    errors[target_name] = str(e)
                    targets = [t for t in targets if t[0] != target_name]
                    continue

                counts[target_name] += 1
                lb = m.weight_kg * 2.20462
                detail = "full body comp" if target_name == "garmin" else "weight only"
                logger.info("Synced %.2f kg (%.1f lb) → %s (%s)", m.weight_kg, lb, target_name.capitalize(), detail)

                # Small delay between uploads to avoid rate limiting
                time.sleep(1 if target_name == "garmin" else 0.5)

            if not targets:
                # Every target dropped out; the remaining measurements have
                # nowhere to go.
                break

        return counts, errors

    finally:
        eufy.close()
        for _, client in all_targets:
            client.close()
