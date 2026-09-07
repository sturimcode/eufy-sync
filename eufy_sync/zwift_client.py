"""Zwift authentication and verified profile-weight updates."""
from __future__ import annotations

import copy
import math
import time
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from eufy_sync.config import ZwiftConfig

TOKEN_URL = "https://secure.zwift.com/auth/realms/zwift/protocol/openid-connect/token"
PROFILE_URL = "https://us-or-rly101.zwift.com/api/profiles/me"
WRITE_URL = "https://www.zwift.com/api/profiles/me/{profile_id}"
CLIENT_ID = "Zwift_Mobile_Link"
TOKEN_NAME = "zwift"
REFRESH_MARGIN = 300
STABLE_FIELDS = (
    "firstName", "lastName", "male", "dob", "countryAlpha3", "height", "ftp", "useMetric",
    "imageSrc", "emailAddress", "privacy", "countryCode",
)


class _SessionRejected(RuntimeError):
    pass


def _permanent(message: str) -> Exception:
    from eufy_sync.sync import PermanentSyncError
    return PermanentSyncError(message)


def _load_tokens() -> dict[str, Any] | None:
    from eufy_sync import credentials
    vault = credentials._load_vault()
    tokens = vault.get("tokens") if isinstance(vault, dict) else None
    value = tokens.get(TOKEN_NAME) if isinstance(tokens, dict) else None
    return value if isinstance(value, dict) else None


def _save_tokens(tokens: dict[str, Any]) -> None:
    from eufy_sync.credentials import store_token
    store_token(TOKEN_NAME, tokens)


def _json_dict(response: httpx.Response, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Zwift returned invalid JSON during {stage}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Zwift returned an invalid object during {stage}")
    return value


def _profile(response: httpx.Response) -> tuple[str, int | float, dict[str, Any]]:
    value = _json_dict(response, "profile read")
    if isinstance(value.get("data"), dict):
        value = value["data"]
    profile_id, weight = value.get("id"), value.get("weight")
    if isinstance(profile_id, int) and not isinstance(profile_id, bool) and profile_id >= 0:
        profile_id = str(profile_id)
    elif not (isinstance(profile_id, str) and profile_id.isdigit()):
        raise RuntimeError("Zwift profile did not include a valid account identity")
    if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight):
        raise RuntimeError("Zwift profile did not include a valid weight")
    return profile_id, weight, value


def _token_payload(response: httpx.Response, old_refresh: str | None = None) -> dict[str, Any]:
    value = _json_dict(response, "authentication")
    access = value.get("access_token")
    refresh = value.get("refresh_token") or old_refresh
    if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
        raise RuntimeError("Zwift authentication returned incomplete tokens")
    expires_in = value.get("expires_in", 300)
    if (
        not isinstance(expires_in, (int, float)) or isinstance(expires_in, bool) or
        not math.isfinite(expires_in) or expires_in <= 0
    ):
        expires_in = 300
    return {"access_token": access, "refresh_token": refresh, "expires_at": time.time() + expires_in}


