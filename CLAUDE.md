# CLAUDE.md — eufy-garmin-sync

## What this project does

A Python service that automatically syncs body composition data from a Eufy smart scale (via the EufyLife cloud API) to Garmin Connect. Runs as a scheduled job — no manual intervention after setup.

## Problem

Eufy smart scales sync to Apple Health, Fitbit, and Google Fit — but NOT to Garmin Connect. Users who track fitness on Garmin (cycling, running, etc.) have no way to see their body composition data alongside their activity data without manual entry. This project bridges that gap.

## Users

- **Primary:** Me (Elias). I weigh ~190 lbs, track cycling/lifting in Garmin, and want weight + body composition to flow automatically into Garmin Connect so it reflects in training metrics (power-to-weight, VO2max estimates, etc.).
- **Secondary:** My roommate, who also uses the same Eufy scale with their own Eufy account. The scale auto-identifies users.
- **Future:** Potentially other people if this evolves into a shared tool or iOS app.

## Architecture

```
Eufy Cloud API ──► eufy_client.py ──► transform.py ──► garmin_client.py ──► Garmin Connect
(fetch history)     (auth + pull)     (filter, dedup)   (FIT file + upload)
                                           │
                                       state.db
                                    (sync watermark)
```

### Data flow

1. Authenticate to Eufy cloud API (`home-api.eufylife.com/v1/`)
2. Fetch body composition measurement history for the authenticated user
3. Compare against local state DB to find new (unsynced) measurements
4. For each new measurement: generate a Garmin-compatible FIT file with body composition data
5. Upload FIT file to Garmin Connect via the unofficial API
6. Record successful sync in state DB

### Multi-user support

Each user is a separate config entry with their own Eufy credentials + Garmin credentials. The service iterates through all configured users on each sync cycle. Eufy API returns data scoped to the authenticated account, so there's no cross-contamination.

## Key dependencies

### Eufy side — reverse-engineered cloud API

**Auth endpoint:**
```
POST https://home-api.eufylife.com/v1/user/v2/email/login
Headers: { "category": "Health", "Content-Type": "application/json" }
Body: {
  "client_id": "eufy-app",
  "client_secret": "8FHf22gaTKu7MZXqz5zytw",
  "email": "<email>",
  "password": "<password>"
}
```
Returns `access_token` and `refresh_token`. Token expires ~30 days — must implement refresh logic.

**Device list:** `GET https://home-api.eufylife.com/v1/device/` with `token: <access_token>` header.

**Measurement history:** Endpoint TBD — needs exploration. Reference repos:
- `robbalmbra/eufy-api` (GitHub) — extracted API endpoints from APK, has Python example class
- `m4ary/eufylife-api-hacs` — Home Assistant custom integration that successfully pulls weight, body fat %, muscle mass, BMI for multiple users via cloud API

**Important:** This is an unofficial API. It could break at any time. Build defensively — log raw responses, handle auth failures gracefully, alert on repeated failures.

### Garmin side — python-garminconnect

**Library:** `pip install garminconnect` (v0.2.40+, actively maintained)

**Key methods:**
- `add_body_composition(timestamp, weight, percent_fat, percent_hydration, visceral_fat_mass, bone_mass, muscle_mass, basal_met, ...)` — creates a FIT file internally and uploads it. This is the preferred method.
- `add_weigh_in(weight, unitKey, timestamp)` — simpler, weight-only alternative.
- Auth uses `garth` library under the hood. Tokens stored in `~/.garminconnect` or configurable path. Supports email/password login with token persistence.

**Important:** Also unofficial. Garmin occasionally changes their auth flow. The library handles most of this but monitor for breakage.

### Metrics that map between systems

| Eufy metric | Garmin FIT field | Notes |
|---|---|---|
| Weight (kg/lbs) | weight | Core metric |
| Body fat % | percent_fat | |
| Water % | percent_hydration | |
| Muscle mass (kg) | muscle_mass | May need unit conversion |
| Bone mass (kg) | bone_mass | |
| Visceral fat | visceral_fat_mass | Check if index vs mass |
| BMR (kcal) | basal_met | |
| BMI | — | Skip — Garmin calculates from weight + height |

## Project structure

```
eufy-garmin-sync/
├── src/
│   ├── __init__.py
│   ├── eufy_client.py       # Eufy cloud API auth + data fetching
│   ├── garmin_client.py      # Garmin Connect auth + FIT upload
│   ├── transform.py          # Eufy → Garmin field mapping, unit conversion, dedup
│   ├── sync.py               # Main orchestration: fetch → transform → upload
│   ├── state.py              # SQLite state management (sync watermarks)
│   └── config.py             # Config loading + validation
├── tests/
│   ├── test_transform.py     # Unit tests for field mapping + dedup
│   ├── test_eufy_client.py   # Mock API response tests
│   └── test_sync.py          # Integration test with mocked APIs
├── config.yaml               # User credential pairs (gitignored)
├── config.example.yaml       # Template with placeholder values
├── state.db                  # SQLite sync state (gitignored)
├── requirements.txt
├── Dockerfile
├── .env.example
├── .gitignore
├── CLAUDE.md                 # This file
└── README.md
```

## Config format

```yaml
# config.yaml
sync_interval_minutes: 15
log_level: INFO

users:
  - name: "elias"
    eufy:
      email: "elias@example.com"
      password: "${EUFY_PASSWORD_ELIAS}"  # Env var interpolation
    garmin:
      email: "elias@example.com"
      password: "${GARMIN_PASSWORD_ELIAS}"

  - name: "roommate"
    eufy:
      email: "roommate@example.com"
      password: "${EUFY_PASSWORD_ROOMMATE}"
    garmin:
      email: "roommate@example.com"
      password: "${GARMIN_PASSWORD_ROOMMATE}"
```

