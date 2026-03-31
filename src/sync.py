from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import UserConfig, load_config
from src.eufy_client import EufyClient
from src.garmin_client import GarminClient
from src.state import SyncState
from src.transform import transform

logger = logging.getLogger("eufy_garmin_sync")


def sync_user(user: UserConfig, state: SyncState, backfill_days: int | None = None) -> int:
    """Sync one user's Eufy data to Garmin. Returns count of measurements synced."""
    eufy = EufyClient(user.eufy)
    garmin = GarminClient(user.garmin)

    try:
        logger.info("Syncing user: %s", user.name)
        eufy.authenticate()
        garmin.authenticate()

        # Determine how far back to fetch
        after_timestamp: int | None = None
        if backfill_days:
            after_timestamp = int(time.time()) - (backfill_days * 86400)
        else:
            after_timestamp = state.get_latest_sync_timestamp(user.name)

        measurements = eufy.fetch_measurements(after_timestamp=after_timestamp)
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

            result = garmin.upload_body_composition(body_comp)

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


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync Eufy scale data to Garmin Connect")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"), help="Path to config file")
    parser.add_argument("--backfill-days", type=int, default=None, help="Sync measurements from the last N days")
    parser.add_argument("--db", type=Path, default=Path("state.db"), help="Path to state database")
    args = parser.parse_args()

    config = load_config(args.config)
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    state = SyncState(args.db)

    try:
        total = 0
        for user in config.users:
            try:
                count = sync_user(user, state, backfill_days=args.backfill_days)
                total += count
                logger.info("User %s: synced %d measurements", user.name, count)
            except Exception:
                logger.exception("Failed to sync user %s", user.name)

        logger.info("Sync complete. Total measurements synced: %d", total)
        sys.exit(0 if total >= 0 else 1)

    finally:
        state.close()


if __name__ == "__main__":
    main()
