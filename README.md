# eufy-garmin-sync

[![PyPI](https://img.shields.io/pypi/v/eufy-garmin-sync)](https://pypi.org/project/eufy-garmin-sync/)
[![Downloads](https://img.shields.io/pypi/dm/eufy-garmin-sync)](https://pypi.org/project/eufy-garmin-sync/)
![Python](https://img.shields.io/pypi/pyversions/eufy-garmin-sync)
![License](https://img.shields.io/badge/license-MIT-green)

Syncs body composition data from a Eufy smart scale to Garmin Connect. Weight, body fat %, muscle mass, bone mass, hydration, BMR, visceral fat, and metabolic age all come through.

> Requires Python 3.9+ and a terminal. Setup is guided - you just answer a few prompts.

## The problem

Eufy scales sync to Apple Health, Fitbit, and Google Fit - but not Garmin. If you use Garmin for training, your body comp data is stuck in a separate app. This fixes that.

## Why not just use python-garminconnect?

Every Python library that talked to Garmin broke in March 2026. Garmin put Cloudflare in front of their SSO, which blocks any login that doesn't come from a real browser. [garth](https://github.com/matin/garth) is [deprecated](https://github.com/matin/garth/discussions/222), [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) can't authenticate anymore, and there's no official API.

This project gets around it with Playwright. On first run, a real Chromium window opens and you log in normally. OAuth2 tokens get saved locally and refresh on their own for about a year. After that first login, no browser needed - body comp data goes up as FIT files through Garmin's upload endpoint.

## Install

You need Python 3.9+, a Eufy scale with cloud sync, and a Garmin Connect account.

**Option A - pipx (recommended):**
```bash
brew install pipx       # or: pip3 install pipx
pipx install eufy-garmin-sync
eufy-sync
```

**Option B - clone and run:**
```bash
git clone https://github.com/sturimcode/eufy-garmin-sync.git
cd eufy-garmin-sync
./setup.sh
```

Both options walk you through setup on first run - enter your credentials, log into Garmin in the browser that opens, and your data syncs automatically.

## Usage

```bash
eufy-sync                      # sync new measurements
eufy-sync --status           # check last sync + token health
eufy-sync --dry-run          # preview without uploading
eufy-sync --reauth           # re-login to Garmin if tokens expire
eufy-sync --update-password  # change stored passwords
eufy-sync --backfill-days 30 # sync last 30 days
```

## Automatic sync (macOS)

Includes a Launch Agent that syncs every 4 hours and on login. Weigh yourself, open your laptop later, and it will automatically sync.

```bash
ln -s ~/eufy-garmin-sync/com.sturimcode.eufy-garmin-sync.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist
```

Logs go to `/tmp/eufy-garmin-sync.log`. You get a macOS notification if something fails.

To stop: `launchctl unload ~/Library/LaunchAgents/com.sturimcode.eufy-garmin-sync.plist`

## How it works

```
Eufy Cloud API --> eufy_client.py --> transform.py --> garmin_client.py --> Garmin Connect
(fetch history)    (auth + pull)     (filter, dedup)   (FIT file + upload)
                                          |
                                      state.db
                                   (sync watermark)
```

1. Authenticate to Eufy cloud API, pull measurement history
2. Check local SQLite DB for what's already been synced
3. Check Garmin for existing entries on the same date (handles multi-machine dedup)
4. Generate a FIT binary file for each new measurement
5. Upload to Garmin Connect, record in DB

## Security

Your credentials and tokens are stored in `~/.garmin-sync/` with `600` permissions (only your user can read). They are only sent to Eufy and Garmin's own servers over HTTPS. They are never logged, uploaded, or transmitted anywhere else. You can verify this yourself - the codebase is small and the only outbound calls are in `eufy_client.py` and `garmin_auth.py`.

If your machine is compromised, these tokens could be used to access your Garmin account - but that's true of any saved login.

## Known quirks

**Weight precision:** The Eufy cloud API returns weight at ~0.05 kg resolution, which can differ slightly from what the Eufy app displays (the app may read from Bluetooth/local storage with higher precision). In testing, most days match within 0.1 lbs, but occasional readings can be off by up to ~0.5 lbs. If Garmin displays in lbs, the kg-to-lbs conversion adds a bit more rounding on top.

## Tests

```bash
pytest tests/ -v
```

## Disclaimer

Uses unofficial APIs for both Eufy and Garmin. Could break if either company changes things. Use at your own risk.
