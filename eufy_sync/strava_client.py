"""Strava OAuth2 authentication and weight sync.

Flow:
1. First run: User registers a Strava API app at strava.com/settings/api,
   then runs --setup-strava. We open the browser for OAuth authorization,
   capture the callback code via a local HTTP server, and exchange for tokens.
2. Tokens stored in keychain (file fallback).
3. Access tokens expire after 6 hours; refresh tokens are indefinite.
4. On each sync, we PUT /athlete with the latest weight.
5. The API host follows Strava's published move to api-v3.strava.com
   (see _api_bases).
"""
from __future__ import annotations

import json
import logging
import secrets
import time
import webbrowser
from collections.abc import Callable
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from eufy_sync.config import StravaConfig

logger = logging.getLogger(__name__)

STRAVA_AUTH_URL = "https://www.strava.com/oauth/authorize"
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE_OLD = "https://www.strava.com/api/v3"
STRAVA_API_BASE_NEW = "https://api-v3.strava.com"
# Strava's changelog (2026-06-01) says the new base is available from this date.
NEW_API_BASE_FROM = date(2027, 1, 4)
# Third-party trackers report the old base stops working by this date.
# Strava's own docs name it only as the end of oauth/deauthorize.
OLD_API_BASE_UNTIL = date(2027, 6, 1)
CALLBACK_PORT = 8089
REDIRECT_URI = f"http://localhost:{CALLBACK_PORT}/callback"
REFRESH_SAFETY_MARGIN = 300  # seconds before expiry to trigger refresh


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _api_bases(today: date) -> tuple[str, ...]:
    """API bases to try, in order, for a request made on ``today``.

    Before the new host exists we use the old one. During the overlap we try
    the new host first and keep the old one as a fallback, since users may
    run an old release well past either date. Once the old host is retired,
    only the new one is left.
    """
    if today < NEW_API_BASE_FROM:
        return (STRAVA_API_BASE_OLD,)
    if today < OLD_API_BASE_UNTIL:
        return (STRAVA_API_BASE_NEW, STRAVA_API_BASE_OLD)
    return (STRAVA_API_BASE_NEW,)


def _auth_url(client_id: str, state_value: str) -> str:
    """The authorization URL, with every query value percent-encoded."""
    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "profile:write,profile:read_all",
        "approval_prompt": "force",
        "state": state_value,
    })
    return f"{STRAVA_AUTH_URL}?{query}"


def authorize_strava(config: StravaConfig) -> dict:
    """Run the OAuth authorization flow in the browser.

    Starts a local HTTP server to capture the callback, opens the browser,
    and exchanges the authorization code for tokens.

    Returns the token dict (access_token, refresh_token, expires_at).
    """
    captured_code: list[str] = []
    captured_error: list[str] = []
    state_value = secrets.token_urlsafe(32)

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlparse(self.path).query)
            if "code" in query:
                # Verify CSRF state parameter
                if query.get("state", [None])[0] != state_value:
                    self.send_response(400)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h2>Invalid state parameter</h2></body></html>")
                    return
                captured_code.append(query["code"][0])
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h2>Authorization successful!</h2>"
                    b"<p>You can close this tab and return to the terminal.</p>"
                    b"</body></html>"
                )
            elif "error" in query:
                error = query.get("error", ["unknown"])[0]
                captured_error.append(error)
                self.send_response(400)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(f"<html><body><h2>Authorization failed: {error}</h2></body></html>".encode())
            else:
                # Ignore non-callback requests (favicon, etc.)
                self.send_response(404)
                self.end_headers()

        def log_message(self, format, *args):
            pass  # Suppress HTTP server logging

    try:
        server = HTTPServer(("localhost", CALLBACK_PORT), CallbackHandler)
    except OSError as e:
        raise RuntimeError(
            f"Could not start the local OAuth callback server on port {CALLBACK_PORT} "
            f"({e}). Something else may already be using that port - close it and try again."
        ) from e

    def _serve_until_done():
        while not captured_code and not captured_error:
            server.handle_request()

    server_thread = Thread(target=_serve_until_done, daemon=True)
    server_thread.start()

    auth_url = _auth_url(config.client_id, state_value)

    print("\nOpening Strava authorization in your browser...")
    print(f"If it doesn't open, visit:\n{auth_url}\n")
    webbrowser.open(auth_url)

    server_thread.join(timeout=120)
    server.server_close()

    if captured_error:
        raise RuntimeError(
            f"Strava authorization denied: {captured_error[0]}. "
            f"Try again with: eufy-sync --setup-strava"
        )

    if not captured_code:
        raise RuntimeError(
            "Strava authorization timed out - no callback received. "
            "Make sure your Strava API app's Authorization Callback Domain is set to: localhost"
        )

    # Exchange code for tokens
    now = time.time()
    resp = httpx.post(
        STRAVA_TOKEN_URL,
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": captured_code[0],
            "grant_type": "authorization_code",
        },
        timeout=30.0,
    )

    if resp.status_code != 200:
        logger.debug("Strava token exchange failed: %d %s", resp.status_code, resp.text)
        raise RuntimeError(
            f"Failed to exchange Strava authorization code (HTTP {resp.status_code}). "
            f"Check your Client ID and Secret, then retry with: eufy-sync --setup-strava"
        )

    data = resp.json()
    tokens = {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data.get("expires_at", now + data.get("expires_in", 21600)),
    }

    _save_tokens(tokens)
    logger.info("Strava authorization complete")
    return tokens


