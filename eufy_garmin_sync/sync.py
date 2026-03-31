from __future__ import annotations

import json
import logging
import subprocess
import time
from datetime import datetime, timezone

from eufy_garmin_sync.config import UserConfig
from eufy_garmin_sync.eufy_client import EufyClient
from eufy_garmin_sync.garmin_client import GarminClient
from eufy_garmin_sync.state import SyncState
from eufy_garmin_sync.transform import transform

logger = logging.getLogger("eufy_garmin_sync")

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
