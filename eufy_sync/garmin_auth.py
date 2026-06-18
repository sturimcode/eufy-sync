"""Garmin authentication via the python-garminconnect library (browser-free).

The library logs in with curl_cffi TLS impersonation, handles MFA via a
callback, auto-refreshes the access token, and uploads body composition.
We persist its token blob to the system keychain so logins are rare.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)


def _mfa_prompt() -> str:
    return input("Garmin MFA code (check your email): ").strip()


class GarminAuth:
    """Manages a garminconnect.Garmin client and its persisted token blob."""

    def __init__(self, email: str, password: str, session_path: Path | None = None):
        self.email = email
        self.password = password
        self.session_path = session_path or Path.home() / ".garmin-sync" / "session.json"

    def login(self, interactive: bool = True) -> Garmin:
        """Return an authenticated Garmin client.

        Restores the saved token blob if present; otherwise does a fresh
        login (prompting for MFA) when interactive, or raises when not.
        """
        garmin = Garmin(self.email, self.password, prompt_mfa=_mfa_prompt)

        blob = self._load_token()
        if blob is not None:
            try:
                # client.loads() takes a JSON string; the stored blob is a dict.
                garmin.client.loads(json.dumps(blob))
                logger.info("Restored saved Garmin session")
                return garmin
            except Exception as e:
                logger.warning("Saved Garmin token unusable (%s); re-logging in", e)

        if not interactive:
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError("Garmin login needed; run: eufy-sync --reauth")

        garmin.login()
        self._save_token(garmin)
        logger.info("Authenticated to Garmin as %s", self.email)
        return garmin

    def force_reauth(self) -> Garmin:
        """Clear the stored token and do a fresh interactive login."""
        self._clear_token()
        garmin = Garmin(self.email, self.password, prompt_mfa=_mfa_prompt)
        garmin.login()
        self._save_token(garmin)
        logger.info("Re-authenticated to Garmin as %s", self.email)
        return garmin

    def token_status(self) -> dict:
        """Return token health. The library auto-refreshes, so the states
        collapse to valid (a blob exists) or no_session."""
        if self._load_token() is not None:
            return {"state": "valid", "days_remaining": None}
        return {"state": "no_session", "days_remaining": None}

    def _load_token(self) -> dict | None:
        from eufy_sync.credentials import get_token, _keyring_available
        # A valid library blob has di_token as a STRING. The old Playwright
        # session also had a "di_token" key but as a nested dict; reject it so
        # those users migrate cleanly to a fresh login.
        if _keyring_available():
            data = get_token("garmin")
            if data and isinstance(data.get("di_token"), str):
                return data
            return None
        if not self.session_path.exists():
            return None
        try:
            data = json.loads(self.session_path.read_text())
            return data if isinstance(data.get("di_token"), str) else None
        except Exception:
            return None

    def _save_token(self, garmin: Garmin) -> None:
        blob = json.loads(garmin.client.dumps())
        from eufy_sync.credentials import store_token, _keyring_available
        if _keyring_available():
            store_token("garmin", blob)
            if self.session_path.exists():
                self.session_path.unlink()
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.session_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(blob, indent=2))

    def _clear_token(self) -> None:
        from eufy_sync.credentials import delete_token, _keyring_available
        if _keyring_available():
            delete_token("garmin")
        if self.session_path.exists():
            self.session_path.unlink()
