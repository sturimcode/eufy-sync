"""Garmin Connect client. Delegates login, refresh, and upload to
python-garminconnect; keeps a same-date duplicate check.
"""
from __future__ import annotations

import logging
from datetime import datetime

from garminconnect import GarminConnectAuthenticationError

from eufy_sync.config import GarminConfig
from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.transform import GarminBodyComposition

logger = logging.getLogger(__name__)


class GarminClient:
    def __init__(self, config: GarminConfig):
        self.config = config
        self._auth = GarminAuth(config.email, config.password)
        self._garmin = None
        self._allow_interactive = True

    def authenticate(self, allow_interactive: bool = True) -> None:
        self._allow_interactive = allow_interactive
        self._garmin = self._auth.login(interactive=allow_interactive)
        logger.info("Authenticated to Garmin Connect as %s", self.config.email)

    def has_weight_on_date(self, dt: datetime) -> bool:
        """Whether Garmin already has a weight entry for the date."""
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = self._garmin.get_body_composition(date_str, date_str)
            entries = data.get("dateWeightList", data.get("dailyWeightSummaries", []))
            return len(entries) > 0
        except Exception as e:
            # Fail open: let the upload proceed; Garmin de-dupes by timestamp.
            logger.warning("Garmin duplicate-check failed for %s: %s", date_str, e)
            return False

    def _add_body_composition(self, body_comp: GarminBodyComposition):
        return self._garmin.add_body_composition(
            timestamp=body_comp.timestamp,
            weight=body_comp.weight,
            percent_fat=body_comp.percent_fat,
            percent_hydration=body_comp.percent_hydration,
            visceral_fat_rating=body_comp.visceral_fat_rating,
            bone_mass=body_comp.bone_mass,
            muscle_mass=body_comp.muscle_mass,
            basal_met=body_comp.basal_met,
            metabolic_age=body_comp.metabolic_age,
            bmi=body_comp.bmi,
        )

    def upload_body_composition(self, body_comp: GarminBodyComposition) -> dict:
        try:
            result = self._add_body_composition(body_comp)
        except GarminConnectAuthenticationError:
            # Restored session is dead (refresh token expired or revoked).
            if not self._allow_interactive:
                from eufy_sync.sync import PermanentSyncError
                raise PermanentSyncError(
                    "Garmin session expired; run: eufy-sync --reauth"
                )
            logger.info("Garmin session expired; re-authenticating")
            self._garmin = self._auth.force_reauth()
            result = self._add_body_composition(body_comp)
        logger.info(
            "Uploaded body comp to Garmin: %.1f kg at %s",
            body_comp.weight, body_comp.timestamp,
        )
        return result if isinstance(result, dict) else {"status": "ok"}

    def close(self) -> None:
        self._garmin = None
