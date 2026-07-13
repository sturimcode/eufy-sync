"""Garmin authentication via the python-garminconnect library (browser-free).

The library logs in with curl_cffi TLS impersonation, handles MFA via a
callback, auto-refreshes the access token, and uploads body composition.
We persist its token blob to the system keychain so logins are rare.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import sys
from pathlib import Path

import httpx

from garminconnect import Garmin, GarminConnectAuthenticationError

logger = logging.getLogger(__name__)

# Garmin OAuth2 / SSO endpoints (public app identifiers, not per-user secrets)
DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
DI_CLIENT_IDS = [
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
]
DI_GRANT_TYPE = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
SERVICE_URL = "https://mobile.integration.garmin.com/gcm/android"
SSO_LOGIN_URL = (
    "https://sso.garmin.com/mobile/sso/en_US/sign-in"
    "?clientId=GCM_ANDROID_DARK"
    "&service=https://mobile.integration.garmin.com/gcm/android"
)


def _basic_auth_header(client_id: str) -> str:
    """Garmin uses Basic auth with client_id as username, empty password."""
    encoded = base64.b64encode(f"{client_id}:".encode()).decode()
    return f"Basic {encoded}"


def browser_login(email: str, password: str) -> str:
    """Open browser, log in to Garmin, intercept serviceTicketId.

    Returns the service ticket string.
    """
    from playwright.sync_api import sync_playwright

    captured_ticket = []

    def handle_login_capture(source, result_json):
        try:
            data = json.loads(result_json) if isinstance(result_json, str) else result_json
            if data.get("responseStatus", {}).get("type") == "SUCCESSFUL":
                ticket = data.get("serviceTicketId")
                if ticket:
                    captured_ticket.append(ticket)
                    logger.info("Captured service ticket from login response")
        except Exception as e:
            logger.warning("Failed to parse login capture: %s", e)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; sdk_gphone64_arm64)"
                " AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/121.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 412, "height": 915},
            is_mobile=True,
        )
        context.expose_binding(
            "eufySyncCaptureLogin",
            lambda source, data: handle_login_capture(source, data),
        )
        context.add_init_script("""
            (function() {
                const originalFetch = window.fetch;
                window.fetch = async function(...args) {
                    const response = await originalFetch.apply(this, args);
                    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
                    if (url.includes('/mobile/api/login')) {
                        try {
                            const clone = response.clone();
                            const data = await clone.json();
                            window.eufySyncCaptureLogin(JSON.stringify(data));
                        } catch(e) {}
                    }
                    return response;
                };
                const origOpen = XMLHttpRequest.prototype.open;
                const origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this._url = url;
                    return origOpen.call(this, method, url, ...rest);
                };
                XMLHttpRequest.prototype.send = function(...args) {
                    this.addEventListener('load', function() {
                        if (this._url && this._url.includes('/mobile/api/login')) {
                            try {
                                window.eufySyncCaptureLogin(this.responseText);
                            } catch(e) {}
                        }
                    });
                    return origSend.apply(this, args);
                };
            })();
        """)

        page = context.new_page()
        page.goto(SSO_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(
            "input[name='username'], input[name='email'], #username, #email",
            timeout=30000,
        )
        for selector in ["input[name='username']", "input[name='email']", "#username", "#email"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.fill(email)
                    break
            except Exception:
                continue
        for selector in ["input[name='password']", "#password"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.fill(password)
                    break
            except Exception:
                continue
        for selector in ["button[type='submit']", "#login-btn-signin", "button.btn-primary"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    break
            except Exception:
                continue

        logger.info("Waiting for Garmin login to complete (check the browser window)...")
        for _ in range(360):
            if captured_ticket:
                break
            page.wait_for_timeout(500)
        browser.close()

    if not captured_ticket:
        raise RuntimeError(
            "Garmin login failed - no service ticket captured. "
            "Check your Garmin email/password, and watch for CAPTCHA or MFA prompts."
        )
    return captured_ticket[0]


def _exchange_ticket_for_tokens(service_ticket: str) -> tuple[str, str, str]:
    """Exchange a service ticket for DI tokens. Returns (access, refresh, client_id)."""
    for client_id in DI_CLIENT_IDS:
        try:
            resp = httpx.post(
                DI_TOKEN_URL,
                headers={
                    "authorization": _basic_auth_header(client_id),
                    "content-type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": DI_GRANT_TYPE,
                    "service_ticket": service_ticket,
                    "service_url": SERVICE_URL,
                    "client_id": client_id,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info("Got DI tokens with client_id: %s", client_id)
                return data["access_token"], data["refresh_token"], client_id
            logger.debug("Client ID %s returned %d, trying next", client_id, resp.status_code)
        except Exception as e:
            logger.debug("Client ID %s failed: %s", client_id, e)
    raise RuntimeError(
        "Failed to exchange Garmin service ticket for tokens. "
        "This is usually a temporary Garmin server issue."
    )


def _ensure_chromium() -> None:
    """Install Playwright Chromium if not already present (lazy, only on fallback)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if not Path(path).exists():
                raise FileNotFoundError(path)
    except Exception:
        print("Installing Chromium for the Garmin browser login (one-time)...")
        result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if result.returncode != 0:
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError(
                "Failed to install Chromium for the Garmin browser fallback. "
                "Try: playwright install chromium"
            )


