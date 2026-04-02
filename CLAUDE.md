# CLAUDE.md - eufy-sync

## What this project does

A Python CLI tool (`eufy-sync`) that syncs body composition data from a Eufy smart scale to Garmin Connect and/or Strava. Published on PyPI as `eufy-sync`.

## Architecture

```
                                                  /--> garmin_client.py --> Garmin Connect
Eufy Cloud API --> eufy_client.py --> transform.py     (FIT file + upload)
(fetch history)    (auth + pull)     (filter, dedup) \
                                          |           \--> strava_client.py --> Strava
                                   ~/.garmin-sync/          (weight update)
                                   (config, tokens, state.db)
```

### Data flow

1. Authenticate to Eufy cloud API (`api.eufylife.com/v1/`)
2. Fetch body composition measurements (weight as float hectograms, divide by 10 for kg)
3. Compare against SQLite state DB to find new measurements (per target)
4. For Garmin: check for existing entries on same date (multi-machine dedup)
5. Generate FIT binary file with body composition data, upload to Garmin Connect
6. Update athlete weight on Strava via `PUT /api/v3/athlete`
7. Record sync in state DB with target name

### Strava integration

- OAuth2 standard web flow: authorize URL -> local callback server on port 8089 -> exchange code for tokens
- Access tokens expire every 6 hours, refresh tokens are indefinite (rotated on each refresh)
- Only weight is synced (Strava API doesn't accept body composition data)
- Tokens stored in keychain as `token:strava` (same pattern as Garmin)
- Users must register their own Strava API app at strava.com/settings/api

### Garmin auth (post-March 2026)

garth and python-garminconnect are deprecated - Garmin added Cloudflare blocking to all programmatic SSO. We use:

1. **First run**: Playwright opens real Chromium, user logs in, we intercept `serviceTicketId` from XHR
2. **Exchange**: ticket -> DI OAuth2 tokens via `diauth.garmin.com`
3. **Subsequent runs**: tokens auto-refresh (~1 year lifespan)
4. **Upload**: Bearer token auth against `connectapi.garmin.com` with Android-mimicking headers

Key files: `garmin_auth.py` (OAuth flow), `fit.py` (FIT binary encoder), `garmin_client.py` (upload)

## Project structure

```
eufy-sync/
├── eufy_sync/
│   ├── __init__.py          # Public API + version
│   ├── cli.py               # CLI entry point, update checker, notifications, status display
│   ├── config.py             # Config loading
│   ├── eufy_client.py        # Eufy cloud API auth + data fetching
│   ├── garmin_auth.py         # Playwright OAuth2 + token refresh + token_status()
│   ├── garmin_client.py       # Garmin Connect FIT upload
│   ├── fit.py                 # FIT binary file encoder
│   ├── state.py               # SQLite sync state
│   ├── strava_client.py        # Strava OAuth2 + weight update
│   ├── sync.py                # Core multi-target sync logic (sync_user, retry)
│   └── transform.py           # Eufy -> Garmin field mapping + validation
├── tests/
│   ├── test_cli.py            # Config writing + permissions + Launch Agent
│   ├── test_eufy_client.py    # Record parsing
│   ├── test_fit.py            # FIT encoder (magic bytes, CRC, fields)
│   ├── test_garmin_auth.py    # token_status() states
│   ├── test_retry.py          # Retry with backoff
│   ├── test_strava_client.py  # Strava auth, token refresh, weight update
│   ├── test_summary.py        # One-line sync summary formatting (multi-target)
│   ├── test_sync.py           # State DB operations + multi-target + v1 migration
│   ├── test_transform.py      # Field mapping + weight bounds
│   └── test_update_check.py   # PyPI version check + cache behavior
├── .github/workflows/
│   └── publish.yml            # PyPI publish on GitHub release
├── pyproject.toml             # Package config + entry point
├── com.sturimcode.eufy-garmin-sync.plist  # macOS Launch Agent (label kept for backward compat)
├── README.md
└── CLAUDE.md
```

## Config

Config lives at `~/.garmin-sync/config.yaml` (created by first-run wizard). Passwords stored in system keychain (file fallback with chmod 600). Garmin and Strava are both optional targets - at least one must be configured.

## Key technical details

- **Eufy weight format**: float hectograms (e.g., `886.5` = 88.65 kg). Precision ~0.05 kg.
- **Eufy API base**: `api.eufylife.com` (not `home-api.eufylife.com`)
- **Eufy client_secret**: `8FHf22gaTKu7MZXqz5zytw` - public app identifier from APK, not a per-user secret
- **Garmin DI client IDs**: public mobile app identifiers, tried in order (2025Q2, 2024Q4, base)
- **FIT encoder**: custom implementation in `fit.py`, stdlib only (struct, io, time)
- **Visceral fat**: maps to `visceral_fat_rating` (level), not `visceral_fat_mass` (kg)
- **BMI**: skipped - Garmin calculates from weight + height
- **Weight precision**: cloud API can differ from Eufy app by up to ~0.5 lbs (app may use Bluetooth/local data)
- **Token files**: written atomically with `os.open(..., 0o600)` to prevent TOCTOU

## CLI commands

```
eufy-sync                      # sync new measurements to all configured targets
eufy-sync --status             # last sync time + token health (all targets)
eufy-sync --dry-run            # preview without uploading
eufy-sync --setup-strava       # connect Strava (add to existing setup)
eufy-sync --reauth             # re-authenticate all targets
eufy-sync --reauth garmin      # force Garmin browser re-login
eufy-sync --reauth strava      # re-authorize Strava
eufy-sync --update-password    # change stored passwords
eufy-sync --backfill-days 30   # sync last N days
eufy-sync --verbose            # show detailed sync logs (default output is one line)
eufy-sync --headless           # no browser popups (Launch Agent uses this)
eufy-sync --uninstall          # remove all data, tokens, Launch Agent (offers to keep sync history)
```

## Publishing

- PyPI: `eufy-sync` v1.5.0
- Publish via GitHub Actions trusted publishing (create a release -> auto-publishes)
- Bump version in both `pyproject.toml` and `eufy_sync/__init__.py`

## Running tests

```
pytest tests/ -v
```

## Development philosophy

Clean architecture but don't over-engineer. Optimize for "works reliably" over "perfect abstractions."
