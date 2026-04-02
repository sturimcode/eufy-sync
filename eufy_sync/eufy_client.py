from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from eufy_sync.config import EufyConfig

logger = logging.getLogger(__name__)

BASE_URL = "https://api.eufylife.com/v1"

COMMON_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "User-Agent": "EufyLife-iOS-3.3.7",
    "Category": "Health",
    "Language": "en",
    "Timezone": "UTC",
    "Country": "US",
    "Content-Type": "application/json",
}


@dataclass
class EufyMeasurement:
    measurement_id: str  # customer_id + update_time as unique key
    customer_id: str
    device_id: str
    timestamp: datetime
    weight_kg: float
    body_fat_pct: float | None = None
    muscle_mass_kg: float | None = None
    water_pct: float | None = None
    bone_mass_kg: float | None = None
    bmr_kcal: int | None = None
    visceral_fat_level: float | None = None
    metabolic_age: int | None = None
    bmi: float | None = None


class EufyClient:
    def __init__(self, config: EufyConfig, token_path: Path | None = None):
        self.config = config
        self.access_token: str | None = None
        self.user_id: str | None = None
        self.token_path = token_path or Path.home() / ".garmin-sync" / "eufy_token.json"
        self._client = httpx.Client(headers=COMMON_HEADERS, timeout=30.0)

    def authenticate(self) -> None:
        # Try cached token first
        if self._load_cached_token():
            return

        self._fresh_login()

    def _fresh_login(self) -> None:
        resp = self._client.post(f"{BASE_URL}/user/v2/email/login", json={
            "client_id": "eufy-app",
            "client_secret": "8FHf22gaTKu7MZXqz5zytw",  # Public app identifier from EufyLife APK, not a per-user secret
            "email": self.config.email,
            "password": self.config.password,
        })
        resp.raise_for_status()
        data = resp.json()

        if data.get("res_code") != 1:
            msg = data.get("message", "unknown error")
            raise RuntimeError(
                f"Eufy login failed: {msg}. "
                "If you changed your Eufy password, update it in .env and restart the sync."
            )

        self.access_token = data["access_token"]
        self.user_id = data["user_id"]
        expires_in = data.get("expires_in", 2592000)  # default 30 days
        self._save_token(expires_in)
        logger.info("Authenticated to Eufy as user %s", self.user_id)

    def _load_cached_token(self) -> bool:
        # Try keychain first
        from eufy_sync.credentials import get_token, _keyring_available
        if _keyring_available():
            data = get_token("eufy")
            if data and time.time() < data.get("expires_at", 0) - 3600:
                self.access_token = data["access_token"]
                self.user_id = data["user_id"]
                logger.info("Using cached Eufy token from keychain (expires in %d days)",
                            int((data["expires_at"] - time.time()) / 86400))
                return True

        # Fallback to file
        if not self.token_path.exists():
            return False
        try:
            data = json.loads(self.token_path.read_text())
            if time.time() < data["expires_at"] - 3600:
                self.access_token = data["access_token"]
                self.user_id = data["user_id"]
                logger.info("Using cached Eufy token (expires in %d days)",
                            int((data["expires_at"] - time.time()) / 86400))
                return True
        except Exception:
            pass
        return False

    def _save_token(self, expires_in: int) -> None:
        token_data = {
            "access_token": self.access_token,
            "user_id": self.user_id,
            "expires_at": time.time() + expires_in,
        }

        from eufy_sync.credentials import store_token, _keyring_available
        if _keyring_available():
            store_token("eufy", token_data)
            # Remove legacy file if it exists
            if self.token_path.exists():
                self.token_path.unlink()
            return

        # Fallback to file
        self.token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(token_data, f)

    def _clear_cached_token(self) -> None:
        from eufy_sync.credentials import delete_token, _keyring_available
        if _keyring_available():
            delete_token("eufy")
        if self.token_path.exists():
            self.token_path.unlink()
            logger.info("Cleared cached Eufy token")

    def fetch_measurements(self, after_timestamp: int | None = None) -> list[EufyMeasurement]:
        if not self.access_token or not self.user_id:
            raise RuntimeError("Must authenticate before fetching measurements")

        params = {}
        if after_timestamp is not None:
            params["after"] = str(after_timestamp)

        resp = self._client.get(
            f"{BASE_URL}/device/data",
            params=params,
            headers={"Token": self.access_token, "Uid": self.user_id},
        )

        # If token was rejected, clear cache and re-login
        needs_reauth = resp.status_code in (401, 403)
        if not needs_reauth and resp.status_code == 200:
            try:
                needs_reauth = resp.json().get("res_code") not in (1, None)
            except Exception:
                pass
        if needs_reauth:
            logger.warning("Eufy token rejected, re-authenticating...")
            self._clear_cached_token()
            self._fresh_login()
            resp = self._client.get(
                f"{BASE_URL}/device/data",
                params=params,
                headers={"Token": self.access_token, "Uid": self.user_id},
            )

        resp.raise_for_status()
        body = resp.json()

        if body.get("res_code") != 1:
            raise RuntimeError(f"Eufy fetch failed: {body.get('message', 'unknown error')}")

        raw_records = body.get("data", [])
        logger.info("Fetched %d raw measurements from Eufy", len(raw_records))
        logger.debug("Raw Eufy response: %s", body)

        measurements = []
        for record in raw_records:
            m = self._parse_record(record)
            if m is not None:
                measurements.append(m)

        return measurements

    def _parse_record(self, record: dict) -> EufyMeasurement | None:
        scale_data = record.get("scale_data")
        if not scale_data:
            logger.warning("Record missing scale_data: %s", record)
            return None

        raw_weight = scale_data.get("weight")
        if raw_weight is None or raw_weight <= 0:
            logger.warning("Record has invalid weight: %s", raw_weight)
            return None

        weight_kg = raw_weight / 10.0  # Eufy returns decigrams
        update_time = record.get("update_time", record.get("create_time", 0))
        customer_id = record.get("customer_id", "unknown")
        measurement_id = f"{customer_id}_{update_time}"

        return EufyMeasurement(
            measurement_id=measurement_id,
            customer_id=customer_id,
            device_id=record.get("device_id", "unknown"),
            timestamp=datetime.fromtimestamp(update_time, tz=timezone.utc),
            weight_kg=weight_kg,
            body_fat_pct=scale_data.get("body_fat"),
            muscle_mass_kg=scale_data.get("muscle_mass"),
            water_pct=scale_data.get("water"),
            bone_mass_kg=scale_data.get("bone_mass"),
            bmr_kcal=scale_data.get("bmr"),
            visceral_fat_level=scale_data.get("visceral_fat"),
            metabolic_age=scale_data.get("body_age"),
            bmi=scale_data.get("bmi"),
        )

    def close(self) -> None:
        self._client.close()
