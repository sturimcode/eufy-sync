"""Garmin Connect client for uploading body composition data.

Uses FIT file upload with OAuth2 bearer tokens from garmin_auth,
bypassing the deprecated garth/python-garminconnect libraries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

import httpx

from eufy_sync.config import GarminConfig
from eufy_sync.fit import FitEncoder
from eufy_sync.garmin_auth import API_HEADERS, GarminAuth
from eufy_sync.transform import GarminBodyComposition

logger = logging.getLogger(__name__)

CONNECT_API = "https://connectapi.garmin.com"
UPLOAD_URL = f"{CONNECT_API}/upload-service/upload"
WEIGHT_URL = f"{CONNECT_API}/weight-service/weight/range"


class GarminClient:
    def __init__(self, config: GarminConfig):
        self.config = config
        self._auth = GarminAuth(config.email, config.password)
        self._client = httpx.Client(timeout=30.0)

    def authenticate(self, allow_browser: bool = True) -> None:
        token = self._auth.ensure_authenticated(allow_browser=allow_browser)
        self._client.headers.update({
            **API_HEADERS,
            "authorization": f"Bearer {token}",
        })
        logger.info("Authenticated to Garmin Connect as %s", self.config.email)

    def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make an authenticated request, refreshing token on 401."""
        resp = self._client.request(method, url, **kwargs)
        if resp.status_code == 401:
            logger.info("Got 401, refreshing token")
            token = self._auth.ensure_authenticated()
            self._client.headers["authorization"] = f"Bearer {token}"
            resp = self._client.request(method, url, **kwargs)
        if resp.status_code >= 400:
            logger.error("API error %d: %s", resp.status_code, resp.text)
        resp.raise_for_status()
        return resp

    def _build_fit_file(self, body_comp: GarminBodyComposition) -> bytes:
        """Build a FIT binary file with body composition data."""
        dt = datetime.fromisoformat(body_comp.timestamp)

        encoder = FitEncoder()
        encoder.write_file_info()
        encoder.write_file_creator()
        encoder.write_device_info(dt)
        encoder.write_weight_scale(
            dt,
            weight=body_comp.weight,
            percent_fat=body_comp.percent_fat,
            percent_hydration=body_comp.percent_hydration,
            visceral_fat_rating=body_comp.visceral_fat_rating,
            bone_mass=body_comp.bone_mass,
            muscle_mass=body_comp.muscle_mass,
            basal_met=body_comp.basal_met,
            metabolic_age=body_comp.metabolic_age,
        )
        encoder.finish()
        return encoder.getvalue()

    def has_weight_on_date(self, dt: datetime) -> bool:
        """Check if Garmin already has a weight entry for a given date."""
        date_str = dt.strftime("%Y-%m-%d")
        try:
            resp = self._request("GET", f"{WEIGHT_URL}/{date_str}/{date_str}")
            data = resp.json()
            entries = data.get("dailyWeightSummaries", data.get("dateWeightList", []))
            return len(entries) > 0
        except Exception as e:
            # If we can't check, assume no duplicate and let the upload proceed.
            # Garmin de-dupes by timestamp server-side, so worst case is a re-upload.
            logger.warning("Garmin duplicate-check failed for %s: %s", date_str, e)
            return False

    def upload_body_composition(self, body_comp: GarminBodyComposition) -> dict:
        """Upload body composition data to Garmin Connect via FIT file."""
        fit_data = self._build_fit_file(body_comp)

        resp = self._request(
            "POST",
            UPLOAD_URL,
            files={"file": ("body_composition.fit", fit_data)},
        )
        logger.info(
            "Uploaded body comp to Garmin: %.1f kg at %s",
            body_comp.weight, body_comp.timestamp,
        )
        return resp.json() if resp.text else {"status": resp.status_code}

    def close(self) -> None:
        self._client.close()
