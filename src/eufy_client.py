from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from src.config import EufyConfig

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
    def __init__(self, config: EufyConfig):
        self.config = config
        self.access_token: str | None = None
        self.user_id: str | None = None
        self._client = httpx.Client(headers=COMMON_HEADERS, timeout=30.0)

    def authenticate(self) -> None:
        resp = self._client.post(f"{BASE_URL}/user/v2/email/login", json={
            "client_id": "eufy-app",
            "client_secret": "8FHf22gaTKu7MZXqz5zytw",
            "email": self.config.email,
            "password": self.config.password,
        })
        resp.raise_for_status()
        data = resp.json()

        if data.get("res_code") != 1:
            raise RuntimeError(f"Eufy auth failed: {data.get('message', 'unknown error')}")

        self.access_token = data["access_token"]
        self.user_id = data["user_id"]
        logger.info("Authenticated to Eufy as user %s", self.user_id)

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