class ZwiftClient:
    def __init__(self, config: "ZwiftConfig"):
        self.config = config
        self._client = httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=False)
        self._tokens: dict[str, Any] | None = None
        self._profile_id: str | None = None

    def _auth_headers(self, token: str | None = None) -> dict[str, str]:
        access = token or (self._tokens or {}).get("access_token")
        return {"Accept": "application/json", "Authorization": f"Bearer {access}", "Cache-Control": "no-cache"}

    def _read_profile(self, token: str | None = None) -> tuple[str, int | float, dict[str, Any]]:
        response = self._client.get(PROFILE_URL, headers=self._auth_headers(token))
        if response.status_code in (401, 403):
            raise _SessionRejected("Zwift rejected the authenticated profile request")
        if 400 <= response.status_code < 500 and response.status_code != 429:
            raise _permanent(f"Zwift profile request failed (HTTP {response.status_code})")
        if not response.is_success:
            raise RuntimeError(f"Temporary Zwift profile failure (HTTP {response.status_code})")
        return _profile(response)

    def _password_login(self) -> None:
        response = self._client.post(TOKEN_URL, data={
            "grant_type": "password", "client_id": CLIENT_ID,
            "username": self.config.email, "password": self.config.password,
        }, headers={"Accept": "application/json"})
        if response.status_code in (400, 401, 403):
            raise _permanent("Zwift login was rejected; run: eufy-sync --update-password")
        if not response.is_success:
            raise RuntimeError(f"Temporary Zwift login failure (HTTP {response.status_code})")
        tokens = _token_payload(response)
        try:
            profile_id, _, _ = self._read_profile(tokens["access_token"])
        except _SessionRejected as exc:
            raise _permanent("Zwift rejected the new login session") from exc
        tokens.update({"email": self.config.email, "profile_id": profile_id})
        self._tokens, self._profile_id = tokens, profile_id
        _save_tokens(tokens)

    def _refresh(self, cached: dict[str, Any]) -> bool:
        """Refresh cached tokens. Return False only for a definitively dead grant."""
        response = self._client.post(TOKEN_URL, data={
            "grant_type": "refresh_token", "client_id": CLIENT_ID,
            "refresh_token": cached["refresh_token"],
        }, headers={"Accept": "application/json"})
        if response.status_code in (400, 401):
            return False
        if response.status_code == 403:
            raise _permanent("Zwift token refresh was forbidden")
        if not response.is_success:
            raise RuntimeError(f"Temporary Zwift token refresh failure (HTTP {response.status_code})")
        tokens = _token_payload(response, cached["refresh_token"])
        try:
            profile_id, _, _ = self._read_profile(tokens["access_token"])
        except _SessionRejected as exc:
            raise _permanent("Zwift rejected the refreshed session") from exc
        if profile_id != cached["profile_id"]:
            raise _permanent("Saved Zwift session belongs to a different profile")
        tokens.update({"email": self.config.email, "profile_id": profile_id})
        self._tokens, self._profile_id = tokens, profile_id
        _save_tokens(tokens)
        return True

    def authenticate(self, force: bool = False) -> None:
        """Authenticate, optionally validating a fresh password before replacing cache."""
        if force:
            self._password_login()
            return
        cached = _load_tokens()
        if not cached:
            self._password_login()
            return
        access = cached.get("access_token")
        refresh = cached.get("refresh_token")
        expires_at = cached.get("expires_at")
        profile_binding = cached.get("profile_id")
        valid_expiry = (
            isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and
            math.isfinite(expires_at)
        )
        valid_binding = (
            isinstance(profile_binding, int) and not isinstance(profile_binding, bool) and profile_binding >= 0
        ) or (isinstance(profile_binding, str) and profile_binding.isdigit())
        if not (
            isinstance(access, str) and access and isinstance(refresh, str) and refresh and
            valid_expiry and isinstance(cached.get("email"), str) and cached["email"] and valid_binding
        ):
            raise _permanent("Saved Zwift session is invalid; run: eufy-sync --reauth zwift")
        if cached["email"] != self.config.email:
            raise _permanent("Saved Zwift session is bound to a different email; run: eufy-sync --reauth zwift")
        cached = dict(cached)
        cached["profile_id"] = str(cached["profile_id"])
        expired = time.time() >= cached["expires_at"] - REFRESH_MARGIN
        if expired:
            if not self._refresh(cached):
                self._password_login()
            return
        self._tokens, self._profile_id = cached, cached["profile_id"]
        try:
            profile_id, _, _ = self._read_profile()
        except _SessionRejected:
            if not self._refresh(cached):
                self._password_login()
            return
        if profile_id != self._profile_id:
            raise _permanent("Saved Zwift session belongs to a different profile")

    def _recover_session(self) -> None:
        cached = self._tokens
        if not cached or not self._refresh(cached):
            self._password_login()

    def check_connection(self) -> None:
        if self._tokens is None:
            self.authenticate()
        try:
            profile_id, _, _ = self._read_profile()
        except _SessionRejected:
            self._recover_session()
            try:
                profile_id, _, _ = self._read_profile()
            except _SessionRejected as exc:
                raise _permanent("Zwift rejected the refreshed profile request") from exc
        if profile_id != self._profile_id:
            raise _permanent("Zwift profile identity changed")

    @staticmethod
    def _grams(weight_kg: float) -> int:
        try:
            value = Decimal(str(weight_kg))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError("Zwift weight must be a finite number") from exc
        if not value.is_finite() or value < Decimal("30") or value > Decimal("300"):
            raise ValueError("Zwift weight must be between 30 and 300 kg")
        return int((value * 1000).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    @staticmethod
    def _preserved(before: dict[str, Any], after: dict[str, Any]) -> bool:
        return all(field in after and after[field] == before[field] for field in STABLE_FIELDS if field in before)

    def update_weight(self, weight_kg: float) -> dict:
        if self._tokens is None:
            self.authenticate()
        grams = self._grams(weight_kg)
        try:
            profile_id, before_weight, before = self._read_profile()
        except _SessionRejected:
            self._recover_session()
            try:
                profile_id, before_weight, before = self._read_profile()
            except _SessionRejected as exc:
                raise _permanent("Zwift rejected the refreshed weight update session") from exc
        if profile_id != self._profile_id:
            raise _permanent("Zwift profile identity changed before weight update")
        if before_weight == grams:
            return {"verified": True, "changed": False, "weight_grams": grams}
        payload = copy.deepcopy(before)
        payload["weight"] = grams
        status: int | None = None
        timed_out = False
        try:
            response = self._client.put(WRITE_URL.format(profile_id=profile_id), json=payload, headers={
                **self._auth_headers(), "Source": "zwift-web",
            })
            status = response.status_code
        except httpx.HTTPError:
            timed_out = True
        try:
            after_id, after_weight, after = self._read_profile()
        except _SessionRejected:
            self._recover_session()
            try:
                after_id, after_weight, after = self._read_profile()
            except _SessionRejected as exc:
                raise _permanent("Zwift rejected the refreshed weight read-back session") from exc
        if after_id != profile_id:
            raise _permanent("Zwift profile identity changed during weight verification")
        if not self._preserved(before, after):
            raise _permanent("Zwift changed unrelated profile fields during weight verification")
        verified = after_weight == grams
        if timed_out:
            if verified:
                return {"verified": True, "changed": True, "weight_grams": grams, "write_status": "unknown"}
            raise RuntimeError("Zwift write status is unknown and read-back did not verify the target weight")
        if status is not None and 400 <= status < 500 and status != 429:
            raise _permanent(f"Zwift weight update was rejected (HTTP {status})")
        if status is None or not 200 <= status < 300:
            raise RuntimeError(f"Temporary Zwift weight update failure (HTTP {status})")
        if not verified:
            raise RuntimeError("Zwift accepted the weight request but read-back verification failed")
        return {"verified": True, "changed": True, "weight_grams": grams, "write_http_status": status}

    def token_status(self) -> dict:
        tokens = _load_tokens()
        if not tokens:
            return {"state": "no_session", "days_remaining": None}
        access = tokens.get("access_token")
        refresh = tokens.get("refresh_token")
        email = tokens.get("email")
        profile_id = tokens.get("profile_id")
        expires_at = tokens.get("expires_at")
        valid_binding = (
            isinstance(profile_id, int) and not isinstance(profile_id, bool) and profile_id >= 0
        ) or (isinstance(profile_id, str) and profile_id.isdigit())
        valid_expiry = (
            isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool) and
            math.isfinite(expires_at)
        )
        if not (
            isinstance(access, str) and access and isinstance(refresh, str) and refresh and
            isinstance(email, str) and email == self.config.email and valid_binding and valid_expiry
        ):
            return {"state": "expired", "days_remaining": 0}
        if time.time() >= expires_at - REFRESH_MARGIN:
            return {"state": "refresh_needed", "days_remaining": None}
        hours = int((expires_at - time.time()) / 3600)
        return {"state": "valid", "days_remaining": None, "hours_remaining": hours}

    def close(self) -> None:
        self._client.close()
