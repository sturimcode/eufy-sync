# eufy-sync

[![PyPI](https://img.shields.io/pypi/v/eufy-sync)](https://pypi.org/project/eufy-sync/)
[![Downloads](https://img.shields.io/pypi/dm/eufy-sync)](https://pypi.org/project/eufy-sync/)
![Python](https://img.shields.io/pypi/pyversions/eufy-sync)
![License](https://img.shields.io/badge/license-MIT-green)

Syncs body composition from a Eufy smart scale to Garmin Connect and Strava.

> macOS only. Needs Python 3.12+ and a terminal.

## Sync targets

| Target | What syncs | Stability |
|--------|------------|-----------|
| Garmin Connect | Full body composition: weight, body fat, muscle mass, bone mass, hydration, BMR, visceral fat, metabolic age | Stable |
| Strava | Weight | Stable |

## Why

Eufy scales sync to Apple Health, Fitbit, and Google Fit, but not Garmin or Strava. If you train on either, your body comp is stuck in a separate app. This fixes that.

## How Garmin login works

Garmin has no official API for writing body composition into Connect. In March 2026 it put Cloudflare in front of its login, which broke the Python libraries that talked to it; [garth](https://github.com/matin/garth) was [deprecated](https://github.com/matin/garth/discussions/222) and stays that way.

eufy-sync logs in through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), which gets past Cloudflare without a browser. On first run you enter your Garmin email and password, plus a code if you use two-factor. The tokens save to your keychain and refresh on their own, so later runs need no login. If that direct login gets rate-limited, a Chromium window opens once for you to sign in, then it continues. Headless setups use the direct login only, since there is no screen for a browser.

## Install

You need a Eufy scale with cloud sync and a Garmin Connect and/or Strava account.

New to the terminal? Press Cmd+Space, type "terminal", hit Enter. Each block below can be pasted in whole; the lines run one after another.

**If you have [Homebrew](https://brew.sh/):**

```bash
brew install pipx
pipx ensurepath
pipx install eufy-sync
```

**If you don't, or aren't sure:** use [uv](https://docs.astral.sh/uv/). One paste, nothing to install first, and it fetches a compatible Python on its own (so a Python older than 3.12 is fine too):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install eufy-sync
```

Then open a new terminal window, so it picks up the newly installed command, and run:

```bash
eufy-sync
```

First run walks you through choosing targets and entering credentials, then runs the first sync. Whichever installer you used, `eufy-sync --update` handles updates from then on.

> **Cloned the repo?** Run install commands from outside the repo directory to avoid path conflicts, e.g. `cd /tmp && pipx install eufy-sync`.

## Usage

```bash
eufy-sync                      # sync new measurements to all configured targets
eufy-sync --status             # last sync + token health
eufy-sync --doctor             # check the whole setup and print fixes for anything wrong
eufy-sync --dry-run            # preview without uploading
eufy-sync --update             # update to the latest version
eufy-sync --setup-strava       # add Strava
eufy-sync --select-profile     # pick your profile on a shared scale
eufy-sync --reauth [target]    # re-login (all, or garmin / strava)
eufy-sync --update-password    # change stored passwords
eufy-sync --backfill-days 30   # sync the last 30 days
eufy-sync --verbose            # detailed logs
eufy-sync --install-agent      # turn automatic sync on
eufy-sync --uninstall-agent    # turn automatic sync off
eufy-sync --uninstall          # remove all data and clean up
eufy-sync --use-file-store     # store credentials in a 0o600 file, no keychain prompts
eufy-sync --use-keychain       # move credentials back into the system keychain
```

## Updating

eufy-sync checks weekly and tells you when a new version is out. To update:

```bash
eufy-sync --update
```

## Automatic sync (macOS)

On first run you can opt into syncing every 4 hours. If you do, a macOS Launch Agent runs it in the background: weigh yourself, open your laptop later, and it syncs on its own. Logs go to `~/.garmin-sync/sync.log`, and you get a notification if something fails. Turn it off with `eufy-sync --uninstall-agent`.

## Headless Linux (server or VPS)

eufy-sync runs on Linux too, and a server is a good home for it: no laptop that has to be awake. Without a system keychain, credentials fall back to a file with `600` permissions.

Set it up once over SSH with a plain `eufy-sync` run (the Garmin login and any two-factor code work in the terminal). Then schedule it with a systemd user timer:

```ini
# ~/.config/systemd/user/eufy-sync.service
[Unit]
Description=eufy-sync

[Service]
Type=oneshot
ExecStart=%h/.local/bin/eufy-sync --headless
```

```ini
# ~/.config/systemd/user/eufy-sync.timer
[Unit]
Description=Run eufy-sync every 4 hours

[Timer]
OnBootSec=5min
OnUnitActiveSec=4h

[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now eufy-sync.timer
```

If a login expires later, the sync fails with a message naming the `--reauth` command; run it over SSH the same way. The one Eufy quirk hits hardest here: the cloud only has data after the phone app has processed the weigh-in (see Known quirks).

## Adding Strava

If Garmin is already set up and you want Strava:

1. Create a Strava API app at https://www.strava.com/settings/api
2. Set "Authorization Callback Domain" to `localhost`
3. Run `eufy-sync --setup-strava` and enter your Client ID and Secret
4. Authorize in the browser when it opens

## How it works

```
Eufy Cloud  ->  eufy_client.py  ->  transform  ->  garmin_client.py  ->  Garmin (body comp)
(pull)          (auth)              (filter,    ->  strava_client.py  ->  Strava (weight)
                                    dedup,
                                    state.db)
```

On each run it pulls your Eufy history and checks a local SQLite DB for what each target already has, then uploads only what is new: full body composition to Garmin through python-garminconnect's upload API (skipping dates Garmin already holds, so two machines do not double up), and the latest weight to Strava. Every sync is recorded in the DB.

## Security

Passwords and OAuth tokens live in a single item in your macOS Keychain, not plaintext files. The config in `~/.garmin-sync/` holds only email addresses and Strava app credentials, at `600` permissions. Credentials go over HTTPS to Eufy, Garmin, and Strava only, and are never logged or sent anywhere else. The one other outbound call is a weekly version check to pypi.org, with no credentials. On a host without a keychain (headless Linux), or if you choose `--use-file-store`, credentials fall back to a single `600` file at `~/.garmin-sync/credentials.json`.

### Credential storage

By default, credentials go into one keychain item, so macOS only asks to "Always Allow" once, not once per secret. The keychain is used whenever it works. Run `eufy-sync --use-file-store` to switch permanently to a `600` file with no keychain prompts at all, a good fit for headless or scheduled setups; run `eufy-sync --use-keychain` to move them back. A credentials file that was not created by `--use-file-store` does not override a working keychain. Systems without a keychain (headless Linux) use the file automatically.

## Known quirks

The Eufy cloud reports weight at about 0.05 kg resolution, so it can differ from the Eufy app, which may read Bluetooth at higher precision. Most days match within 0.1 lb; some can be off by up to ~0.5 lb, and the kg-to-lb conversion on Garmin adds a little rounding.

If more than one person uses the same Eufy account, setup asks which profile is yours, so only your weigh-ins sync. Until you choose, a sync that sees several profiles stops and lists them rather than guessing. Set it up before this existed? Run `eufy-sync --select-profile` once, then `eufy-sync --backfill-days 30` to pull any of your weigh-ins an earlier version skipped.

The Eufy cloud only returns a weigh-in after the Eufy app has processed it. If you step on the scale and a sync finds nothing, open the app once so it uploads, then run `eufy-sync` again. The tool cannot trigger that upload itself, so it shows up most on headless or scheduled setups.

## Tests

```bash
pytest tests/ -v
```

## Disclaimer

Uses unofficial APIs for Eufy and Garmin, and the official Strava API. Could break if any of them change things. Use at your own risk.
