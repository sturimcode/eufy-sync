# CLAUDE.md - eufy-garmin-sync

## What this project does

A Python CLI tool (`eufy-sync`) that syncs body composition data from a Eufy smart scale to Garmin Connect. Published on PyPI as `eufy-garmin-sync`.

## Architecture

```
Eufy Cloud API --> eufy_client.py --> transform.py --> garmin_client.py --> Garmin Connect
(fetch history)    (auth + pull)     (filter, dedup)   (FIT file + upload)
                                          |
                                   ~/.garmin-sync/
                                   (config, tokens, state.db)
```

### Data flow

1. Authenticate to Eufy cloud API (`api.eufylife.com/v1/`)
2. Fetch body composition measurements (weight as float hectograms, divide by 10 for kg)
3. Compare against SQLite state DB to find new measurements
4. Check Garmin for existing entries on same date (multi-machine dedup)
5. Generate FIT binary file with body composition data
6. Upload FIT file to Garmin Connect via `POST /upload-service/upload`
7. Record sync in state DB

### Garmin auth (post-March 2026)

garth and python-garminconnect are deprecated - Garmin added Cloudflare blocking to all programmatic SSO. We use:

1. **First run**: Playwright opens real Chromium, user logs in, we intercept `serviceTicketId` from XHR
2. **Exchange**: ticket -> DI OAuth2 tokens via `diauth.garmin.com`
3. **Subsequent runs**: tokens auto-refresh (~1 year lifespan)
4. **Upload**: Bearer token auth against `connectapi.garmin.com` with Android-mimicking headers

Key files: `garmin_auth.py` (OAuth flow), `fit.py` (FIT binary encoder), `garmin_client.py` (upload)

## Project structure

```
eufy-garmin-sync/
├── eufy_garmin_sync/
│   ├── __init__.py          # Public API + version
│   ├── cli.py               # CLI entry point, update checker, notifications, status display
│   ├── config.py             # Config loading
│   ├── eufy_client.py        # Eufy cloud API auth + data fetching
│   ├── garmin_auth.py         # Playwright OAuth2 + token refresh + token_status()
│   ├── garmin_client.py       # Garmin Connect FIT upload
│   ├── fit.py                 # FIT binary file encoder
│   ├── state.py               # SQLite sync state
│   ├── sync.py                # Core sync logic (sync_user, retry)
│   └── transform.py           # Eufy -> Garmin field mapping + validation
├── tests/
│   ├── test_cli.py            # Config writing + permissions + Launch Agent
│   ├── test_eufy_client.py    # Record parsing
│   ├── test_fit.py            # FIT encoder (magic bytes, CRC, fields)
│   ├── test_garmin_auth.py    # token_status() states
│   ├── test_retry.py          # Retry with backoff
│   ├── test_summary.py        # One-line sync summary formatting
│   ├── test_sync.py           # State DB operations
│   ├── test_transform.py      # Field mapping + weight bounds
│   └── test_update_check.py   # PyPI version check + cache behavior
├── .github/workflows/
│   └── publish.yml            # PyPI publish on GitHub release
├── pyproject.toml             # Package config + entry point
├── com.sturimcode.eufy-garmin-sync.plist  # macOS Launch Agent
├── README.md
└── CLAUDE.md
```

## Config

Config lives at `~/.garmin-sync/config.yaml` (created by first-run wizard). Passwords stored directly in the file (chmod 600).

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
eufy-sync                      # sync new measurements
eufy-sync --status             # last sync time + token health
eufy-sync --dry-run            # preview without uploading
eufy-sync --reauth             # force Garmin browser re-login
eufy-sync --update-password    # change stored passwords
eufy-sync --backfill-days 30   # sync last N days
eufy-sync --verbose            # show detailed sync logs (default output is one line)
eufy-sync --headless           # no browser popups (Launch Agent uses this)
eufy-sync --uninstall          # remove all data, tokens, Launch Agent (offers to keep sync history)
```

## Publishing

- PyPI: `eufy-garmin-sync` v1.3.1
- Publish via GitHub Actions trusted publishing (create a release -> auto-publishes)
- Bump version in both `pyproject.toml` and `eufy_garmin_sync/__init__.py`

## Running tests

```
pytest tests/ -v
```

## Development philosophy

Clean architecture but don't over-engineer. Optimize for "works reliably" over "perfect abstractions."
