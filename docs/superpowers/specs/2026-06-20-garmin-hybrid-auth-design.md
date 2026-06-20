# Garmin Hybrid Auth: curl_cffi primary, browser fallback (Design)

**Date:** 2026-06-20
**Status:** approved, pending implementation plan
**Owner:** Elias

## Goal

Make Garmin login resilient by trying the browser-free `python-garminconnect` (curl_cffi) login first and falling back to the Playwright browser login when it fails. This keeps headless and zero-hardware setups working through the curl_cffi path, while giving every interactive user the browser login as a reliable backstop when curl_cffi is rate-limited or the library's login is temporarily broken.

## Background

The curl_cffi swap (merged to main, version still 1.7.3 on PyPI) works, but live testing and the library's own tracker show its login is in active cat-and-mouse with Garmin:

- The mobile login strategy returns account-wide 429s that follow a user across IPs.
- An open library issue (#369) has the post-login token-validation call returning 401 "Token is not active" for some accounts, intermittently, unresolved as of mid-June 2026.

The library is well-maintained (2,456 stars, 4 open issues, releases every 1-2 weeks, responsive owner), and its multi-strategy cascade usually gets a user in. But login can fail, and login is the critical path. The Playwright browser login uses a real browser in a different auth bucket and is currently more reliable for interactive desktop use. The hybrid gets the benefits of both.

Verified during design: the library's session serializes to `{di_token, di_refresh_token, di_client_id}`, and `client.loads()` hydrates a working session from any blob with those keys (`is_authenticated` is true when `di_token` is set; the API authorization header is `Bearer {di_token}`). So tokens obtained by the browser flow can be injected into the library session and used for upload. The browser path also bypasses the library's login-validation call, so it is immune to issue #369.

## Constraints and risks

- The browser fallback needs a display, so it cannot run in a headless context.
- Re-adding Playwright brings back the Chromium dependency. Mitigated by downloading the Chromium binary lazily, only when the fallback actually fires.
- The DI token exchange and the browser scraping are the old code being restored; they carry the same fragility they always did (Garmin can change the login page), but they are now a fallback, not the only path.

## Scope

In scope:
- Two-tier `GarminAuth.login()`: curl_cffi first, Playwright browser fallback on any fresh-login failure (interactive only).
- Bridging browser-obtained DI tokens into the library session and persisting them in the unified blob format.
- Lazy Chromium install, triggered only by the browser fallback.
- Re-adding `playwright` as a dependency; README and wizard text updates.

Out of scope:
- Changing the upload, dedup, Eufy, or Strava paths. `GarminClient` is unchanged.
- Removing the curl_cffi 429 message handling and `_is_permanent` 429 rule added earlier; they still protect the headless and mid-sync cases.
- A headless browser fallback. Headless stays curl_cffi-only.

## Architecture

All changes are in `eufy_sync/garmin_auth.py` plus dependency, CLI text, and README edits. `garmin_client.py`, `sync.py`, `transform.py`, `config.py`, `credentials.py` are unchanged.

### Restored from git history (pre-swap `garmin_auth.py`)

- `browser_login(email, password) -> str`: opens Chromium via Playwright, auto-fills the stored credentials, intercepts the Garmin SSO login XHR, returns the service ticket. Identical to the pre-swap implementation.
- `_exchange_ticket_for_tokens(service_ticket) -> tuple[str, str, str]`: posts the service ticket to Garmin's DI OAuth2 endpoint and returns `(access_token, refresh_token, client_id)`. Adapted from the pre-swap version to also return the `client_id` it succeeded with (needed so the library can refresh the token later).
- Supporting constants and helper restored: `DI_TOKEN_URL`, `DI_CLIENT_IDS`, `DI_GRANT_TYPE`, `SERVICE_URL`, `SSO_LOGIN_URL`, `_basic_auth_header`.

### New: the bridge and the two-tier login

`GarminAuth.login(interactive: bool = True) -> Garmin`:

1. Build the `Garmin` object and try to restore a saved blob via `client.loads()` (unchanged). Return it if it loads.
2. If no usable blob and `interactive` is False, raise `PermanentSyncError("Garmin login needed; run: eufy-sync --reauth")` (unchanged headless behavior; no browser).
3. **Tier 1 (curl_cffi):** call `garmin.login()`. On success, persist and return.
4. **Tier 2 (browser), only if Tier 1 raised any exception:** log that curl_cffi failed and the browser is opening, then run the browser fallback. On success, persist and return.
5. If the browser fallback also fails, raise a clear `PermanentSyncError`.

`_browser_fallback(garmin: Garmin) -> None` (new):
- `_ensure_chromium()` (lazy install).
- `ticket = browser_login(self.email, self.password)`.
- `access, refresh, client_id = _exchange_ticket_for_tokens(ticket)`.
- `blob = {"di_token": access, "di_refresh_token": refresh, "di_client_id": client_id}`.
- `garmin.client.loads(json.dumps(blob))`.

`force_reauth()` follows the same two-tier flow (curl_cffi fresh login, browser fallback), since it is always interactive.

### Error handling

- Tier 1 failure is never fatal on its own when interactive; it always falls through to the browser.
- Browser `browser_login` returns no ticket (wrong password, CAPTCHA, or the user closed the window): raise `PermanentSyncError("Garmin login failed. If you changed your password, run: eufy-sync --update-password. Otherwise re-run: eufy-sync --reauth")`.
- DI exchange failure after a captured ticket: raise `PermanentSyncError("Garmin token exchange failed; try: eufy-sync --reauth")`.
- A wrong password fails both tiers and ends on the update-password message, so the browser is the final arbiter and there is no loop.
- The earlier curl_cffi-specific 429/bad-credential conversion (`_fresh_login`) is folded into Tier 1: Tier 1 simply attempts `garmin.login()` and lets any exception trigger the fallback, so it no longer needs to translate messages itself.

### Dependency and CLI

- `pyproject.toml` / `requirements.txt`: re-add `playwright>=1.40.0` alongside `garminconnect` and `curl_cffi`.
- `eufy_sync/cli.py`: restore `_ensure_chromium()` but call it only from `_browser_fallback` in `garmin_auth.py` (lazy), never at startup. Update the first-run wizard line to "Logging in to Garmin (a browser may open if needed)." rather than promising no browser.
- `README.md`: rewrite "How Garmin login works" to describe the hybrid: curl_cffi first (browser-free, works headless), Playwright browser as an automatic fallback when needed.

## Test plan

All tests mock `garminconnect.Garmin`, `browser_login`, and `_exchange_ticket_for_tokens`; none open a real browser or hit the network.

- Tier 1 success: `garmin.login()` succeeds, the browser fallback is never called, token saved.
- Restore path: a saved blob loads without any login attempt (unchanged behavior, keep the existing test).
- Fallback path: `garmin.login()` raises (e.g., `GarminConnectTooManyRequestsError`); `browser_login` + exchange succeed; the constructed blob is passed to `garmin.client.loads()`; token saved; the returned client is the same `Garmin`.
- Headless: `login(interactive=False)` with no saved blob raises `PermanentSyncError` and never calls `browser_login`.
- Bad credentials: Tier 1 raises, `browser_login` returns no ticket (raises), and `login` surfaces the update-password `PermanentSyncError`.
- `force_reauth`: clears the token, runs the two-tier flow, saves on success.

## Success criteria

- A normal interactive login uses curl_cffi and never opens a browser.
- When curl_cffi fails (rate-limited or library login broken), the browser opens automatically and the login completes, with the resulting tokens used for upload.
- A headless run with no token fails with a clear "run --reauth" message and no browser.
- Chromium is downloaded only when the browser fallback first runs; a user whose curl_cffi login works never downloads it.
- The test suite stays green with both paths mocked.
