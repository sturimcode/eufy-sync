"""Intervals.icu wellness sync through its official API.

Authentication is HTTP Basic with the literal username "API_KEY" and the
athlete's personal API key (Settings > Developer Settings) as the password.
Intervals.icu keeps one wellness record per local date, and a PUT to
/api/v1/athlete/{id}/wellness/{date} changes only the fields it sends, so
re-sending a date is safe and the last value sent for that date stays.

Only fields the published Wellness schema documents are sent: ``weight`` (kg)
and ``bodyFat`` (%). The schema has no muscle mass, bone mass, or BMR field,
and its ``hydration`` field is an integer rating rather than a percentage.
See https://intervals.icu/api/v1/docs and
https://forum.intervals.icu/t/api-access-to-intervals-icu/609.
"""
from __future__ import annotations

import logging
import math
from datetime import date
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import httpx

if TYPE_CHECKING:
    from eufy_sync.config import IntervalsConfig

logger = logging.getLogger(__name__)

API_BASE = "https://intervals.icu/api/v1"
API_USERNAME = "API_KEY"
# The API guide: "0" in an athlete-id path means the key's own athlete.
OWN_ATHLETE = "0"
SETUP_HINT = "Run: eufy-sync --setup-intervals"


def _permanent(message: str) -> Exception:
    from eufy_sync.sync import PermanentSyncError
    return PermanentSyncError(message)


def _raise_for_status(response: httpx.Response, action: str) -> None:
    """Map an unsuccessful response to the sync error classes.

    401/403: the key is wrong or revoked, or it does not belong to the
    configured athlete; only setup fixes that. 429: the API's rate limit,
    which will not clear within seconds, so stop for this run and let the
    next scheduled run send it. Other 4xx: the request itself is refused and
    a retry would send the same thing. 5xx: the service is having trouble,
    so the normal backoff applies.
    """
    status = response.status_code
    if 200 <= status < 300:
        return
    logger.debug("Intervals.icu %s failed: HTTP %d", action, status)
    if status in (401, 403):
        raise _permanent(f"Intervals.icu rejected the API key (HTTP {status}). {SETUP_HINT}")
    if status == 429:
        from eufy_sync.sync import RetryNextRunError
        raise RetryNextRunError(
            f"Intervals.icu rate limit reached during {action} (HTTP 429); the next run tries again"
        )
    if 400 <= status < 500:
        raise _permanent(f"Intervals.icu refused the {action} (HTTP {status})")
    raise RuntimeError(f"Temporary Intervals.icu failure during {action} (HTTP {status})")


class IntervalsClient:
    """Writes weight and body fat to Intervals.icu wellness records."""

    def __init__(self, config: "IntervalsConfig", transport: httpx.BaseTransport | None = None):
        self.config = config
        # transport exists for tests, which answer requests with a MockTransport.
        self._client = httpx.Client(
            base_url=API_BASE,
            auth=httpx.BasicAuth(API_USERNAME, config.api_key),
            headers={"Accept": "application/json"},
            timeout=30.0,
            follow_redirects=False,
            transport=transport,
        )

    def authenticate(self) -> None:
        """Nothing to log in to: every request carries the key. Refuse an
        empty key or athlete id here so the run reports setup, not a 401."""
        if not self.config.api_key or not self.config.athlete_id:
            raise _permanent(f"Intervals.icu is missing its API key or athlete id. {SETUP_HINT}")

    def athlete_id(self, athlete: str = OWN_ATHLETE) -> str:
        """Read the athlete profile and return its id (e.g. "i12345").

        With the default "0" this resolves the key's own athlete, which is how
        setup learns the id without asking for it."""
        response = self._client.get(f"/athlete/{quote(athlete, safe='')}/profile")
        _raise_for_status(response, "profile read")
        try:
            body = response.json()
        except ValueError as exc:
            raise RuntimeError("Intervals.icu returned invalid JSON for the profile read") from exc
        athlete_obj = body.get("athlete") if isinstance(body, dict) else None
        found = athlete_obj.get("id") if isinstance(athlete_obj, dict) else None
        if isinstance(found, int) and not isinstance(found, bool):
            found = str(found)
        if not isinstance(found, str) or not found:
            raise RuntimeError("Intervals.icu profile did not include an athlete id")
        return found

    def check_connection(self) -> None:
        """Verify the key can read the configured athlete. A key that belongs
        to another athlete is refused by the API or reads a different id."""
        found = self.athlete_id(self.config.athlete_id)
        if found != self.config.athlete_id:
            raise _permanent(
                f"The Intervals.icu API key belongs to athlete {found}, not {self.config.athlete_id}. "
                f"{SETUP_HINT}"
            )

    @staticmethod
    def wellness_payload(weight_kg: float, body_fat_pct: float | None) -> dict[str, float]:
        """The fields to send. A missing or non-finite body fat is left out,
        so the PUT leaves any value already on that date untouched."""
        payload = {"weight": round(float(weight_kg), 2)}
        if body_fat_pct is not None and math.isfinite(body_fat_pct):
            payload["bodyFat"] = round(float(body_fat_pct), 1)
        return payload

    def update_wellness(self, day: date, weight_kg: float, body_fat_pct: float | None = None) -> dict[str, Any]:
        """Write one local date's weight (and body fat when known).

        Returns what was sent, not the response: the wellness record also
        holds sleep, HRV, and other data this tool has no reason to keep."""
        payload = self.wellness_payload(weight_kg, body_fat_pct)
        response = self._client.put(
            f"/athlete/{quote(self.config.athlete_id, safe='')}/wellness/{day.isoformat()}", json=payload,
        )
        _raise_for_status(response, "wellness update")
        logger.info("Updated Intervals.icu wellness for %s: %s", day.isoformat(), payload)
        return {"date": day.isoformat(), **payload}

    def close(self) -> None:
        self._client.close()
