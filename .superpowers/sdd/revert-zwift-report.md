# Zwift Revert Report

## Reason

Zwift silently ignores weight writes from API clients on accounts with a connected weight source (e.g. a paired Bluetooth scale or ANT+ device). The sync appeared to succeed but had no effect. Removed entirely.

## Files removed

- `eufy_sync/zwift_client.py` (git rm)
- `tests/test_zwift_client.py` (git rm)

## Files modified

### eufy_sync/config.py
- Removed `ZwiftConfig` dataclass
- Removed `zwift: ZwiftConfig | None = None` field from `UserConfig`
- Removed `zwift` parse block in `load_config`
- Removed `zwift=zwift` from `UserConfig(...)` construction
- Reverted "no sync targets" guard error message: no longer mentions zwift

### eufy_sync/sync.py
- Reverted return type from `tuple[dict[str, int], dict[str, str]]` to `dict[str, int]`
- Removed `errors` dict and `return counts, errors` -> `return counts`
- Removed Zwift `if user.zwift:` try/except block
- Removed `if user.zwift and not state.has_any_syncs(user.name, "zwift"): new_target = True` line
- Updated docstring

### eufy_sync/cli.py
- Reverted argparse `description` to "Sync Eufy smart scale data to Garmin Connect and Strava"
- Reverted `--reauth` help text (no longer mentions zwift)
- Removed `--setup-zwift` argument and dispatch block
- Removed `_setup_zwift` function
- Removed `do_zwift` variable and zwift reauth branch from `_reauth`
- Reverted both `sync_user` call sites from `counts, errors =` to `counts =`, removed `errors` loops and log args
- Removed zwift from `_update_password` (prompt, early-exit check, store/clear, "changed" list)
- Removed zwift sections from `_show_status` and `_print_summary`
- Removed `"zwift"` suffix from keychain loop in `_uninstall` and `delete_token("zwift")`
- Removed first-run wizard zwift prompt and related config-building
- Removed `zwift_password` parameter from `_store_passwords_in_keychain`
- Removed zwift from targets summary list in first-run

### tests/test_sync.py
- Removed three zwift tests: `test_zwift_gets_exactly_one_put_per_sync`, `test_zwift_failure_does_not_block_strava`, `test_zwift_new_install_triggers_backfill`
- Removed `from eufy_sync.config import ZwiftConfig`
- Reverted `counts, errors = sync_user(...)` to `counts = sync_user(...)` and dropped `assert errors == {}`
- Added `GarminConfig` to import (was being imported inside removed test)

### tests/test_config.py
- Removed `test_load_config_with_zwift_only`
- Removed `test_load_config_with_all_three_targets`

### tests/test_cli.py
- Reverted `_uninstall` test: removed `"zwift": {"email": "z@example.com"}` from config and removed `assert "elias:zwift" in deleted_accounts`
- Reverted `fake_sync_user` that returned `(dict, dict)` to return just the dict

### tests/test_summary.py
- Removed `has_zwift` parameter from `_mock_user`
- Removed `user.zwift` handling from `_mock_user`

### README.md
- Reverted intro line (removed ", and Zwift")
- Removed Zwift row from sync targets table
- Removed Zwift-caveat paragraph after the table
- Reverted "How it works" diagram and prose (back to Garmin + Strava)
- Removed `--setup-zwift` usage line
- Reverted `--reauth` usage line
- Reverted disclaimer ("Eufy and Garmin" only, plus Strava)
- Reverted "Why" paragraph
- Reverted Security section

## Final test count

128 passed in 0.40s

## grep -ri zwift eufy_sync/ tests/ README.md

(no output - clean)

## Uncertainties

None. All changes were straightforward reversions. The `GarminConfig` import in `test_sync.py` was already used by other tests; the zwift tests had been importing it locally inside the test function.