## State DB schema

```sql
CREATE TABLE sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_name TEXT NOT NULL,
    eufy_measurement_id TEXT NOT NULL,  -- Unique ID from Eufy API
    measurement_timestamp TEXT NOT NULL, -- ISO 8601
    weight_kg REAL,
    synced_to_garmin_at TEXT NOT NULL,   -- ISO 8601
    garmin_response TEXT,                -- Raw response for debugging
    UNIQUE(user_name, eufy_measurement_id)
);

CREATE TABLE auth_tokens (
    user_name TEXT PRIMARY KEY,
    eufy_access_token TEXT,
    eufy_refresh_token TEXT,
    eufy_token_expires_at TEXT,
    garmin_token_path TEXT  -- Path to garth token directory
);
```

## Phased build plan

### Phase 1 — Local CLI (MVP)
- [ ] Eufy client: auth + fetch measurement history
- [ ] Transform layer: map Eufy fields → Garmin fields, handle unit conversions
- [ ] Garmin client: authenticate + upload body composition via `add_body_composition()`
- [ ] State DB: track synced measurements, prevent duplicates
- [ ] CLI entry point: `python -m src.sync --config config.yaml`
- [ ] Backfill mode: `--backfill-days 30` to sync historical data on first run
- [ ] Basic logging to stdout
- **Milestone:** Run manually, see weight appear in Garmin Connect

### Phase 2 — Cloud deployment
- [ ] Dockerfile with slim Python image
- [ ] Deploy to Railway or Fly.io
- [ ] Cron-style scheduling (every 15 min)
- [ ] Persistent volume for state.db
- [ ] Environment variable config for credentials
- [ ] Health check endpoint (optional — simple HTTP 200)
- [ ] Error alerting: log failures, optionally notify via email/webhook on repeated failures
- [ ] Multi-user: roommate's credentials added
- **Milestone:** Fully automated, zero-touch sync for 2 users

### Phase 3 — Hardening (optional)
- [ ] Token refresh handling for Eufy (30-day expiry)
- [ ] Retry logic with exponential backoff
- [ ] Rate limiting awareness (Garmin is sensitive to rapid requests)
- [ ] Metric validation (reject obviously wrong readings — e.g., weight < 50 lbs or > 400 lbs)
- [ ] Simple web dashboard showing sync status per user
- [ ] Prometheus-style metrics endpoint

### Phase 4 — iOS app (future, separate project)
- Would use Apple HealthKit as data source instead of Eufy cloud API
- Cleaner distribution model — users don't share credentials
- Different tech stack entirely (Swift/SwiftUI)

## Coding conventions

- **Python 3.11+** — use modern syntax (match/case, type hints, `|` union types)
- **Type hints everywhere** — all function signatures fully typed
- **dataclasses or pydantic** for data models (EufyMeasurement, GarminBodyComposition, SyncResult)
- **httpx** over requests (async-ready, better timeout handling)
- **Structured logging** with `structlog` or standard `logging` with JSON formatter
- **No secrets in code** — all credentials via env vars or config file (gitignored)
- **Tests with pytest** — mock external APIs, test transform logic thoroughly
- **.env + .gitignore from day one** — never commit credentials

## Security foundations

- `.env` and `config.yaml` in `.gitignore` from first commit
- No API keys or passwords in source code
- Token storage files (garth tokens, state.db) gitignored
- Rate limiting awareness — don't hammer either API
- Input validation on all API responses before processing

## Known risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Eufy API changes/breaks | Sync stops | Log raw responses, alert on failures, pin to known working endpoints |
| Eufy token expires (30 days) | Auth fails | Implement refresh token flow, alert on auth failures |
| Garmin auth flow changes | Upload fails | python-garminconnect is actively maintained, pin version, monitor releases |
| Garmin rate limiting | Uploads rejected | Add delays between uploads, batch conservatively, exponential backoff |
| Duplicate measurements | Double entries in Garmin | Dedup in state DB using eufy_measurement_id + timestamp |
| Scale attributes wrong user | Wrong data synced | Eufy API scopes to authenticated user's account — each user has own credentials |

## Reference repos

- **Eufy API:** https://github.com/robbalmbra/eufy-api
- **Eufy HA integration:** https://github.com/m4ary/eufylife-api-hacs
- **Garmin Connect Python:** https://github.com/cyberjunky/python-garminconnect
- **FIT file body comp example:** https://gist.github.com/janikvonrotz/c6faa987efef97535ed627130fdccaeb
- **Renpho → Garmin (similar project, CSV-based):** https://github.com/eclat-shubh/Garmin-Body-Comp-Upload

## Quick start for Claude Code

When starting a session:

1. Read this file first
2. Check if `config.yaml` exists — if not, copy from `config.example.yaml`
3. Check current phase progress against the build plan above
4. When writing new code, follow the project structure and coding conventions
5. Always run tests after making changes: `pytest tests/ -v`
6. When exploring the Eufy API, log full responses to understand the data shape before writing parsing logic

## Context about the developer

I'm a PM who codes. I'm comfortable with Python, SQL, and CLI tools. I use Claude Code as my primary development environment. I care about clean architecture but don't over-engineer — this is a personal tool that might grow. Optimize for "works reliably" over "perfect abstractions." Ship phase 1 fast, harden later.
