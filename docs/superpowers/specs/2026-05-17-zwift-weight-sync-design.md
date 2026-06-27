# Zwift Weight Sync - Design

**Date:** 2026-05-17
**Status:** approved, pending implementation plan
**Owner:** Elias

## Goal

Add Zwift as a third sync target in `eufy-sync`, so a new Eufy weight measurement updates the user's Zwift profile weight alongside Garmin Connect and Strava. Zwift uses weight for power-to-weight ratio in races, so keeping it accurate without manual entry is the value.

## Constraints and Risks

- Zwift has no public API for hobby developers. The Developer API is gated and `developers@zwift.com` access is not granted for personal projects.
- The path is community-reverse-engineered: Keycloak OAuth2 password grant for auth, `PUT /api/profiles/{id}` for the write. Both can change with any Zwift release.
- No maintained Python library implements the write. We are first to ship.
- Zwift's first-party Companion-app sync (Withings -> Zwift) has been reportedly broken for years per their forum, so users expect some flakiness in this space.

The feature is shipped opt-in with explicit "unofficial, may break" framing in the setup flow.

## Scope

In scope:
- Update Zwift profile weight from the latest Eufy measurement on each sync.
- Token-based auth with automatic refresh, falling back to a stored password if the refresh token dies.
- Inclusion in first-run wizard, `--setup-zwift` for retrofitting, `--reauth zwift` for token refresh.
- Status/health reporting in `--status` output and the sync summary line.

Out of scope (calling out so we do not drift):
- No FTP, height, or other profile fields. Weight only.
- No ride/workout pushes.
- No Companion-app handshake or Withings-style integration.
- No retroactive weight history. Zwift only stores current weight; backfill is meaningless for Zwift.

## Architecture

Mirrors the Strava client + Eufy password handling already in the codebase. One new module, parallel surface.

### New module: `eufy_sync/zwift_client.py`

Public surface:

- `class ZwiftClient`
  - `__init__(config: ZwiftConfig)`
  - `authenticate() -> None` - loads cached tokens from keychain, refreshes if expired, falls back to fresh password-grant login if no refresh token.
  - `update_weight(weight_kg: float) -> dict` - `PUT /api/profiles/me` with `{"weight": int(round(weight_kg * 1000))}` (grams). Returns the Zwift response JSON. 4xx -> `PermanentSyncError`; 5xx -> retried by `_retry`.
  - `token_status() -> dict` - same shape as `StravaClient.token_status` and `GarminAuth.token_status` so the existing summary/status formatters work without special-casing.
  - `close() -> None`

Internal helpers:
- `_fresh_login()` - `POST https://secure.zwift.com/auth/realms/zwift/tokens/access/codes` with `grant_type=password`, `client_id=Zwift_Mobile_Link`, `username`, `password`.
- `_refresh_access_token()` - same URL, `grant_type=refresh_token`.
- `_load_tokens()` / `_save_tokens(tokens)` - keychain first, file fallback, same pattern as Strava's helpers.

Token shape:
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1715948000
}
```

Keychain key: `token:zwift`. File fallback: `~/.garmin-sync/zwift_token.json`.

### Config additions: `eufy_sync/config.py`

- `@dataclass class ZwiftConfig: email: str; password: str`
- `UserConfig.zwift: ZwiftConfig | None = None`
- `load_config` resolves the password the same way it resolves Eufy/Garmin passwords (`_get_password(name, "zwift", email, yaml_password)`).
- The "user has no sync targets" guard updates to include Zwift: a user must have at least one of `garmin`, `strava`, `zwift`.

### Sync flow: `eufy_sync/sync.py`

Two behavioral differences from the Strava code path:

1. **One PUT per sync, not one per measurement.** Zwift's profile endpoint is heavier than Strava's `PUT /athlete`, and the user only cares about the final weight. So we find the newest unsynced measurement once and call `update_weight` once. Every measurement we considered gets a `record_sync(target="zwift")` entry so the duplicate check stays correct.

2. **Zwift failures are isolated.** Today, if Strava throws inside `sync_user`, Garmin sync also stops for that measurement. For Zwift, which we expect to be the flakiest target, we wrap the Zwift work in its own try/except so a Zwift outage cannot block Garmin/Strava. The CLI summary reports per-target failures.

Concretely, inside `sync_user`:

```python
# After Garmin/Strava processing, before returning counts:
if user.zwift:
    try:
        zwift = ZwiftClient(user.zwift)
        zwift.authenticate()
        # "Unsynced for Zwift" = measurement not yet recorded in state DB with target="zwift".
        unsynced = [m for m in measurements
                    if not state.is_synced(user.name, m.measurement_id, "zwift")]
        if unsynced and not dry_run:
            newest = unsynced[-1]  # measurements list is sorted chronologically ascending
            _retry(lambda: zwift.update_weight(newest.weight_kg), "Zwift update")
            for m in unsynced:
                state.record_sync(
                    user_name=user.name,
                    measurement_id=m.measurement_id,
                    measurement_timestamp=m.timestamp.isoformat(),
                    weight_kg=m.weight_kg,
                    synced_at=datetime.now(timezone.utc).isoformat(),
                    target="zwift",
                    response=None if m is not newest else json.dumps(result),
                )
            counts["zwift"] = 1
        zwift.close()
    except Exception as e:
        logger.exception("Zwift sync failed; continuing")
        zwift_error = str(e)  # surfaced via the new errors channel below
