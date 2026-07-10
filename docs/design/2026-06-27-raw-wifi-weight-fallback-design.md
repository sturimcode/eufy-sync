# Raw Wi-Fi weight fallback design

Date: 2026-06-27

## Problem

Eufy's cloud only exposes a weigh-in through the normal endpoints after the phone
app has processed it. On a headless or scheduled setup, a run that fires before
the app is opened gets nothing, so the weight never reaches Garmin or Strava until
someone opens the app by hand. This breaks the tool's core promise of headless,
zero-touch syncing. It is the substance of issue #2, where the reporter (gwgr)
found a workaround we never implemented.

The workaround: a per-device raw Wi-Fi endpoint returns the new weight before the
app processes it. A read-only probe against a live account on 2026-06-27 confirmed
the endpoint is real and reachable (`res_code: 1`), and that records live under a
`list` field rather than the `data` field the normal endpoints use. The endpoint
returned empty in the probe because that account was already synced, so a populated
raw record was not captured. That single unknown (the raw record's exact fields,
and whether it carries a `customer_id`) is resolved by the live test below.

## Goals

- When the normal pull returns no new measurement in the window, recover the
  weight from the raw Wi-Fi endpoint so a headless run still syncs.
- Never sync a wrong or implausible weight, and never sync another profile's weight.
- Reuse the existing parser, transform, profile filter, and dedupe. Add no config.

## Non-goals

- No retroactive enrichment. If the app is opened later and the enriched record
  appears, that date stays weight-only on Garmin. Enrich-later is a possible v2.
- No change to the normal path when it returns data.
- No new config toggle. The fallback is automatic and safe.

## Design

### Trigger

Inside `EufyClient.fetch_measurements`, after the existing normal path produces the
windowed, profile-filtered list, fall back only when that list is empty:

```
measurements = <existing normal path>
if measurements:
    return measurements
return self._fetch_raw_measurements(after_timestamp)
```

The existing `AmbiguousProfileError` branch (multiple profiles, none selected) still
runs first on the normal path, so the fallback is only reached when a profile is
configured or the account has a single profile.

### Three new EufyClient methods

- `_list_device_ids() -> list[str]`: `GET /device/v2` with the `Token`/`Uid`
  headers, return `[d["id"] for d in body.get("devices", []) if d.get("id")]`.
  Return `[]` on `res_code != 1`.
- `_get_raw_records(device_id, after_timestamp) -> list[dict]`:
  `GET /device/wifi_scale/raw_data/{device_id}` with `after` as a string param when
  set. Return `body.get("list") or []`. Return `[]` on a non-200 status or
  `res_code != 1`. The `list` field can be JSON `null`, hence `or []`.
- `_fetch_raw_measurements(after_timestamp) -> list[EufyMeasurement]`: for each
  device id, collect raw records, parse them with the existing `_parse_all`, then
  apply the same `customer_id` filter and `after` window filter the normal path
  uses. Log how many measurements were recovered. When raw records were parsed but
  all dropped by the profile filter, log that the raw records carried no matching
  profile id, so the live test is diagnostic.

### Why this reuses the existing pipeline

- Weight-only records need no new parsing. `_parse_record` reads
  `scale_data.weight` (decigrams), and `transform` clamps a zero body-fat, muscle,
  or water value to `None`, so a raw record with zeroed body composition becomes a
  clean weight-only upload.
- A unit mismatch is self-protecting. `transform` rejects any weight outside
  22.7 to 181.4 kg, so a misread raw weight is dropped, never synced wrong.
- Profile attribution reuses the normal filter `m.customer_id == config.customer_id`.
  If raw records carry a `customer_id`, the selected profile is honored. If they do
  not, that filter drops them for a profile-configured account (the pet can never
  slip through) while a single-profile account with no selection keeps them.

### Dedupe

No new logic. If the enriched record later arrives with the same id, `is_synced`
skips it. If it arrives with a different id but the same date, the Garmin
`has_weight_on_date` check skips it. Strava re-applies the same current weight,
which is harmless.

### Auth

The fallback runs only after the normal `_get_records` call has already
authenticated and, if needed, refreshed the token, so the fallback GETs use a valid
token. On any auth or server error they return `[]` and the run degrades to today's
behavior.

## Testing

Unit tests with mocked HTTP responses (the real endpoint cannot be exercised
because it is empty when an account is synced):

1. `_list_device_ids` parses `devices[].id` from a `/device/v2` body, and returns
   `[]` on `res_code != 1`.
2. `_get_raw_records` returns the `list` array, returns `[]` when `list` is `null`,
   and returns `[]` on a 500 or `res_code != 1`.
3. `fetch_measurements` falls back to the raw path only when the normal path is
   empty, and recovers a weight-only measurement from a mocked raw record.
4. `fetch_measurements` does not call the raw endpoint when the normal path returns
   data.
5. The profile filter applies to raw records: a raw record whose `customer_id`
   matches the configured profile is kept, a mismatch is dropped.
6. The fallback degrades to `[]` when `/device/v2` errors.

## Live test (also the raw-record capture)

Weigh in, do not open the Eufy app, then run `eufy-sync --dry-run`. If it reports
the new weight, the raw path works and raw records carry a usable profile id. If it
reports nothing, raw records lack a `customer_id`, and the follow-up is to relax the
filter for the single-device case. Either outcome is safe.

## Files touched

- `eufy_sync/eufy_client.py`: the trigger in `fetch_measurements` and the three new
  methods.
- `tests/test_eufy_client.py`: the six tests above.
