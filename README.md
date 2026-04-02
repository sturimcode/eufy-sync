# eufy-sync

[![PyPI](https://img.shields.io/pypi/v/eufy-sync)](https://pypi.org/project/eufy-sync/)
[![Downloads](https://img.shields.io/pypi/dm/eufy-sync)](https://pypi.org/project/eufy-sync/)
![Python](https://img.shields.io/pypi/pyversions/eufy-sync)
![License](https://img.shields.io/badge/license-MIT-green)

Syncs body composition data from a Eufy smart scale to Garmin Connect and/or Strava. Weight, body fat %, muscle mass, bone mass, hydration, BMR, visceral fat, and metabolic age all come through to Garmin. Strava gets weight updates.

> macOS only. Requires Python 3.9+ and a terminal. Setup is guided - you just answer a few prompts.

## The problem

Eufy scales sync to Apple Health, Fitbit, and Google Fit - but not Garmin or Strava. If you use either for training, your body comp data is stuck in a separate app. This fixes that.

## Why not just use python-garminconnect?

Every Python library that talked to Garmin broke in March 2026. Garmin put Cloudflare in front of their SSO, which blocks any login that doesn't come from a real browser. [garth](https://github.com/matin/garth) is [deprecated](https://github.com/matin/garth/discussions/222), [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) can't authenticate anymore, and there's no official API.

This project gets around it with Playwright. On first run, a real Chromium window opens and you log in normally. OAuth2 tokens get saved to your system keychain and refresh on their own for about a year. After that first login, no browser needed - body comp data goes up as FIT files through Garmin's upload endpoint.

## Install

You need Python 3.9+, a Eufy scale with cloud sync, and a Garmin Connect and/or Strava account.

First, install pipx if you don't have it:
```bash
brew install pipx
```
Or if you don't use Homebrew: `pip3 install pipx`

Then install and run:
```bash
pipx install eufy-sync
eufy-sync
```

Setup is guided on first run - choose your sync targets (Garmin, Strava, or both), enter your credentials, and your data syncs automatically.

> **Note:** If you've cloned this repo, run pipx commands from outside the repo directory to avoid path conflicts (e.g., `cd /tmp && pipx install eufy-sync`).

## Usage

```bash
eufy-sync                      # sync new measurements to all configured targets
eufy-sync --status           # check last sync + token health
eufy-sync --dry-run          # preview without uploading
eufy-sync --setup-strava     # connect Strava (add to existing setup)
eufy-sync --reauth           # re-login to all targets
eufy-sync --reauth garmin    # re-login to Garmin only
eufy-sync --reauth strava    # re-authorize Strava only
eufy-sync --update-password  # change stored passwords
eufy-sync --backfill-days 30 # sync last 30 days
eufy-sync --verbose          # show detailed sync logs
eufy-sync --install-agent   # set up automatic sync
eufy-sync --uninstall-agent # remove automatic sync
eufy-sync --uninstall       # remove all data and clean up
```

## Updating

The tool checks for updates weekly and will let you know when a new version is available. To update:

```bash
pipx install --force eufy-sync
```

## Automatic sync (macOS)

On first run, you'll be asked if you want to sync automatically every 4 hours. If you say yes, a macOS Launch Agent is installed that runs in the background - weigh yourself, open your laptop later, and it syncs on its own.

Logs go to `~/.garmin-sync/sync.log`. You get a macOS notification if something fails.

To disable: `eufy-sync --uninstall-agent`

## Adding Strava

If you already have Garmin set up and want to add Strava:

1. Create a Strava API application at https://www.strava.com/settings/api
2. Set 'Authorization Callback Domain' to `localhost`
3. Run `eufy-sync --setup-strava` and enter your Client ID and Secret
4. Authorize in the browser when it opens

Future syncs will update both Garmin and Strava automatically. Strava receives weight only (no body composition - Strava's API limitation).

## How it works

```
                                                  /--> garmin_client.py --> Garmin Connect
Eufy Cloud API --> eufy_client.py --> transform.py     (FIT file + upload)
(fetch history)    (auth + pull)     (filter, dedup) \
                                          |           \--> strava_client.py --> Strava
                                      state.db              (weight update)
                                   (sync watermark)
```

1. Authenticate to Eufy cloud API, pull measurement history
2. Check local SQLite DB for what's already been synced (per target)
3. For Garmin: check for existing entries on the same date (multi-machine dedup)
4. Generate a FIT binary file and upload to Garmin Connect
5. Update athlete weight on Strava
6. Record syncs in DB

## Security

Your passwords and OAuth tokens are stored in your system keychain (macOS Keychain) - not in plaintext files. Config files in `~/.garmin-sync/` only contain email addresses and Strava API app credentials, with `600` permissions. Credentials are only sent to Eufy, Garmin, and Strava's own servers over HTTPS. They are never logged, uploaded, or transmitted anywhere else. The only other outbound call is a weekly version check to `pypi.org` (no credentials sent). You can verify this yourself - the codebase is small and the outbound calls are in `eufy_client.py`, `garmin_auth.py`, `strava_client.py`, and the update checker in `cli.py`.

On systems without keychain support (headless Linux), credentials fall back to file-based storage with `600` permissions.

## Known quirks

**Weight precision:** The Eufy cloud API returns weight at ~0.05 kg resolution, which can differ slightly from what the Eufy app displays (the app may read from Bluetooth/local storage with higher precision). In testing, most days match within 0.1 lbs, but occasional readings can be off by up to ~0.5 lbs. If Garmin displays in lbs, the kg-to-lbs conversion adds a bit more rounding on top.

## Tests

```bash
pytest tests/ -v
```

## Disclaimer

Uses unofficial APIs for Eufy and Garmin, and the official Strava API. Could break if any of these companies change things. Use at your own risk.

