from __future__ import annotations

import json
import logging
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


@dataclass
class EufyProfile:
    customer_id: str
    last_measured: datetime
    last_weight_kg: float
    name: str | None = None


class AmbiguousProfileError(Exception):
    """Several Eufy profiles exist on the account and none has been selected.

    Carries the detected profiles so the CLI can show a picker. Not retryable.
    """

    def __init__(self, profiles: list["EufyProfile"]):
        self.profiles = profiles
        super().__init__(
            f"{len(profiles)} Eufy profiles found but none selected. "
            "Run: eufy-sync --select-profile"
        )


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
            from eufy_sync.sync import PermanentSyncError
            msg = data.get("message", "unknown error")
            raise PermanentSyncError(
                f"Eufy login failed: {msg}. "
                "If you changed your Eufy password, run: eufy-sync --update-password"
            )

        self.access_token = data["access_token"]
        self.user_id = data["user_id"]
        expires_in = data.get("expires_in", 2592000)  # default 30 days
        self._save_token(expires_in)
        logger.info("Authenticated to Eufy as user %s", self.user_id)

    def token_status(self) -> dict:
        """Return token health without authenticating."""
        from eufy_sync.credentials import get_token
        data = get_token("eufy")
        if data is None and self.token_path.exists():
            try:
                data = json.loads(self.token_path.read_text())
            except Exception:
                pass
        if data is None:
            return {"state": "no_token", "days_remaining": None}
        remaining = data.get("expires_at", 0) - time.time()
        if remaining < 3600:
            return {"state": "expired", "days_remaining": 0}
        days = int(remaining / 86400)
        return {"state": "valid", "days_remaining": days}

    def _load_cached_token(self) -> bool:
        # Try the credential store first
        from eufy_sync.credentials import get_token
        data = get_token("eufy")
        if data and time.time() < data.get("expires_at", 0) - 3600:
            self.access_token = data["access_token"]
            self.user_id = data["user_id"]
            logger.info("Using cached Eufy token from credential store (expires in %d days)",
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

        from eufy_sync.credentials import store_token
        store_token("eufy", token_data)
        # Remove legacy file if it exists
        if self.token_path.exists():
            self.token_path.unlink()

    def _clear_cached_token(self) -> None:
        from eufy_sync.credentials import delete_token
        delete_token("eufy")
        if self.token_path.exists():
            self.token_path.unlink()
            logger.info("Cleared cached Eufy token")

    def _get_records(self, after_timestamp: int | None) -> list[dict]:
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
        return raw_records

    def _list_device_ids(self) -> list[str]:
        """Return the account's device ids from /device/v2. Empty on any error."""
        resp = self._client.get(
            f"{BASE_URL}/device/v2",
            headers={"Token": self.access_token, "Uid": self.user_id},
        )
        if resp.status_code != 200:
            return []
        body = resp.json()
        if body.get("res_code") != 1:
            return []
        return [d["id"] for d in body.get("devices", []) if d.get("id")]

    def _get_raw_records(self, device_id: str, after_timestamp: int | None) -> list[dict]:
        """Fetch a device's raw Wi-Fi weight records. These appear before the
        phone app processes a weigh-in. Records live under a nullable `list`
        field. Empty on any error."""
        params = {}
        if after_timestamp is not None:
            params["after"] = str(after_timestamp)
        resp = self._client.get(
            f"{BASE_URL}/device/wifi_scale/raw_data/{device_id}",
            params=params,
            headers={"Token": self.access_token, "Uid": self.user_id},
        )
        if resp.status_code != 200:
            return []
        body = resp.json()
        if body.get("res_code") != 1:
            return []
        return body.get("list") or []

    def _parse_all(self, records: list[dict]) -> list[EufyMeasurement]:
        out = []
        for record in records:
            m = self._parse_record(record)
            if m is not None:
                out.append(m)
        return out

    def _profiles_from(self, measurements: list[EufyMeasurement]) -> list[EufyProfile]:
        latest: dict[str, EufyMeasurement] = {}
        for m in measurements:
            cur = latest.get(m.customer_id)
            if cur is None or m.timestamp > cur.timestamp:
                latest[m.customer_id] = m
        profiles = [
            EufyProfile(
                customer_id=m.customer_id,
                last_measured=m.timestamp,
                last_weight_kg=m.weight_kg,
            )
            for m in latest.values()
        ]
        profiles.sort(key=lambda p: p.last_measured, reverse=True)
        return profiles

    def list_profiles(self) -> list[EufyProfile]:
        """Return one profile per customer_id seen in the full history, newest first."""
        records = self._get_records(None)
        return self._profiles_from(self._parse_all(records))

    def fetch_measurements(self, after_timestamp: int | None = None) -> list[EufyMeasurement]:
        if self.config.customer_id:
            parsed = self._parse_all(self._get_records(after_timestamp))
            measurements = [m for m in parsed if m.customer_id == self.config.customer_id]
        else:
            # No profile selected: read full history so the profile count is
            # reliable even when only one person has weighed in recently.
            parsed = self._parse_all(self._get_records(None))
            distinct = {m.customer_id for m in parsed}
            if len(distinct) > 1:
                raise AmbiguousProfileError(self._profiles_from(parsed))
            if after_timestamp is not None:
                cutoff = datetime.fromtimestamp(after_timestamp, tz=timezone.utc)
                measurements = [m for m in parsed if m.timestamp >= cutoff]
            else:
                measurements = parsed

        if measurements:
            return measurements
        # The normal endpoints had nothing in the window. This is the headless
        # case: a weigh-in that the phone app has not processed yet. Fall back to
        # the per-device raw Wi-Fi endpoint, which exposes the weight earlier.
        return self._fetch_raw_measurements(after_timestamp)

    def _fetch_raw_measurements(self, after_timestamp: int | None) -> list[EufyMeasurement]:
        """Recover weight-only measurements from the raw Wi-Fi endpoint, applying
        the same profile and window filters as the normal path. Degrades to an
        empty list on any error so the run is never worse than today."""
        try:
            device_ids = self._list_device_ids()
        except Exception as e:
            logger.warning("Raw Wi-Fi fallback: could not list devices: %s", e)
            return []

        records: list[dict] = []
        for device_id in device_ids:
            try:
                records.extend(self._get_raw_records(device_id, after_timestamp))
            except Exception as e:
                logger.warning("Raw Wi-Fi fallback: fetch failed for %s: %s", device_id, e)

        try:
            measurements = self._parse_all(records)
            raw_count = len(measurements)
            if self.config.customer_id:
                measurements = [m for m in measurements if m.customer_id == self.config.customer_id]
            if after_timestamp is not None:
                cutoff = datetime.fromtimestamp(after_timestamp, tz=timezone.utc)
                measurements = [m for m in measurements if m.timestamp >= cutoff]
        except Exception as e:
            logger.warning("Raw Wi-Fi fallback: could not parse raw records: %s", e)
            return []

        if measurements:
            logger.info("Recovered %d weight-only measurement(s) from the raw Wi-Fi endpoint", len(measurements))
        elif raw_count:
            logger.info(
                "Raw Wi-Fi endpoint returned %d record(s) but none passed the profile or window filter "
                "(raw records may not carry a profile id)", raw_count,
            )
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
