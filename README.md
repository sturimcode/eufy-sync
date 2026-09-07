# eufy-sync

[![CI](https://github.com/sturimcode/eufy-sync/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/sturimcode/eufy-sync/actions/workflows/test.yml?query=branch%3Amain)
[![PyPI](https://img.shields.io/pypi/v/eufy-sync)](https://pypi.org/project/eufy-sync/)
[![Downloads](https://img.shields.io/pypi/dm/eufy-sync)](https://pypi.org/project/eufy-sync/)
![Python](https://img.shields.io/pypi/pyversions/eufy-sync)
![License](https://img.shields.io/badge/license-MIT-green)

Syncs body composition from a Eufy smart scale to Garmin Connect, plus current weight to Strava and Zwift.

> macOS, Windows, and headless Linux. Needs Python 3.12+ and a terminal.

## What syncs

| Target | What syncs |
|--------|------------|
| Garmin Connect | Full body composition: weight, body fat, muscle mass, bone mass, hydration, BMR, visceral fat, metabolic age |
| Strava | Current weight |
| Zwift (experimental, opt-in) | Current weight, verified after saving |

## Install

You need a Eufy scale with cloud sync and an account with at least one target: Garmin Connect, Strava, or Zwift.

Each installer block below can be pasted in whole; its lines run one after another.

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

Use the same uv commands as macOS. For an always-on machine, follow the [headless Linux guide](https://github.com/sturimcode/eufy-sync/blob/main/docs/headless-linux.md).

### First run

Open a new terminal window so it picks up the newly installed command, then run:

```bash
eufy-sync
```

Setup asks for your Eufy login, then lets you choose Garmin Connect, Strava, and/or Zwift. Pick any combination, including Zwift alone. It asks for the credentials each selected service needs, lets you choose your profile on a shared Eufy account, and runs the first sync. Zwift is opt-in and marked experimental.

If you want to add another target later, follow [Adding Strava](#adding-strava) or [Adding Zwift](#adding-zwift-experimental).

> **Cloned the repo?** Run install commands from outside the repo directory to avoid path conflicts, e.g. `cd /tmp && pipx install eufy-sync`.

## Common commands

```bash
eufy-sync                      # sync new measurements to every configured target
eufy-sync --status             # show the last sync and token health
eufy-sync --dry-run            # preview a sync without uploading
eufy-sync --doctor             # check the setup and print fixes
eufy-sync --history            # show recent sync history
eufy-sync --install-agent      # turn automatic sync on (macOS and Windows)
eufy-sync --uninstall-agent    # turn automatic sync off
eufy-sync --update             # update to the latest version
```

See the [command reference](https://github.com/sturimcode/eufy-sync/blob/main/docs/command-reference.md) for account, profile, recovery, storage, and maintenance commands. `eufy-sync --help` also lists every option.

eufy-sync checks PyPI weekly and says so when a new version is available. `eufy-sync --update` installs it whichever installer you used.

## Automatic sync

On macOS and Windows, setup offers automatic sync every four hours after a successful first sync, whichever targets you chose. You can also enable it later with `eufy-sync --install-agent`. Logs go to `~/.garmin-sync/sync.log`, a notification tells you when a run fails, and `eufy-sync --uninstall-agent` turns it off.

- **macOS** uses a Launch Agent. If [terminal-notifier](https://github.com/julienXX/terminal-notifier) is installed (`brew install terminal-notifier`), clicking a failure notification opens Terminal with the fix command already running. Notifications still appear without it, but clicking them will not run the fix.
- **Windows** registers a Scheduled Task that runs with no visible window. When a run fails, a toast notification names the command to fix it.
- **Linux** has no managed agent. The [headless Linux guide](https://github.com/sturimcode/eufy-sync/blob/main/docs/headless-linux.md) includes a systemd user timer.

## Adding Strava

Strava requires an active subscription to create a new API application. This is [Strava's requirement](https://developers.strava.com/docs/getting-started/), and Garmin sync works without Strava.

1. Create a Strava API app at <https://www.strava.com/settings/api>.
2. Set **Authorization Callback Domain** to `localhost`.
3. Run `eufy-sync --setup-strava` and enter the Client ID and Secret.
4. Authorize eufy-sync in the browser when it opens.

## Adding Zwift (experimental)

To add Zwift to an existing installation, run `eufy-sync --setup-zwift` and enter your Zwift email and password. Setup checks that it can read your Zwift profile before enabling sync, then saves the login in the existing credential store. Later runs reuse the saved login and renew the session automatically.

Run `eufy-sync` to sync, or enable [automatic sync](#automatic-sync). Zwift receives only the newest valid weight for your selected Eufy profile, not body composition or weight history. Each update reads your current profile, changes its weight, then reads it again to verify the save. If the weight already matches, the tool records a verified match without sending another update.

This uses an unofficial Zwift route and is experimental. It has been verified on a real account, but that does not establish reliability across every account or future Zwift changes. Zwift failures are reported separately so Garmin and Strava can continue.

Use `eufy-sync --target zwift --dry-run` to preview a run. The first sync looks back seven days; if your latest weigh-in is older, use `eufy-sync --target zwift --backfill-days 30`. Older backfill cannot replace a newer weight already recorded as synced. `eufy-sync --disconnect-zwift` removes this target and its saved login from the installation.

## Privacy and limitations

There is no telemetry. Credentials go over HTTPS only to the service they belong to and are never logged. eufy-sync also checks PyPI weekly for updates without sending credentials. Passwords and tokens use your system credential store when available; headless Linux uses a local file with `600` permissions. See [Security, troubleshooting, and how it works](https://github.com/sturimcode/eufy-sync/blob/main/docs/security-and-troubleshooting.md) for storage details, login recovery, data quirks, and the sync architecture.

Eufy can send a raw Wi-Fi weight to its cloud before the phone app processes the full body composition. If only weight syncs, open the Eufy app, wait for it to process the weigh-in, then run `eufy-sync` again.

Garmin, Eufy, and Zwift use unofficial APIs here; the Strava integration uses its official API. Any of these services can change. Headless login renewal is designed to recover automatically, but cannot be guaranteed to keep working after a service changes its login flow.

## Tests

```bash
pytest tests/ -v
```

## Support

For setup trouble, start with `eufy-sync --doctor` and the [troubleshooting guide](https://github.com/sturimcode/eufy-sync/blob/main/docs/security-and-troubleshooting.md). [Report a bug](https://github.com/sturimcode/eufy-sync/issues/new?template=bug_report.yml) or open [GitHub Issues](https://github.com/sturimcode/eufy-sync/issues) for a feature request.

If this saves you from typing your weight into Garmin by hand, you can [buy me a coffee](https://ko-fi.com/sturim).
