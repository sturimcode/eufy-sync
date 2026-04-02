from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone

from eufy_sync.config import UserConfig
from eufy_sync.eufy_client import EufyClient
from eufy_sync.state import SyncState
from eufy_sync.transform import transform

logger = logging.getLogger("eufy_sync")

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


def sync_user(user: UserConfig, state: SyncState, backfill_days: int | None = None, headless: bool = False, dry_run: bool = False) -> dict[str, int]:
    """Sync one user's Eufy data to configured targets. Returns count per target."""
    eufy = EufyClient(user.eufy)

    targets: list[tuple[str, object]] = []
    if user.garmin:
        from eufy_sync.garmin_client import GarminClient
        targets.append(("garmin", GarminClient(user.garmin)))
    if user.strava:
        from eufy_sync.strava_client import StravaClient
        targets.append(("strava", StravaClient(user.strava)))

    try:
        logger.info("Syncing user: %s", user.name)
        eufy.authenticate()
        for target_name, client in targets:
            if target_name == "garmin":
                client.authenticate(allow_browser=not headless)
            else:
                client.authenticate()

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
        logger.info("Found %d measurements for %s", len(measurements), user.name)

        counts = {name: 0 for name, _ in targets}
        for m in measurements:
            body_comp = transform(m)
            if body_comp is None:
                logger.warning("Skipping invalid measurement: %s (%.1f kg)", m.measurement_id, m.weight_kg)
                continue

            for target_name, client in targets:
                if state.is_synced(user.name, m.measurement_id, target_name):
                    logger.debug("Already synced to %s: %s", target_name, m.measurement_id)
                    continue

                if dry_run:
                    logger.info("[DRY RUN] Would sync to %s: %.1f kg at %s", target_name, m.weight_kg, m.timestamp)
                    counts[target_name] += 1
                    continue

                # Garmin-specific: check for existing entry on this date
                if target_name == "garmin" and client.has_weight_on_date(m.timestamp):
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

                state.record_sync(
                    user_name=user.name,
                    measurement_id=m.measurement_id,
                    measurement_timestamp=m.timestamp.isoformat(),
                    weight_kg=m.weight_kg,
                    synced_at=datetime.now(timezone.utc).isoformat(),
                    target=target_name,
                    response=response_str,
                )
                counts[target_name] += 1
                logger.info("Synced measurement %s to %s: %.1f kg", m.measurement_id, target_name, m.weight_kg)

                # Small delay between uploads to avoid rate limiting
                time.sleep(1 if target_name == "garmin" else 0.5)

        return counts

    finally:
        eufy.close()
        for _, client in targets:
            client.close()