```

`sync_user` returns `(counts, errors)` where `errors: dict[str, str]` maps target name to a failure message for that target only. The CLI summary prints any per-target errors alongside the success counts, so a user whose Zwift target failed still sees "Synced 1 measurement to Garmin and Strava; Zwift failed: ...".

### CLI: `eufy_sync/cli.py`

- First-run wizard: after the Strava prompt, ask `Connect Zwift? [y/N]`. Show the "unofficial, may break" notice before prompting for credentials so the user opts in eyes-open.
- `--setup-zwift` (new flag): adds or updates Zwift on an existing install. Same shape as `--setup-strava`.
- `--reauth zwift` (extended): treats Zwift like the other two for force-reauth.
- `--update-password`: now also prompts for the Zwift password.
- `--status`: prints a Zwift section using the existing `token_status` shape.
- Sync summary one-liner: includes Zwift counts when the target is configured.

### Storage

- Keychain entries: `default:zwift` (password) and `token:zwift` (refresh+access tokens).
- File fallback (no keychain): tokens at `~/.garmin-sync/zwift_token.json`, password in config YAML.
- `_uninstall` extends to clear `default:zwift` and `token:zwift`.

### Error handling

- Bad password on first login -> `PermanentSyncError`, surfaces in CLI as "Zwift login failed; run --update-password".
- 4xx on `PUT /api/profiles/me` -> `PermanentSyncError`, surfaces as "Zwift update failed (HTTP 4xx)".
- 5xx and network errors -> normal `_retry` behavior.
- Schema drift (Zwift response shape changes) -> caught by the per-target try/except in `sync_user`, logged, Zwift target marked failed for this run, Garmin/Strava continue.

### Out-of-band: README and help text

- Add a new "Sync targets" section to README listing Garmin (FIT upload), Strava (current weight), Zwift (current weight, unofficial).
- Setup wizard prints a one-liner about Zwift being unofficial before prompting.

## Test Plan

New tests:
- `tests/test_zwift_client.py`
  - `token_status` returns `valid` / `refresh_needed` / `expired` / `no_session` in each branch.
  - `authenticate` with valid token sets the bearer header.
  - `authenticate` with expired access token triggers refresh.
  - `authenticate` with no tokens triggers fresh password-grant login.
  - `update_weight` sends grams (not kg).
  - `update_weight` 401 raises `PermanentSyncError`.
  - `update_weight` 500 raises a retryable error.

Updated tests:
- `tests/test_sync.py`
  - Three-target sync (Garmin + Strava + Zwift) makes exactly one Zwift PUT, regardless of how many measurements arrive.
  - A Zwift exception during sync does not prevent Garmin uploads from being recorded.
  - Zwift state-DB rows are written for every measurement considered.
- `tests/test_config.py`
  - Loading a config with `zwift:` section parses into `ZwiftConfig`.
  - User with only Zwift configured is accepted.

## Open Questions

None. All decisions resolved during brainstorming:

- Auth UX: terminal-prompted password into keychain, same as Eufy.
- Update cadence: one PUT per sync, newest measurement, all measurements recorded in state DB.
- Failure isolation: per-target try/except so Zwift cannot break Garmin/Strava.
- First-run inclusion: yes, Zwift is in the wizard.
- Out-of-scope items locked: weight only, no Companion-app handshake, no other profile fields.

## Success Criteria

- `eufy-sync` first-run wizard offers Zwift with an "unofficial" notice.
- A successful sync updates the user's profile weight on Zwift's website to match the latest Eufy reading.
- `--status` shows Zwift token health alongside Garmin/Strava.
- Zwift breaking does not block Garmin/Strava sync; the CLI tells the user which target failed.
- Test suite stays green and adds Zwift-specific coverage.
