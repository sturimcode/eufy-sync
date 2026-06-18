# Garmin Auth Swap: Playwright to python-garminconnect — Design

**Date:** 2026-06-18
**Status:** approved, pending implementation plan
**Owner:** Elias

## Goal

Replace the Playwright browser-based Garmin login with the browser-free `python-garminconnect` library (curl_cffi TLS impersonation). Let the library own Garmin login, token refresh, and the body-composition upload, while eufy-sync keeps its same-date duplicate check. This removes the heaviest dependency (Chromium), enables a fully headless first run, and offloads the Garmin authentication cat-and-mouse to an actively maintained community project.

## Background

Today Playwright is used for one thing only: the first interactive login. `browser_login` opens a Chromium window, injects JavaScript to intercept the login XHR, captures a service ticket, and exchanges it for DI OAuth2 tokens. Everything after that (token refresh, FIT generation, upload, dedup) already runs on plain httpx. So the change is surgical: replace the login + token plumbing + FIT encoder with library calls.

`python-garminconnect` 0.3.x (latest 0.3.6, June 2026) logs in fully in-process via curl_cffi with a 5-strategy fallback, handles MFA via a callback, persists tokens as a JSON string (`dumps()`/`loads()`) suitable for the keychain, auto-refreshes the access token, and exposes `add_body_composition(...)` covering every field the Eufy scale produces.

## Constraints and risks

- The library is a new external dependency that itself chases Garmin's defenses. If it breaks, eufy-sync breaks until upstream ships a fix (track record: fixes within days, ~9 releases since April 2026). Mitigation: pin a minimum version; the risk is brief outages, not permanent breakage.
- curl_cffi impersonates a browser's TLS fingerprint, which is more detectable than a real browser long-term. It is the current winning approach, not a moat.
- `curl_cffi` is a native dependency (bundled libcurl). Lighter than Chromium, fine on macOS.
- Existing users must re-login once: their Playwright-era saved session is not loadable by the new code.

## Scope

In scope:
- Replace Garmin login + token management with `python-garminconnect`.
- Use the library's `add_body_composition` for upload; keep a same-date duplicate check via the library's read API.
- Terminal MFA prompt for interactive runs; clean fail-with-reauth for headless runs.
- One-time migration: old session is treated as absent, triggering a fresh login.
- Dependency, CLI, and README updates; test rewrite.

Out of scope:
- Changing Strava, Eufy, or the sync orchestration beyond the one `authenticate` kwarg rename.
- Any change to `transform.py`'s validation logic (only its output is consumed differently).
- Keeping Playwright as a fallback (explicitly rejected; the point is to remove it).

## Architecture

### Deleted

- `eufy_sync/fit.py` and `tests/test_fit.py` — the library builds the FIT weigh-in file inside `add_body_composition`.
- The Playwright login, JS interception, DI token exchange/refresh, and `TokenPair`/`GarminSession` dataclasses in `garmin_auth.py`. Most of `tests/test_garmin_auth.py` is rewritten.

### `eufy_sync/garmin_auth.py` (rewritten)

`GarminAuth` becomes a thin manager around `garminconnect.Garmin` plus token persistence.

Public surface:
- `__init__(email, password, session_path=None)` — unchanged signature.
- `login(interactive: bool = True) -> Garmin` — returns an authenticated `garminconnect.Garmin`. Loads the saved token blob from the keychain and restores it with `client.loads(...)`; if there is no blob (or it does not load), does a fresh `Garmin(...).login()` when `interactive` is True, or raises `PermanentSyncError("Garmin login needed; run: eufy-sync --reauth")` when False. Persists the library's `dumps()` blob after a fresh login.
- `force_reauth() -> Garmin` — clears the stored token and does a fresh interactive login.
- `token_status() -> dict` — returns `{"state": "valid", "days_remaining": None}` when a token blob is present, else `{"state": "no_session", "days_remaining": None}`. (The library auto-refreshes, so the old refresh_needed/expired distinctions collapse; the shape stays compatible with the CLI formatters.)

Internal:
- MFA callback: `lambda: input("Garmin MFA code (check your email): ").strip()`, passed as `prompt_mfa` to `Garmin(...)`.
- Token persistence reuses `eufy_sync.credentials`: `store_token("garmin", blob_dict)` / `get_token("garmin")` / `delete_token("garmin")`. The blob is the library's `{di_token, di_refresh_token, di_client_id}` dict (`json.loads(garmin.client.dumps())`). File fallback for headless Linux keeps `~/.garmin-sync/session.json` holding the same dict.

### `eufy_sync/garmin_client.py` (kept surface, new internals)

`GarminClient` holds a `GarminAuth` and an authenticated `garminconnect.Garmin`. No more raw httpx client.

