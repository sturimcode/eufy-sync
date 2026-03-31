"""Garmin OAuth2 authentication via Playwright browser login + token refresh.

Replaces garth/python-garminconnect which broke in March 2026 when Garmin
added Cloudflare protections to their SSO endpoints.

Flow:
1. First run: Playwright opens Garmin mobile SSO, user logs in, we intercept
   the serviceTicketId from the XHR response.
2. Exchange ticket for DI (Device Identity) OAuth2 tokens.
3. Save tokens to disk.
4. Future runs: refresh tokens automatically. Browser only needed when
   refresh tokens expire (~1 year).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Garmin OAuth2 endpoints
DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
# Public client IDs from Garmin Connect mobile app - not per-user secrets
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

API_HEADERS = {
    "user-agent": "GCM-Android-5.23",
    "x-garmin-client-platform": "Android",
    "x-app-ver": "10861",
    "x-lang": "en",
}

REFRESH_SAFETY_MARGIN = 300  # seconds before expiry to trigger refresh


@dataclass
class TokenPair:
    access_token: str
    refresh_token: str
    expires_at: float  # unix timestamp
    refresh_expires_at: float

    @property
    def is_expired(self) -> bool:
        return time.time() >= (self.expires_at - REFRESH_SAFETY_MARGIN)

    @property
    def refresh_is_expired(self) -> bool:
        return time.time() >= (self.refresh_expires_at - REFRESH_SAFETY_MARGIN)


@dataclass
class GarminSession:
    di_token: TokenPair

    def to_dict(self) -> dict:
        return {"di_token": asdict(self.di_token)}

    @classmethod
    def from_dict(cls, data: dict) -> GarminSession:
        return cls(di_token=TokenPair(**data["di_token"]))


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
        """Called from JS when login XHR response is intercepted."""
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

        # Expose a Python function to JS so we can capture the login response
        context.expose_binding(
            "pirateGarminCaptureLogin",
            lambda source, data: handle_login_capture(source, data),
        )

        # Inject script to intercept fetch calls to /mobile/api/login
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
                            window.pirateGarminCaptureLogin(JSON.stringify(data));
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
                                window.pirateGarminCaptureLogin(this.responseText);
                            } catch(e) {}
                        }
                    });
                    return origSend.apply(this, args);
                };
            })();
        """)

        page = context.new_page()
        page.goto(SSO_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

        # Wait for a login form element to appear
        page.wait_for_selector(
            "input[name='username'], input[name='email'], #username, #email",
            timeout=30000,
        )

        # Auto-fill credentials
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

        # Click submit
        for selector in ["button[type='submit']", "#login-btn-signin", "button.btn-primary"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    break
            except Exception:
                continue

        # Wait for the ticket to be captured (up to 60s for MFA, CAPTCHA, etc.)
        logger.info("Waiting for Garmin login to complete (check the browser window)...")
        for _ in range(120):
            if captured_ticket:
                break
            page.wait_for_timeout(500)

        browser.close()

    if not captured_ticket:
        raise RuntimeError(
            "Garmin login failed - no service ticket captured. "
            "This usually means the login didn't complete in the browser window. "
            "Check your Garmin email/password, and watch for CAPTCHA or MFA prompts. "
            "Run again with: eufy-sync --reauth"
        )

    return captured_ticket[0]


def _exchange_ticket_for_tokens(service_ticket: str) -> TokenPair:
    """Exchange a service ticket for DI OAuth2 tokens."""
    now = time.time()

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
                return TokenPair(
                    access_token=data["access_token"],
                    refresh_token=data["refresh_token"],
                    expires_at=now + data["expires_in"],
                    refresh_expires_at=now + data.get("refresh_token_expires_in", 86400 * 365),
                )
            logger.debug("Client ID %s returned %d, trying next", client_id, resp.status_code)
        except Exception as e:
            logger.debug("Client ID %s failed: %s", client_id, e)

    raise RuntimeError(
        "Failed to exchange Garmin service ticket for tokens. "
        "This is usually a temporary Garmin server issue. Try again in a few minutes."
    )


def _refresh_di_token(token: TokenPair) -> TokenPair:
    """Refresh the DI token using its refresh token."""
    now = time.time()

    for client_id in DI_CLIENT_IDS:
        try:
            resp = httpx.post(
                DI_TOKEN_URL,
                headers={
                    "authorization": _basic_auth_header(client_id),
                    "content-type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": token.refresh_token,
                    "client_id": client_id,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info("Refreshed DI token with client_id: %s", client_id)
                return TokenPair(
                    access_token=data["access_token"],
                    refresh_token=data.get("refresh_token", token.refresh_token),
                    expires_at=now + data["expires_in"],
                    refresh_expires_at=now + data.get("refresh_token_expires_in", 86400 * 365),
                )
        except Exception as e:
            logger.debug("Refresh with %s failed: %s", client_id, e)

    raise RuntimeError(
        "Failed to refresh Garmin token. "
        "Re-login with: eufy-sync --reauth"
    )


class GarminAuth:
    """Manages Garmin OAuth2 authentication with automatic token refresh."""

    def __init__(self, email: str, password: str, session_path: Path | None = None):
        self.email = email
        self.password = password
        self.session_path = session_path or Path.home() / ".garmin-sync" / "session.json"
        self.session: GarminSession | None = None

    def needs_browser_login(self) -> bool:
        """Check if tokens are missing or fully expired (refresh token dead)."""
        session = self._load_session()
        if session is None:
            return True
        di = session.di_token
        if di.refresh_is_expired:
            return True
        return False

    def ensure_authenticated(self, allow_browser: bool = True) -> str:
        """Return a valid DI access token, refreshing or re-logging in as needed.

        If allow_browser is False and a browser login would be needed,
        raises RuntimeError instead of opening a browser window.
        """
        # Try to load saved session
        if self.session is None:
            self.session = self._load_session()

        if self.session is not None:
            di = self.session.di_token
            if not di.is_expired:
                return di.access_token

            # Try refresh
            if not di.refresh_is_expired:
                try:
                    self.session.di_token = _refresh_di_token(di)
                    self._save_session()
                    return self.session.di_token.access_token
                except Exception:
                    logger.warning("Token refresh failed, falling back to browser login")

        if not allow_browser:
            raise RuntimeError(
                "Garmin session expired (password change or token expired). "
                "Run manually to re-authenticate: eufy-sync --reauth"
            )

        # Fresh browser login
        logger.info("Starting browser login flow")
        ticket = browser_login(self.email, self.password)
        di_token = _exchange_ticket_for_tokens(ticket)
        self.session = GarminSession(di_token=di_token)
        self._save_session()
        return self.session.di_token.access_token

    def force_reauth(self) -> str:
        """Force a fresh browser login regardless of token state."""
        logger.info("Forcing browser re-authentication")
        ticket = browser_login(self.email, self.password)
        di_token = _exchange_ticket_for_tokens(ticket)
        self.session = GarminSession(di_token=di_token)
        self._save_session()
        return self.session.di_token.access_token

    def token_status(self) -> dict:
        """Return token health without exposing session internals.

        Returns {"state": "valid"|"refresh_needed"|"expired"|"no_session",
                 "days_remaining": int|None}
        """
        session = self._load_session()
        if session is None:
            return {"state": "no_session", "days_remaining": None}
        di = session.di_token
        if di.refresh_is_expired:
            return {"state": "expired", "days_remaining": 0}
        days = int((di.refresh_expires_at - time.time()) / 86400)
        if di.is_expired:
            return {"state": "refresh_needed", "days_remaining": days}
        return {"state": "valid", "days_remaining": days}

    def _load_session(self) -> GarminSession | None:
        if not self.session_path.exists():
            return None
        try:
            data = json.loads(self.session_path.read_text())
            session = GarminSession.from_dict(data)
            logger.info("Loaded saved Garmin session")
            return session
        except Exception as e:
            logger.warning("Failed to load session: %s", e)
            return None

    def _save_session(self) -> None:
        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.session_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(self.session.to_dict(), indent=2))
        logger.info("Saved Garmin session to %s", self.session_path)
