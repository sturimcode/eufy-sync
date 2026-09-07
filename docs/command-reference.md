# Command reference

Running `eufy-sync` with no options syncs new measurements to every configured target.

## Check and preview

```bash
eufy-sync --status             # last sync and token health
eufy-sync --history            # the last 14 sync-history entries
eufy-sync --history 30         # choose how many entries to show
eufy-sync --dry-run            # preview without uploading
eufy-sync --doctor             # check the whole setup and print fixes
eufy-sync --verbose            # show detailed logs
eufy-sync --version            # show the installed version
```

## Accounts, targets, and profiles

```bash
eufy-sync --setup-strava       # add or reconnect Strava
eufy-sync --setup-zwift        # add experimental Zwift weight sync
eufy-sync --disconnect-zwift   # remove Zwift and its saved login
eufy-sync --target garmin      # sync one target (garmin, strava, or zwift)
eufy-sync --select-profile     # choose a profile on a shared Eufy account
eufy-sync --reauth             # log back into every configured target
eufy-sync --reauth garmin      # log back into one target (garmin, strava, or zwift)
eufy-sync --update-password    # change stored Eufy, Garmin, or Zwift passwords
```

On a fresh installation, run `eufy-sync` and choose any combination of Garmin, Strava, and experimental Zwift. The separate `--setup-zwift` command also supports a fresh Zwift-only installation, but does not offer automatic scheduling; run `eufy-sync --install-agent` afterward on macOS or Windows if needed.

## Automation and storage

```bash
eufy-sync --install-agent      # enable four-hour syncs on macOS or Windows
eufy-sync --uninstall-agent    # disable automatic sync
eufy-sync --headless           # never prompt during a scheduled run
eufy-sync --use-file-store     # move credentials to a local file and avoid keychain prompts
eufy-sync --use-keychain       # move credentials back to the system keychain
```

Linux scheduling is covered in [Headless Linux](headless-linux.md). `--headless` tries to renew or restore expired sessions with stored credentials, but a service can still require interactive recovery later.

## History recovery

```bash
eufy-sync --backfill-days 30   # sync eligible measurements not recorded as delivered
eufy-sync --repair-days 30     # resend Garmin history even when recorded as delivered
```

`--backfill-days` sends measurements in the chosen window only when the local database says they have not reached that target. It is useful after adding a target or increasing the default seven-day lookback.

`--repair-days` is for Garmin history that was deleted from Garmin Connect or filed under the wrong date by eufy-sync versions before 1.9.0. It resends Garmin measurements in the window even when the local database says they were delivered. It still leaves alone dates eufy-sync never uploaded when Garmin already holds data from another source.

Delete wrong-dated entries in Garmin Connect before repair. eufy-sync cannot delete them, so otherwise they remain beside the corrected entries. Strava and Zwift store current weight rather than history; in repair mode they receive only the newest eligible current weight.

`--repair-days` and `--backfill-days` cannot be used together. Both can be limited to one target with `--target` and previewed with `--dry-run`.

## Maintenance and paths

```bash
eufy-sync --update             # update to the latest release
eufy-sync --uninstall          # remove saved data, credentials, and automatic sync
eufy-sync --config PATH        # use another configuration file
eufy-sync --db PATH            # use another sync database
```

The default config is `~/.garmin-sync/config.yaml`; the default database is `~/.garmin-sync/state.db`. `eufy-sync --help` is the source of truth for the options supported by your installed version.
