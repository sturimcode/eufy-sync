# eufy-sync

[![PyPI](https://img.shields.io/pypi/v/eufy-sync)](https://pypi.org/project/eufy-sync/)
[![Downloads](https://img.shields.io/pypi/dm/eufy-sync)](https://pypi.org/project/eufy-sync/)
![Python](https://img.shields.io/pypi/pyversions/eufy-sync)
![License](https://img.shields.io/badge/license-MIT-green)

Syncs body composition from a Eufy smart scale to Garmin Connect and Strava.

> macOS, Windows, and headless Linux. Needs Python 3.12+ and a terminal.

Eufy scales sync to Apple Health, Fitbit, and Google Fit, but not Garmin or Strava. If you train on either, your body comp is stuck in a separate app. This fixes that.

## What syncs

| Target | What syncs |
|--------|------------|
| Garmin Connect | Full body composition: weight, body fat, muscle mass, bone mass, hydration, BMR, visceral fat, metabolic age |
| Strava | Weight |

## Install

You need a Eufy scale with cloud sync and a Garmin Connect and/or Strava account.

Each block below can be pasted in whole; the lines run one after another.

### macOS

New to the terminal? Press Cmd+Space, type "terminal", hit Enter.

The recommended installer is [uv](https://docs.astral.sh/uv/): one paste, nothing to install first, and it fetches a compatible Python on its own (so a Python older than 3.12 is fine too):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.local/bin/env
uv tool install eufy-sync
```

Prefer [Homebrew](https://brew.sh/)?

```bash
brew install pipx
pipx ensurepath
pipx install eufy-sync
```

### Windows

Open PowerShell (press Start, type "powershell", hit Enter) and install uv:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Open a fresh PowerShell so the `uv` command is found, then run `uv tool install eufy-sync`.

### Linux

Same uv commands as macOS. Setting up a server? See [Headless Linux](#headless-linux-server-or-vps) for the scheduling recipe.

### First run

Open a new terminal window, so it picks up the newly installed command, and run:

```bash
eufy-sync
```

It walks you through choosing targets and entering credentials, then runs the first sync.

> **Cloned the repo?** Run install commands from outside the repo directory to avoid path conflicts, e.g. `cd /tmp && pipx install eufy-sync`.

## Usage

```bash
eufy-sync                      # sync new measurements to all configured targets
eufy-sync --status             # last sync + token health
eufy-sync --history            # recent sync history (a number shows more: --history 30)
eufy-sync --dry-run            # preview without uploading
eufy-sync --doctor             # check the whole setup and print fixes for anything wrong
eufy-sync --verbose            # detailed logs

# accounts and profiles
eufy-sync --setup-strava       # add Strava
eufy-sync --select-profile     # pick your profile on a shared scale
eufy-sync --reauth [target]    # re-login (all, or garmin / strava)
eufy-sync --update-password    # change stored passwords

# automation
eufy-sync --install-agent      # turn automatic sync on
eufy-sync --uninstall-agent    # turn automatic sync off
eufy-sync --headless           # never prompt; log back in on its own if the session died (for scheduled runs)

# maintenance
eufy-sync --update             # update to the latest version
eufy-sync --backfill-days 30   # sync the last 30 days
eufy-sync --repair-days 30     # re-sync the last 30 days, even what is already marked synced
eufy-sync --use-file-store     # store credentials in a 0o600 file, no keychain prompts
eufy-sync --use-keychain       # move credentials back into the system keychain
eufy-sync --uninstall          # remove all data and clean up
```

`eufy-sync --help` lists the rest (`--version`, `--config`, `--db`).

eufy-sync checks PyPI weekly and says so when a new version is out; `eufy-sync --update` installs it, whichever installer you used.

### Getting data back after it goes missing

`--backfill-days` only sends what the local record says was never synced, so it cannot help when the record and the target disagree. If you deleted weigh-ins in Garmin Connect, or a version before 1.9.0 filed them under the wrong date, run `eufy-sync --repair-days 30` (or however many days cover the damage). It re-sends every measurement in that window, whatever the local record says. Dates it never uploaded itself, and that Garmin already holds from another source, are still left alone.

Delete any wrong-dated entries in Garmin Connect first. eufy-sync never deletes data it did not just replace, so it cannot clear those for you, and they would otherwise sit next to the corrected ones.

## Automatic sync

On first run you can opt into syncing every 4 hours in the background: weigh yourself, come back later, and it has synced on its own. Logs go to `~/.garmin-sync/sync.log`, a notification tells you when a run fails, and `eufy-sync --uninstall-agent` turns it off.

- **macOS** runs it as a Launch Agent. If [terminal-notifier](https://github.com/julienXX/terminal-notifier) is installed (`brew install terminal-notifier`), clicking a failure notification opens Terminal with the fix command already running. Without it, notifications still appear; the click just does nothing useful.
- **Windows** registers a Scheduled Task that runs with no visible window. When a run fails, a toast notification names the command to fix it.
- **Linux** has no managed agent; use the systemd timer below.

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

If a login expires later, the scheduled run logs back in from the stored password without asking. It only stops when Garmin demands a security code or the password is wrong, and the message then names the command to run over SSH. The one Eufy quirk hits hardest here: the cloud only has data after the phone app has processed the weigh-in (see Known quirks).

## Adding Strava

If Garmin is already set up and you want Strava:

Since June 2026, Strava only lets paid subscribers use its API, so step 1 needs an active Strava subscription on your account. That is [Strava's requirement](https://developers.strava.com/docs/getting-started/), not this tool's, and Garmin sync works fine without it.

1. Create a Strava API app at https://www.strava.com/settings/api
2. Set "Authorization Callback Domain" to `localhost`
3. Run `eufy-sync --setup-strava` and enter your Client ID and Secret
4. Authorize in the browser when it opens

## How it works

```
Eufy Cloud  ->  eufy_client.py  ->  transform   ->  garmin_client.py  ->  Garmin (body comp)
(pull)          (auth)              (filter,    ->  strava_client.py  ->  Strava (weight)
                                    dedup,
                                    state.db)
```

On each run it pulls your Eufy history and checks a local SQLite DB for what each target already has, then uploads only what is new: full body composition to Garmin through python-garminconnect's upload API (skipping dates Garmin already holds, so two machines do not double up), and the latest weight to Strava. Every sync is recorded in the DB.

## How Garmin login works

Garmin has no official API for writing body composition into Connect. In March 2026 it put Cloudflare in front of its login, which broke the Python libraries that talked to it; [garth](https://github.com/matin/garth) was [deprecated](https://github.com/matin/garth/discussions/222) and stays that way.

eufy-sync logs in through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), which gets past Cloudflare without a browser. On first run you enter your Garmin email and password, plus a code if you use two-factor. The tokens save to your keychain and refresh on their own, so later runs need no login.

If that direct login gets rate-limited, eufy-sync can open a Chromium window once for you to sign in, then continue. That fallback is an optional extra, since Playwright is several times the size of the rest of the install and most people never need it. If a login ever needs it, the error names the exact command; it is one of these, matching your installer:

```bash
uv tool install --force 'eufy-sync[browser]'
```

```bash
pipx install --force 'eufy-sync[browser]'
```

Headless setups use the direct login only, since there is no screen for a browser.

## Security

Credentials go over HTTPS to Eufy, Garmin, and Strava only, and are never logged or sent anywhere else. The one other outbound call is a weekly version check to pypi.org, with no credentials.

Where passwords and OAuth tokens live:

- **macOS:** a single Keychain item, not plaintext files. One item means macOS asks to "Always Allow" once, not once per secret.
- **Windows:** Windows Credential Manager.
- **Headless Linux, or after `--use-file-store`:** a single `600` file at `~/.garmin-sync/credentials.json`.

The keychain is used whenever it works; systems without one fall back to the file automatically. `eufy-sync --use-file-store` makes the switch permanent, with no keychain prompts at all, a good fit for headless or scheduled setups; `eufy-sync --use-keychain` moves them back. A credentials file that was not created by `--use-file-store` does not override a working keychain. On Windows the file fallback relies on your user profile's permissions, since Windows does not honor POSIX file modes.

The config in `~/.garmin-sync/` holds only email addresses and Strava app credentials, at `600` permissions.

## Known quirks

The Eufy cloud only returns a weigh-in after the Eufy app has processed it. If you step on the scale and a sync finds nothing, open the app once so it uploads, then run `eufy-sync` again. The tool cannot trigger that upload itself, so it shows up most on headless or scheduled setups.

If more than one person uses the same Eufy account, setup asks which profile is yours, so only your weigh-ins sync. Until you choose, a sync that sees several profiles stops and lists them rather than guessing. Set it up before this existed? Run `eufy-sync --select-profile` once, then `eufy-sync --backfill-days 30` to pull any of your weigh-ins an earlier version skipped.

The Eufy cloud reports weight at about 0.05 kg resolution, so it can differ from the Eufy app, which may read Bluetooth at higher precision. Most days match within 0.1 lb; some can be off by up to ~0.5 lb, and the kg-to-lb conversion on Garmin adds a little rounding.

Garmin login failing over and over with rate-limit or Cloudflare errors, while the Garmin app works fine? Before assuming Garmin changed something, run `eufy-sync --reauth garmin`. Stale saved tokens produce exactly those errors, and a fresh login clears them.

On old Windows builds (reported on Windows Server 2016), the `uv` installer download fails because the system does not trust Let's Encrypt certificates. Update the OS root certificates, or install on a current Windows machine and copy the folder over.

## Tests

```bash
pytest tests/ -v
```

## Support

If this saves you from typing your weight into Garmin by hand, you can [buy me a coffee](https://ko-fi.com/sturim).

## Disclaimer

Uses unofficial APIs for Eufy and Garmin, and the official Strava API. Could break if any of them change things. Use at your own risk.
