"""Zwift weight sync via reverse-engineered Keycloak OAuth2 password grant.

Zwift does not publish a third-party API. This module talks to the same
endpoints the Companion app uses:
- Auth: POST https://secure.zwift.com/auth/realms/zwift/tokens/access/codes
- Profile write: PUT https://us-or-rly101.zwift.com/api/profiles/me (protobuf body)

The endpoints are community-reverse-engineered and may break with any
Zwift release. Failures here should not stop Garmin/Strava sync.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from eufy_sync.config import ZwiftConfig

logger = logging.getLogger(__name__)

TOKEN_URL = "https://secure.zwift.com/auth/realms/zwift/tokens/access/codes"
PROFILE_URL = "https://us-or-rly101.zwift.com/api/profiles/me"
CLIENT_ID = "Zwift_Mobile_Link"
REFRESH_SAFETY_MARGIN = 300  # seconds before expiry to trigger refresh

# Zwift mutates profiles through its protobuf API; a JSON write returns HTTP 415.
# We avoid vendoring the whole PlayerProfile schema by editing one field on the
# wire: weight_in_grams is field 9 (a uint32, varint). Protobuf scalar fields are
# last-wins, so appending field 9 to the fetched profile blob overrides the weight
# while leaving every other field untouched.
PROTOBUF_CONTENT_TYPE = "application/x-protobuf-lite"
WEIGHT_FIELD_NUMBER = 9


def _encode_varint(value: int) -> bytes:
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _set_weight_in_profile(blob: bytes, weight_g: int) -> bytes:
    """Append PlayerProfile.weight_in_grams (field 9, varint) so it overrides any
    existing value on parse, leaving the rest of the profile blob intact."""
    tag = (WEIGHT_FIELD_NUMBER << 3) | 0  # wire type 0 = varint
    return blob + bytes([tag]) + _encode_varint(weight_g)


def _keyring_available() -> bool:
    from eufy_sync.credentials import _keyring_available as fn
    return fn()


def _token_file_path() -> Path:
    return Path.home() / ".garmin-sync" / "zwift_token.json"


def _load_tokens() -> dict | None:
    """Load Zwift tokens from keychain or file fallback."""
    if _keyring_available():
        from eufy_sync.credentials import get_token
        data = get_token("zwift")
        if data:
            return data
    path = _token_file_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _save_tokens(tokens: dict) -> None:
    """Save Zwift tokens to keychain or file fallback."""
    if _keyring_available():
        from eufy_sync.credentials import store_token
        store_token("zwift", tokens)
        path = _token_file_path()
        if path.exists():
            path.unlink()
        return

    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(tokens, indent=2))


class ZwiftClient:
    """Updates Zwift profile weight via reverse-engineered API.

    See module docstring for stability caveats.
    """

    def __init__(self, config: ZwiftConfig):
        self.config = config
        self._client = httpx.Client(timeout=30.0)
        self._tokens: dict | None = None

    def close(self) -> None:
        self._client.close()

    def authenticate(self) -> None:
        """Load tokens from cache; refresh or fresh-login as needed."""
        self._tokens = _load_tokens()

        if self._tokens is None:
            self._fresh_login()
        elif time.time() >= (self._tokens["expires_at"] - REFRESH_SAFETY_MARGIN):
            self._refresh_access_token()

        self._client.headers["Authorization"] = f"Bearer {self._tokens['access_token']}"

    def _fresh_login(self) -> None:
        """Exchange email+password for tokens via Keycloak password grant."""
        now = time.time()
        resp = self._client.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "password",
                "username": self.config.email,
                "password": self.config.password,
            },
        )
        if resp.status_code == 429:
            raise RuntimeError("Zwift login rate-limited (HTTP 429); try again later")
        if resp.status_code != 200:
            logger.debug("Zwift login failed: %d %s", resp.status_code, resp.text)
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError(
                f"Zwift login failed (HTTP {resp.status_code}). "
                f"If you changed your Zwift password, run: eufy-sync --update-password"
            )

        data = resp.json()
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": now + data["expires_in"],
        }
        _save_tokens(self._tokens)
        logger.info("Authenticated to Zwift as %s", self.config.email)

    def _refresh_access_token(self) -> None:
        """Refresh the access token using the refresh token. Falls back to fresh login."""
        now = time.time()
        resp = self._client.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"],
            },
        )
        if resp.status_code != 200:
            logger.warning("Zwift refresh failed (%d), falling back to password login", resp.status_code)
            self._tokens = None
            self._fresh_login()
            return

        data = resp.json()
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", self._tokens["refresh_token"]),
            "expires_at": now + data["expires_in"],
        }
        _save_tokens(self._tokens)
        logger.info("Refreshed Zwift access token")

    def update_weight(self, weight_kg: float) -> dict:
        """Update Zwift profile weight (in grams).

        Zwift rejects JSON profile writes (HTTP 415); the profile is mutated via
        protobuf. We fetch the current profile blob, override weight_in_grams,
        and PUT it back so no other field is disturbed. 4xx (except 429) raises
        PermanentSyncError so _retry skips it; 5xx, 429, and network errors raise
        RuntimeError so _retry retries.
        """
        weight_g = int(round(weight_kg * 1000))

        get_resp = self._client.get(PROFILE_URL, headers={"Accept": PROTOBUF_CONTENT_TYPE})
        if not (200 <= get_resp.status_code < 300):
            self._raise_for_profile(get_resp.status_code, "fetch")
        new_blob = _set_weight_in_profile(get_resp.content, weight_g)

        resp = self._client.put(
            PROFILE_URL,
            content=new_blob,
            headers={"Content-Type": PROTOBUF_CONTENT_TYPE},
        )
        if not (200 <= resp.status_code < 300):
            self._raise_for_profile(resp.status_code, "update")

        logger.info("Updated Zwift weight to %.2f kg (%d g)", weight_kg, weight_g)
        return {"status": resp.status_code}

    @staticmethod
    def _raise_for_profile(status_code: int, action: str) -> None:
        """Classify a failed Zwift profile request and raise. 4xx (non-429) is
        permanent; 5xx and 429 are retryable."""
        logger.debug("Zwift profile %s failed: HTTP %d", action, status_code)
        if 400 <= status_code < 500 and status_code != 429:
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError(f"Failed to {action} Zwift profile (HTTP {status_code})")
        raise RuntimeError(f"Failed to {action} Zwift profile (HTTP {status_code})")

    def token_status(self) -> dict:
        """Return token health matching the shape of StravaClient.token_status."""
        tokens = _load_tokens()
        if tokens is None:
            return {"state": "no_session", "days_remaining": None}
        if "refresh_token" not in tokens or not tokens["refresh_token"]:
            return {"state": "expired", "days_remaining": 0}
        now = time.time()
        if now >= (tokens["expires_at"] - REFRESH_SAFETY_MARGIN):
            return {"state": "refresh_needed", "days_remaining": None}
        hours = int((tokens["expires_at"] - now) / 3600)
        return {"state": "valid", "days_remaining": None, "hours_remaining": hours}