class StravaClient:
    """Syncs weight to Strava via PUT /athlete."""

    def __init__(self, config: StravaConfig, today: Callable[[], date] = _utc_today):
        self.config = config
        self._client = httpx.Client(timeout=30.0)
        self._tokens: dict | None = None
        self._today = today

    def _api_request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send an API request, falling back to the next base only when the
        connection itself fails. Any HTTP response, including 401, 403 and
        429, comes from a live host and is returned as is."""
        bases = _api_bases(self._today())
        for base in bases[:-1]:
            try:
                return self._client.request(method, f"{base}{path}", **kwargs)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                logger.info("Could not reach %s (%s); trying %s", base, e, bases[-1])
        return self._client.request(method, f"{bases[-1]}{path}", **kwargs)

    def authenticate(self) -> None:
        """Load tokens and refresh if needed."""
        self._tokens = _load_tokens()

        if self._tokens is None:
            raise RuntimeError(
                "No Strava tokens found. Run: eufy-sync --setup-strava"
            )

        if time.time() >= (self._tokens["expires_at"] - REFRESH_SAFETY_MARGIN):
            self._refresh_access_token()

        self._client.headers["Authorization"] = f"Bearer {self._tokens['access_token']}"

    def _refresh_access_token(self) -> None:
        """Refresh the access token using the refresh token."""
        resp = self._client.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"],
            },
        )

        if resp.status_code != 200:
            logger.debug("Strava token refresh failed: %d %s", resp.status_code, resp.text)
            if resp.status_code in (400, 401):
                # The grant itself is dead - only re-authorizing will fix it.
                raise RuntimeError(
                    f"Failed to refresh Strava token (HTTP {resp.status_code}). "
                    f"Re-authorize with: eufy-sync --setup-strava"
                )
            raise RuntimeError(
                f"Temporary Strava failure refreshing the access token "
                f"(HTTP {resp.status_code}). This is not a revoked grant; retrying later should work."
            )

        data = resp.json()
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": data.get("expires_at", time.time() + data.get("expires_in", 21600)),
        }
        _save_tokens(self._tokens)
        logger.info("Refreshed Strava access token")

    def check_connection(self) -> None:
        """Verify the saved session with a read-only athlete request."""
        resp = self._api_request("GET", "/athlete")
        if resp.status_code in (401, 403):
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError("Strava rejected the session. Run: eufy-sync --setup-strava")
        resp.raise_for_status()

    def update_weight(self, weight_kg: float) -> dict:
        """Update the athlete's weight on Strava.

        Strava only accepts current weight, without a timestamp or body
        composition fields. The caller selects the newest valid reading.
        """
        resp = self._api_request(
            "PUT",
            "/athlete",
            data={"weight": round(weight_kg, 2)},
        )

        if resp.status_code != 200:
            logger.debug("Strava weight update failed: %d %s", resp.status_code, resp.text)
            # 4xx (except 429) won't recover by retrying; signal that to _retry.
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                from eufy_sync.sync import PermanentSyncError
                raise PermanentSyncError(
                    f"Failed to update Strava weight (HTTP {resp.status_code})"
                )
            raise RuntimeError(
                f"Failed to update Strava weight (HTTP {resp.status_code})"
            )

        logger.info("Updated Strava weight to %.2f kg", weight_kg)
        return resp.json()

    def token_status(self) -> dict:
        """Return token health matching GarminAuth.token_status() shape."""
        tokens = _load_tokens()
        if tokens is None:
            return {"state": "no_session", "days_remaining": None}
        if "refresh_token" not in tokens or not tokens["refresh_token"]:
            return {"state": "expired", "days_remaining": 0}
        # Strava refresh tokens are indefinite, so we report based on that
        if time.time() >= (tokens["expires_at"] - REFRESH_SAFETY_MARGIN):
            return {"state": "refresh_needed", "days_remaining": None}
        hours = int((tokens["expires_at"] - time.time()) / 3600)
        return {"state": "valid", "days_remaining": None, "hours_remaining": hours}

    def close(self) -> None:
        self._client.close()


def _load_tokens() -> dict | None:
    """Load Strava tokens from the credential store or the legacy file fallback."""
    from eufy_sync.credentials import get_token
    data = get_token("strava")
    if data:
        return data

    token_path = Path.home() / ".garmin-sync" / "strava_token.json"
    if token_path.exists():
        try:
            return json.loads(token_path.read_text())
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _save_tokens(tokens: dict) -> None:
    """Save Strava tokens to the credential store, and clear the legacy file."""
    from eufy_sync.credentials import store_token
    store_token("strava", tokens)
    token_path = Path.home() / ".garmin-sync" / "strava_token.json"
    if token_path.exists():
        token_path.unlink()
