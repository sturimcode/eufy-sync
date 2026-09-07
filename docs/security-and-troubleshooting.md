# Security, troubleshooting, and how it works

Start with:

```bash
eufy-sync --doctor
```

It checks configuration, credentials, target authorization, the local database, and automatic sync, then prints a specific command for anything it can repair.

## Credential storage

Credentials travel over HTTPS to their respective services: Eufy, Garmin, Strava, and Zwift. They are never logged or sent elsewhere. The only other outbound request is a weekly version check to `pypi.org`, without credentials.

Passwords and OAuth tokens are stored in one place:

- **macOS:** one Keychain item, so macOS can grant access to the group instead of prompting once per secret.
- **Windows:** Windows Credential Manager.
- **Headless Linux, or after `--use-file-store`:** `~/.garmin-sync/credentials.json`, with `600` permissions on systems that support POSIX modes.

The keychain is used whenever it works. Systems without one fall back to the file automatically. `eufy-sync --use-file-store` moves existing credentials to the file and keeps using it; `eufy-sync --use-keychain` moves them back. A credentials file that was not adopted with `--use-file-store` does not override a working keychain. On Windows, file fallback relies on the user profile's permissions because Windows does not honor POSIX file modes.

`~/.garmin-sync/config.yaml` contains email addresses, the selected Eufy profile ID, and the public Strava client ID. It is written with `600` permissions on systems that support POSIX modes. Passwords and the Strava client secret remain in the credential store.

## Garmin login recovery

Garmin has no official API for writing body composition into Connect. eufy-sync logs in through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), using your Garmin email, password, and a two-factor code when required. It stores the resulting tokens and refreshes them on later runs.

Garmin put Cloudflare in front of its login in March 2026, which broke the Python libraries that had talked to it. [garth was deprecated](https://github.com/matin/garth/discussions/222), so eufy-sync uses python-garminconnect's current login path instead.

If Garmin repeatedly reports rate-limit or Cloudflare errors while the Garmin app still works, run:

```bash
eufy-sync --reauth garmin
```

Stale tokens can produce the same errors, and a new login often clears them.

If direct login is rate-limited, an interactive installation can use a Chromium fallback. The error will name the matching command for your installer:

```bash
uv tool install --force 'eufy-sync[browser]'
```

```bash
pipx install --force 'eufy-sync[browser]'
```

The browser extra is optional because Playwright is much larger than the normal installation. Headless runs cannot open this fallback. They normally renew sessions or log back in from saved credentials, but no unattended login can be guaranteed after Garmin or another service changes its authentication flow.

## Missing or incomplete measurements

The Eufy cloud can return a raw Wi-Fi weigh-in before the phone app processes it. That gives eufy-sync a weight but not body fat, muscle mass, and the other Garmin metrics. If a recent weigh-in is missing or only weight appears, open the Eufy app, wait for it to process the record, then run `eufy-sync` again. eufy-sync cannot trigger that processing.

If several people share one Eufy account, eufy-sync stops instead of guessing which profile is yours. Choose it with:

```bash
eufy-sync --select-profile
eufy-sync --backfill-days 30
```

The Eufy cloud reports weight at about 0.05 kg resolution. This can differ from the Eufy app, which may read Bluetooth data at higher precision. Most measurements match within 0.1 lb; some can differ by up to about 0.5 lb, with a little more rounding when Garmin converts kilograms to pounds.

For missing Garmin history, see [History recovery](command-reference.md#history-recovery).

## Platform problems

On old Windows versions, including reported installations on Windows Server 2016, the uv installer download can fail when the system does not trust Let's Encrypt certificates. Update the operating system's root certificates, or install on a current Windows machine and copy the installed folder.

For a scheduled Linux run, inspect its journal:

```bash
journalctl --user -u eufy-sync.service --since today
```

The complete timer setup is in [Headless Linux](headless-linux.md).

## How a sync works

```text
Eufy Cloud  ->  eufy_client.py  ->  transform   ->  garmin_client.py  ->  Garmin (body comp)
(pull)          (auth)              (filter,    ->  strava_client.py  ->  Strava (weight)
                                    dedup,      ->  zwift_client.py   ->  Zwift (weight)
                                    state.db)
```

Each run pulls Eufy history and checks the local SQLite database for what each target has already received. Garmin gets new full body-composition records through python-garminconnect's upload API. Dates Garmin already holds are skipped, which helps avoid duplicates when two machines sync the same account. Strava and Zwift receive the latest eligible current weight. Successful deliveries are recorded in the database; Zwift is recorded only after eufy-sync reads the profile again and verifies the saved weight.

Garmin, Eufy, and Zwift use unofficial APIs in this project. Strava uses its official API. Changes to any service can require an eufy-sync update.