- `authenticate(allow_interactive: bool = True) -> None` — `self._garmin = self._auth.login(interactive=allow_interactive)`. (Renamed from `allow_browser`; the one call site in `sync.py` updates.)
- `upload_body_composition(body_comp: GarminBodyComposition) -> dict` — calls `self._garmin.add_body_composition(timestamp=body_comp.timestamp, weight=body_comp.weight, percent_fat=..., percent_hydration=..., visceral_fat_rating=..., bone_mass=..., muscle_mass=..., basal_met=..., metabolic_age=..., bmi=...)`. Exact field set confirmed against `transform.py`'s `GarminBodyComposition` during implementation.
- `has_weight_on_date(dt: datetime) -> bool` — calls the library's body-composition/weigh-in read for that date and returns whether an entry exists. On a read error, returns False (same fail-open behavior as today).
- `close() -> None` — closes the library client if it holds a session; otherwise a no-op.

### `eufy_sync/transform.py` (unchanged)

Stays the Eufy-to-Garmin boundary. `GarminBodyComposition` remains the interface `GarminClient` consumes.

### `eufy_sync/sync.py` (one-line change)

`sync_user` calls `client.authenticate(allow_browser=not headless)` today; the kwarg becomes `allow_interactive=not headless`. Nothing else changes (it still calls `has_weight_on_date`, `upload_body_composition`, `close`).

## Login, MFA, and headless behavior

- **Interactive (TTY):** a fresh login prompts for the emailed MFA code in the terminal when Garmin requires it.
- **Cached runs:** restore the blob, library auto-refreshes the access token before calls; no interaction.
- **Headless (`--headless`, launchd):** `allow_interactive=False`. If a fresh login would be needed (no blob, or the refresh token is dead), the run fails with "run: eufy-sync --reauth" instead of blocking on a prompt. This also fixes the previously latent bug where the 401-refresh path ignored the headless flag.
- **Dead refresh token surfacing mid-operation:** if a restored session fails on the first upload/dedup call with the library's authentication error, `GarminClient` re-runs `force_reauth()` when interactive, or raises `PermanentSyncError` with the reauth message when headless.

## Dependencies, CLI, README

- `pyproject.toml` / `requirements.txt`: remove `playwright`; add `garminconnect>=0.3.6` and `curl_cffi`.
- `eufy_sync/cli.py`: remove `_ensure_chromium()` and its call; remove the "A browser window will open for Garmin login" wizard text (the first sync now logs in via the terminal automatically, prompting for MFA if needed); update the Garmin branches of `_reauth`, `_show_status`, and `_print_summary` to the new `GarminAuth` surface and status text.
- `README.md`: rewrite the "How Garmin login works" section to describe the browser-free curl_cffi login accurately (this is the fuller rewrite deferred from the earlier honesty fix).

## Error handling

- Bad Garmin credentials on a fresh login -> `PermanentSyncError` surfacing as "Garmin login failed; run --update-password".
- MFA required in a non-interactive run -> `PermanentSyncError("Garmin login needed; run: eufy-sync --reauth")`.
- Library auth error on a restored session -> interactive: `force_reauth()`; headless: `PermanentSyncError` with the reauth message.
- Transient network/5xx during upload -> the existing `_retry` in `sync.py` handles it (these are not `PermanentSyncError`).

## Test plan

Delete `tests/test_fit.py`. Rewrite `tests/test_garmin_auth.py`. All new tests mock `garminconnect.Garmin` (no network).

- `GarminAuth.login` with a saved blob restores via `loads()` and does not call `login()`.
- `GarminAuth.login` with no blob and `interactive=True` calls `login()` and persists `dumps()`.
- `GarminAuth.login` with no blob and `interactive=False` raises `PermanentSyncError`.
- `GarminAuth.token_status` returns `valid` with a blob, `no_session` without.
- `GarminClient.upload_body_composition` calls `add_body_composition` with the mapped fields (assert the kwargs).
- `GarminClient.has_weight_on_date` returns True/False from the mocked read; returns False on a read exception.
- `sync.py` tests already mock `GarminClient`; confirm the `allow_interactive` rename does not break them.

## Open questions

None. Decisions resolved during brainstorming:
- Do the swap (vs keep Playwright vs hybrid): do it.
- Adoption depth: library owns login + refresh + upload; keep the same-date dedup.
- Headless: fail with reauth rather than ever prompting.
- One-time re-login for existing users: accepted.

## Success criteria

- A fresh install logs in to Garmin from the terminal with no browser window, prompting for MFA when required.
- A cached run syncs with no interaction; the access token refreshes automatically.
- A `--headless` run with no usable token fails with a clear "run --reauth" message instead of hanging.
- Body composition still lands in Garmin Connect with the same fields; the same-date dedup still prevents double-writes.
- Playwright and `fit.py` are gone; install no longer downloads Chromium.
- The test suite stays green with the library mocked.
