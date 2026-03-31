from __future__ import annotations

import logging
from pathlib import Path

from garminconnect import Garmin

from src.config import GarminConfig
from src.transform import GarminBodyComposition

logger = logging.getLogger(__name__)


class GarminClient:
    def __init__(self, config: GarminConfig, token_dir: Path | None = None):
        self.config = config
        self.token_dir = token_dir or Path.home() / ".garminconnect"
        self._client: Garmin | None = None

    def authenticate(self) -> None:
        self.token_dir.mkdir(parents=True, exist_ok=True)
        self._client = Garmin(self.config.email, self.config.password)

        # Try to resume from saved tokens first (avoids SSO rate limits)
        try:
            self._client.login(str(self.token_dir))
            logger.info("Resumed Garmin session from saved tokens")
        except Exception:
            logger.info("No saved tokens or expired, doing fresh login")
            self._client.login()
            self._client.garth.dump(str(self.token_dir))
            logger.info("Authenticated to Garmin Connect and saved tokens")

    def upload_body_composition(self, body_comp: GarminBodyComposition) -> dict:
        if self._client is None:
            raise RuntimeError("Must authenticate before uploading")

        result = self._client.add_body_composition(
            timestamp=body_comp.timestamp,
            weight=body_comp.weight,
            percent_fat=body_comp.percent_fat,
            percent_hydration=body_comp.percent_hydration,
            visceral_fat_rating=body_comp.visceral_fat_rating,
            bone_mass=body_comp.bone_mass,
            muscle_mass=body_comp.muscle_mass,
            basal_met=body_comp.basal_met,
            metabolic_age=body_comp.metabolic_age,
        )
        logger.info(
            "Uploaded body comp to Garmin: %.1f kg at %s",
            body_comp.weight, body_comp.timestamp,
        )
        return result
