"""Garmin Connect client. Delegates login, refresh, and upload to
python-garminconnect; keeps a same-date duplicate check.
"""
from __future__ import annotations

import logging
from datetime import datetime

from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from eufy_sync.config import GarminConfig
from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.transform import GarminBodyComposition

logger = logging.getLogger(__name__)


def _is_garmin_auth_failure(exc: Exception) -> bool:
    """True when a Garmin call failed because the session is dead. Covers the
    dedicated auth error and the 401/403 that the library reports as a generic
    connection error ("API Error 401 - ...")."""
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    return isinstance(exc, GarminConnectConnectionError) and (
        "401" in str(exc) or "403" in str(exc)
    )


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
        # Query by LOCAL calendar date: uploads are now filed under the local
        # date (see transform.py), so the duplicate check must match that.
        date_str = dt.astimezone().strftime("%Y-%m-%d")
        try:
            data = self._garmin.get_body_composition(date_str, date_str)
            entries = data.get("dateWeightList", data.get("dailyWeightSummaries", []))
            return len(entries) > 0
        except Exception as e:
            # Fail open: let the upload proceed; Garmin de-dupes by timestamp.
            logger.warning("Garmin duplicate-check failed for %s: %s", date_str, e)
            return False

    def delete_weight_entry(self, dt: datetime, weight_kg: float) -> bool:
        """Delete the weigh-in we uploaded on dt's local date, matched by
        weight. Used to replace a weight-only (raw Wi-Fi) upload once the
        full body-comp record for the same weigh-in arrives (issue #48).
        Fail-open: returns False when nothing matched or the API errored, and
        the caller uploads anyway - the worst case is the duplicate we would
        have had without this method."""
        date_str = dt.astimezone().strftime("%Y-%m-%d")
        try:
            data = self._garmin.get_daily_weigh_ins(date_str)
            for entry in data.get("dateWeightList", []):
                entry_kg = entry.get("weight", 0) / 1000.0  # Garmin stores grams
                if abs(entry_kg - weight_kg) <= 0.1:
                    self._garmin.delete_weigh_in(entry["samplePk"], date_str)
                    logger.info("Deleted weight-only entry (%.1f kg on %s) ahead of full body comp",
                                weight_kg, date_str)
                    return True
            logger.warning("No weight entry near %.1f kg found on %s to replace", weight_kg, date_str)
            return False
        except Exception as e:
            logger.warning("Could not delete weight entry on %s: %s", date_str, e)
            return False

    def _add_body_composition(self, body_comp: GarminBodyComposition):
        # add_body_composition also accepts visceral_fat_mass, active_met, and
        # physique_rating; the Eufy scale does not provide those, so they are omitted.
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
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            if not _is_garmin_auth_failure(e):
                raise  # transient connection error; let _retry handle it
            # Restored session is dead (token expired or revoked, seen as a 401).
            if not self._allow_interactive:
                from eufy_sync.sync import PermanentSyncError
                raise PermanentSyncError(
                    "Garmin session expired. Re-authenticate: run eufy-sync --reauth garmin"
                ) from e
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