class GarminLoginCancelled(Exception):
    """The user backed out of the interactive login (empty MFA code or Ctrl+C)."""


def _mfa_prompt() -> str:
    print("Garmin emailed a security code to your account address.")
    print("No email after a minute? The stored password is likely wrong; press Enter to cancel.")
    try:
        code = input("Garmin MFA code: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("")
        raise GarminLoginCancelled("cancelled at the MFA prompt") from None
    if not code:
        raise GarminLoginCancelled("no MFA code entered")
    return code


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

        self._fresh_login(garmin)
        self._save_token(garmin)
        logger.info("Authenticated to Garmin as %s", self.email)
        return garmin

    def force_reauth(self) -> Garmin:
        """Clear the stored token and do a fresh interactive login."""
        self._clear_token()
        garmin = Garmin(self.email, self.password, prompt_mfa=_mfa_prompt)
        self._fresh_login(garmin)
        self._save_token(garmin)
        logger.info("Re-authenticated to Garmin as %s", self.email)
        return garmin

    def _fresh_login(self, garmin: Garmin) -> None:
        """Try the browser-free login; fall back to the browser on any failure.

        Two failures skip the fallback: a cancelled MFA prompt (the user chose
        to stop) and definitively rejected credentials (the browser would
        autofill the same bad password and fail minutes later)."""
        from eufy_sync.sync import PermanentSyncError
        try:
            garmin.login()
            return
        except GarminLoginCancelled as e:
            raise PermanentSyncError(
                "Garmin login cancelled. If the MFA email never arrived, the "
                "stored password is likely wrong; run: eufy-sync --update-password"
            ) from e
        except GarminConnectAuthenticationError as e:
            raise PermanentSyncError(
                "Garmin rejected the email or password. Run: eufy-sync --update-password"
            ) from e
        except Exception as e:
            logger.warning(
                "Browser-free Garmin login failed (%s); opening browser fallback", e
            )
        self._browser_fallback(garmin)

    def _browser_fallback(self, garmin: Garmin) -> None:
        """Log in via the Playwright browser and inject the tokens into the session."""
        from eufy_sync.sync import PermanentSyncError
        _ensure_chromium()
        try:
            ticket = browser_login(self.email, self.password)
        except Exception as e:
            raise PermanentSyncError(
                "Garmin login failed. If you changed your password, run: "
                "eufy-sync --update-password. Otherwise re-run: eufy-sync --reauth"
            ) from e
        try:
            access, refresh, client_id = _exchange_ticket_for_tokens(ticket)
        except Exception as e:
            raise PermanentSyncError(
                "Garmin token exchange failed; try: eufy-sync --reauth"
            ) from e
        blob = {"di_token": access, "di_refresh_token": refresh, "di_client_id": client_id}
        garmin.client.loads(json.dumps(blob))

    def token_status(self) -> dict:
        """Return token health. The library auto-refreshes, so the states
        collapse to valid (a blob exists) or no_session."""
        if self._load_token() is not None:
            return {"state": "valid", "days_remaining": None}
        return {"state": "no_session", "days_remaining": None}

    def _load_token(self) -> dict | None:
        from eufy_sync.credentials import get_token
        # A valid library blob has di_token as a STRING. The old Playwright
        # session also had a "di_token" key but as a nested dict; reject it so
        # those users migrate cleanly to a fresh login.
        data = get_token("garmin")
        if data is not None:
            return data if isinstance(data.get("di_token"), str) else None
        if not self.session_path.exists():
            return None
        try:
            data = json.loads(self.session_path.read_text())
            return data if isinstance(data.get("di_token"), str) else None
        except Exception:
            return None

    def _save_token(self, garmin: Garmin) -> None:
        blob = json.loads(garmin.client.dumps())
        from eufy_sync.credentials import store_token
        store_token("garmin", blob)
        if self.session_path.exists():
            self.session_path.unlink()

    def _clear_token(self) -> None:
        from eufy_sync.credentials import delete_token
        delete_token("garmin")
        if self.session_path.exists():
            self.session_path.unlink()
