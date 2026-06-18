# Eufy Profile Selection — Design

**Date:** 2026-06-18
**Status:** approved, pending implementation plan
**Owner:** Elias
**Fixes:** GitHub issue #1 (syncs every household member's weight)

## Goal

On a shared Eufy account, the tool currently syncs every profile's weigh-ins to Garmin and Strava, so a user gets their partner's weight written into their own permanent health record. This adds a way to tell the tool which profile is yours, and makes it refuse to sync when it cannot tell, instead of guessing.

## The bug, precisely

The Eufy `/device/data` endpoint is keyed on the account, so it returns records for every profile that has weighed in. Each record carries a `customer_id`. `EufyClient._parse_record` reads that id into `EufyMeasurement.customer_id` and uses it only to build the dedup key (`f"{customer_id}_{update_time}"`). Nothing filters by it. `sync_user` then iterates all measurements and uploads each one. There is no profile field in `EufyConfig` and no way to select a profile from the CLI.

## Scope

In scope:
- An optional `customer_id` on the Eufy config that filters sync to one profile.
- Profile discovery so a user can identify themselves without knowing the opaque id.
- A safe stop when several profiles exist and none is selected.
- Selection during first-run setup, and a `--select-profile` command for existing installs.

Out of scope:
- Cleaning up weigh-ins already written to Garmin or Strava before the fix. That history cannot be un-written safely. The fix stops the bleeding; the issue note will say so.
- Multi-profile-to-multi-target routing. `load_config` already rejects more than one user per install, so a single account maps to a single set of targets.

## Behavior

`customer_id` is the only stable per-profile identifier the API exposes, so it is what gets stored. The three cases at sync time:

1. **`customer_id` is configured.** Only that profile's measurements sync. Everything else is dropped before upload. This is the fix.
2. **No `customer_id`, and the account has one profile.** Syncs it. This is the existing single-person setup, and its behavior does not change.
3. **No `customer_id`, and the account has more than one profile.** Syncs nothing. The sync stops, prints the detected profiles, and tells the user to run `eufy-sync --select-profile`. This is the safe stop.

The decision in case 2 versus 3 must be based on every profile that has ever appeared on the account, not only the profiles with a measurement inside the current sync window. Otherwise a sync where only one household member weighed in recently would look single-profile and sync the wrong person. The implementation therefore determines the profile set from a full-history read when no `customer_id` is configured.

## Architecture

### Config: `eufy_sync/config.py`

- `EufyConfig` gains `customer_id: str | None = None`.
- `load_config` reads it from the `eufy` section: `customer_id=u["eufy"].get("customer_id")`. It is plain configuration, not a secret, so it stays in the YAML file alongside the email (no keychain involvement).

### Client: `eufy_sync/eufy_client.py`

A new dataclass describes a profile for the picker:

```python
@dataclass
class EufyProfile:
    customer_id: str
    last_measured: datetime
    last_weight_kg: float
    name: str | None = None
```

`name` is populated if the raw record exposes a human-readable label. Whether it does is verified against real account data during implementation; the reliable display is `last_weight_kg` + `last_measured` + the last few characters of `customer_id`, which is enough for a user to recognize their own weigh-in.

New `list_profiles() -> list[EufyProfile]`:
- Reads the full history (`/device/data` with no `after`), groups parsed records by `customer_id`, and returns one `EufyProfile` per id holding the most recent weigh-in, sorted newest first. Used by the setup wizard and `--select-profile`.

Changed `fetch_measurements(after_timestamp)`:
- If `self.config.customer_id` is set: fetch the requested window, parse, return only measurements whose `customer_id` matches.
- If it is not set: read full history once, derive the profile set from it. If more than one profile is present, raise `AmbiguousProfileError` carrying the profile list. If one or zero profiles are present, return the parsed measurements as before. The state DB dedup still ensures only new measurements sync, so reading full history here does not cause re-uploads.

New exception `AmbiguousProfileError(Exception)`:
- Carries `profiles: list[EufyProfile]` so the CLI can render the picker guidance.
- Must not be retried. `sync.py::_is_permanent` is extended to treat it as permanent so `_retry` surfaces it immediately rather than spinning three times.

### Sync: `eufy_sync/sync.py`

- `_is_permanent` recognizes `AmbiguousProfileError`.
- No change to the `sync_user` return shape. The exception propagates out of `sync_user` (the `finally` still closes clients) and is handled by the CLI loop. Keeping the signature untouched avoids colliding with the later Zwift change that reworks it into `(counts, errors)`.

### CLI: `eufy_sync/cli.py`

- **First-run wizard:** after authenticating Eufy and before the first sync, call `list_profiles()`. If more than one profile is found, run the selection prompt and write the chosen `customer_id` into the config before syncing. A single-profile account proceeds with no prompt.
- **`--select-profile` (new flag):** loads config, authenticates Eufy, calls `list_profiles()`, prints a numbered list, prompts for a choice, and writes `customer_id` into `users[0]["eufy"]` via the existing `_write_config` helper. If only one profile exists, it says so and stores that id anyway so future syncs are unambiguous. Mirrors the shape of the existing `--setup-strava` flow (operates on `config["users"][0]`).
- **Sync loop:** wrap each user's `sync_user` call so an `AmbiguousProfileError` for one user prints the detected profiles plus the `--select-profile` instruction and continues, rather than aborting with a traceback.

### Selection prompt

Shared routine used by both the wizard and `--select-profile`:
- Prints each profile as a numbered line: most recent weight in kg and lb, the date, and a name if available.
- Reads a number, validates it, and returns the chosen `customer_id`.

## Error handling

- Configured `customer_id` that matches no current records: zero measurements, which flows through the existing "found 0 measurements" path. A clear log line notes that the selected profile had nothing new.
- `AmbiguousProfileError`: caught in the CLI loop, rendered as guidance, non-fatal for any other configured behavior.
- A user who selects the wrong profile re-runs `--select-profile` to change it.

## Test plan

New and updated tests:
- `tests/test_config.py`: an `eufy` section with `customer_id` parses into `EufyConfig.customer_id`; absence leaves it `None`.
- `tests/test_eufy_client.py`:
  - `fetch_measurements` with a configured `customer_id` returns only matching measurements.
  - `fetch_measurements` with no config and a single account profile returns all measurements (back-compat).
  - `fetch_measurements` with no config and multiple account profiles raises `AmbiguousProfileError` carrying the profiles.
  - `list_profiles` returns one entry per `customer_id` with the most recent weight and date, newest first.
- `tests/test_sync.py`: `AmbiguousProfileError` is treated as permanent (not retried) and propagates out of `sync_user`.
- `tests/test_cli.py`: `--select-profile` lists profiles and writes the chosen `customer_id` to the config (mocked input and `list_profiles`).

## Success criteria

- A configured profile syncs only that person's weigh-ins.
- A fresh single-profile install behaves exactly as before.
- A shared account with no profile selected stops, shows the profiles, and points the user to `--select-profile`, with nothing written to Garmin or Strava.
- `--select-profile` lets an existing install choose without editing config by hand.
- The test suite stays green and adds the coverage above.
