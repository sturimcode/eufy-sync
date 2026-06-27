# Inline Profile Picker and Quieter Garmin 429 Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve a multi-profile Eufy sync in place during an interactive run, and stop a successful Garmin login from printing alarming 429 warnings.

**Architecture:** Two independent changes in `eufy_sync/cli.py`. Part 1 catches `AmbiguousProfileError` in the sync loop and, when a human is present, prompts with the existing picker, saves the choice, and re-runs the sync in the same process. Part 2 extracts logging setup into a helper that quiets the chatty `garminconnect` library logger on normal runs. Both are covered by tests in `tests/test_cli.py`.

**Tech Stack:** Python 3.12, pytest, python-garminconnect, PyYAML.

## Global Constraints

- No em-dashes in any copy, UI text, or documentation. Copy this verbatim into any user-facing string.
- One user per installation. `load_config` raises on more than one user, so the selected profile always belongs to `config.users[0]`.
- Run tests with `.venv/bin/python -m pytest`. Baseline before this plan: 110 passed.
- Follow the codebase habit of local imports inside functions where the surrounding code already does so.
- TDD: write the failing test first, watch it fail, implement the minimum, watch it pass, commit.

## File Structure

- `eufy_sync/cli.py` (modify): add `_configure_logging(verbose)` and `_save_customer_id(config_path, customer_id)` helpers; refactor `_select_profile` to use the new save helper; replace the inline logging block and add a logging call to the reauth dispatch; rewrite the `except AmbiguousProfileError` block in the sync loop; add a dedicated notification branch for the multi-profile failure.
- `tests/test_cli.py` (modify): add `import pytest` and four new tests.

---

### Task 1: Quiet the Garmin 429 retry noise

**Files:**
- Modify: `eufy_sync/cli.py` (new `_configure_logging`; replace inline logging block at lines 951-957; add a call in the reauth dispatch near line 908)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `_configure_logging(verbose: bool) -> None`. Sets the root log level (DEBUG when verbose, WARNING otherwise), quiets httpx on normal runs, and sets the `garminconnect` logger to ERROR on normal runs / DEBUG when verbose.

- [ ] **Step 1: Write the failing tests**

Add to the end of `tests/test_cli.py`:

```python
def test_configure_logging_quiets_garminconnect_when_not_verbose():
    import logging
    from eufy_sync.cli import _configure_logging

    logging.getLogger("garminconnect").setLevel(logging.NOTSET)
    _configure_logging(verbose=False)
    assert logging.getLogger("garminconnect").level == logging.ERROR


def test_configure_logging_keeps_garminconnect_detail_when_verbose():
    import logging
    from eufy_sync.cli import _configure_logging

    logging.getLogger("garminconnect").setLevel(logging.ERROR)
    _configure_logging(verbose=True)
    assert logging.getLogger("garminconnect").level == logging.DEBUG
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_configure_logging_quiets_garminconnect_when_not_verbose tests/test_cli.py::test_configure_logging_keeps_garminconnect_detail_when_verbose -v`
Expected: FAIL with `ImportError` / `cannot import name '_configure_logging'`.

- [ ] **Step 3: Implement `_configure_logging`**

In `eufy_sync/cli.py`, add this helper near the other module-level helpers (for example just above `def _format_profile`):

```python
def _configure_logging(verbose: bool) -> None:
    """Set up logging. On normal runs, quiet chatty library loggers; under
    --verbose, show full detail. The garminconnect library logs a warning for
    each login strategy that hits a 429 before a later strategy succeeds, which
    looks alarming on an otherwise successful login."""
    import logging
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format=fmt)
        logging.getLogger("garminconnect").setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING, format=fmt)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("garminconnect").setLevel(logging.ERROR)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_configure_logging_quiets_garminconnect_when_not_verbose tests/test_cli.py::test_configure_logging_keeps_garminconnect_detail_when_verbose -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Wire the helper into the sync path**

In `eufy_sync/cli.py`, replace the inline logging block in the sync path. The current code is:

```python
    log_level = "DEBUG" if args.verbose else "WARNING"
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    logger = logging.getLogger("eufy_sync")
```

Replace it with:

```python
    _configure_logging(args.verbose)
    logger = logging.getLogger("eufy_sync")
```

Leave the `import logging` line above it in place (it is still used for `getLogger`).

- [ ] **Step 6: Wire the helper into the reauth dispatch**

In `eufy_sync/cli.py`, the reauth dispatch currently reads:

```python
    # Handle reauth
    if args.reauth is not None:
        target = None if args.reauth == "all" else args.reauth
        _reauth(config_path, force=True, target=target)
        return
```

Change it to call the logging helper first so the reauth login is quiet too:

```python
    # Handle reauth
    if args.reauth is not None:
        _configure_logging(args.verbose)
        target = None if args.reauth == "all" else args.reauth
        _reauth(config_path, force=True, target=target)
        return
```

- [ ] **Step 7: Run the full suite to confirm nothing regressed**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (112 passed: 110 baseline + 2 new).

- [ ] **Step 8: Commit**

```bash
git add eufy_sync/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
Quiet garminconnect 429 retry noise on successful login

Extract logging setup into _configure_logging and raise the garminconnect
logger to ERROR on normal runs. The library logs a warning per login strategy
that hits a 429 before a later strategy succeeds; those attempts do not change
the outcome and only show up because the run sets the log floor to WARNING.
--verbose still shows full detail.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Extract `_save_customer_id` and reuse it in `_select_profile`

**Files:**
- Modify: `eufy_sync/cli.py` (new `_save_customer_id`; refactor `_select_profile` lines 296-333)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `_save_customer_id(config_path: Path, customer_id: str) -> None`. Reads the YAML config, sets `users[0]["eufy"]["customer_id"]`, and writes it back with `_write_config`.
- Consumes: `_write_config` (existing).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli.py`:

```python
def test_save_customer_id_writes_into_config(tmp_path: Path):
    from eufy_sync.cli import _save_customer_id, _write_config

    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })

    _save_customer_id(cfg_path, "cid-xyz")

    written = yaml.safe_load(cfg_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-xyz"
    # Existing fields are left intact.
    assert written["users"][0]["eufy"]["email"] == "e@example.com"
    assert written["users"][0]["name"] == "default"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_save_customer_id_writes_into_config -v`
Expected: FAIL with `cannot import name '_save_customer_id'`.

- [ ] **Step 3: Implement `_save_customer_id`**

In `eufy_sync/cli.py`, add the helper just above `def _select_profile`:

```python
def _save_customer_id(config_path: Path, customer_id: str) -> None:
    """Persist the chosen Eufy customer_id into the config file (single user)."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    user = config["users"][0]
    user.setdefault("eufy", {})
    user["eufy"]["customer_id"] = customer_id
    _write_config(config_path, config)
```

- [ ] **Step 4: Refactor `_select_profile` to use the helper**

In `eufy_sync/cli.py`, the tail of `_select_profile` currently reads:

```python
    with open(config_path) as f:
        config = yaml.safe_load(f)

    from eufy_sync.config import load_config
    from eufy_sync.eufy_client import EufyClient

    cfg = load_config(config_path)
    eufy = EufyClient(cfg.users[0].eufy)
    try:
        eufy.authenticate()
        profiles = eufy.list_profiles()
    except Exception as e:
        print(f"Could not reach Eufy: {e}")
        sys.exit(1)
    finally:
        eufy.close()

    if not profiles:
        print("No profiles found yet. Weigh in and open the Eufy app, then try again.")
        return

    user = config["users"][0]
    user.setdefault("eufy", {})
    if len(profiles) == 1:
        user["eufy"]["customer_id"] = profiles[0].customer_id
        _write_config(config_path, config)
        print("Only one profile found on this account; saved it as yours.")
        return

    user["eufy"]["customer_id"] = _prompt_profile_choice(profiles)
    _write_config(config_path, config)
    print("Saved. Future syncs will use only your profile.")
```

Replace it with (the raw YAML read at the top and the in-line config mutation are no longer needed):

```python
    from eufy_sync.config import load_config
    from eufy_sync.eufy_client import EufyClient

    cfg = load_config(config_path)
    eufy = EufyClient(cfg.users[0].eufy)
    try:
        eufy.authenticate()
        profiles = eufy.list_profiles()
    except Exception as e:
        print(f"Could not reach Eufy: {e}")
        sys.exit(1)
    finally:
        eufy.close()

    if not profiles:
        print("No profiles found yet. Weigh in and open the Eufy app, then try again.")
        return

    if len(profiles) == 1:
        _save_customer_id(config_path, profiles[0].customer_id)
        print("Only one profile found on this account; saved it as yours.")
        return

    _save_customer_id(config_path, _prompt_profile_choice(profiles))
    print("Saved. Future syncs will use only your profile.")
```

- [ ] **Step 5: Run the new test and the existing select-profile test**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_save_customer_id_writes_into_config tests/test_cli.py::test_select_profile_writes_chosen_customer_id -v`
Expected: PASS (2 passed). The existing `test_select_profile_writes_chosen_customer_id` still passes because the written `customer_id` is unchanged.

- [ ] **Step 6: Commit**

```bash
git add eufy_sync/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
Extract _save_customer_id and reuse it in _select_profile

Single place that writes the chosen Eufy customer_id into the config, so the
upcoming inline picker and the existing --select-profile flow share one writer.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Inline profile picker in the sync loop

**Files:**
- Modify: `eufy_sync/cli.py` (rewrite the `except AmbiguousProfileError` block in the sync loop around lines 977-984; add a `multiple_profiles` branch in the failure-notification block around lines 991-1001)
- Test: `tests/test_cli.py` (add `import pytest`; two integration tests)

**Interfaces:**
- Consumes: `_prompt_profile_choice` (existing), `_save_customer_id` (Task 2), `_format_profile` (existing), `sync_user` (from `eufy_sync.sync`), `AmbiguousProfileError` (from `eufy_sync.eufy_client`).

- [ ] **Step 1: Add the pytest import**

At the top of `tests/test_cli.py`, add `import pytest` to the imports (after `import os`):

```python
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_cli.py`:

```python
def _ambiguous_profiles():
    from datetime import datetime, timezone
    from eufy_sync.eufy_client import EufyProfile
    return [
        EufyProfile("cid-human", datetime(2026, 6, 27, tzinfo=timezone.utc), 88.0),
        EufyProfile("cid-pet", datetime(2026, 4, 4, tzinfo=timezone.utc), 4.5),
    ]


def _write_synced_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    return config_path


@patch("eufy_sync.cli._print_summary")
@patch("eufy_sync.cli._notify")
@patch("eufy_sync.cli._check_for_updates")
@patch("eufy_sync.cli._show_upgrade_notice")
@patch("eufy_sync.cli._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.cli.sys.stdin")
def test_interactive_ambiguous_profile_resolves_and_syncs(
    mock_stdin, _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    from eufy_sync.cli import main
    from eufy_sync.eufy_client import AmbiguousProfileError

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    profiles = _ambiguous_profiles()
    seen_customer_ids = []

    def fake_sync_user(user, state, **kwargs):
        seen_customer_ids.append(user.eufy.customer_id)
        if len(seen_customer_ids) == 1:
            raise AmbiguousProfileError(profiles)
        return {"garmin": 1}

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path)]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         patch("builtins.input", return_value="1"), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    # The chosen (human) profile was persisted to config.
    written = yaml.safe_load(config_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-human"
    # The sync was retried in-process with that customer_id set in memory.
    assert seen_customer_ids == [None, "cid-human"]


@patch("eufy_sync.cli._print_summary")
@patch("eufy_sync.cli._notify")
@patch("eufy_sync.cli._check_for_updates")
@patch("eufy_sync.cli._show_upgrade_notice")
@patch("eufy_sync.cli._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.cli.sys.stdin")
def test_noninteractive_ambiguous_profile_bails(
    mock_stdin, _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path, capsys
):
    from eufy_sync.cli import main
    from eufy_sync.eufy_client import AmbiguousProfileError

    mock_stdin.isatty.return_value = False  # no human present
    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"

    profiles = _ambiguous_profiles()

    def fake_sync_user(user, state, **kwargs):
        raise AmbiguousProfileError(profiles)

    def boom_input(*a, **k):
        raise AssertionError("input() must not be called with no TTY present")

    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path)]
    with patch("eufy_sync.sync.sync_user", side_effect=fake_sync_user), \
         patch("sys.argv", argv), \
         patch("builtins.input", side_effect=boom_input), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "eufy-sync --select-profile" in out
    _notify.assert_any_call(
        "eufy-sync: choose your profile", "Run: eufy-sync --select-profile"
    )
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_interactive_ambiguous_profile_resolves_and_syncs tests/test_cli.py::test_noninteractive_ambiguous_profile_bails -v`
Expected: FAIL. The interactive test fails because `seen_customer_ids` ends as `[None]` (no retry yet) and no customer_id is written. The non-interactive test fails on the missing `_notify` call for the dedicated message.

- [ ] **Step 4: Rewrite the `except AmbiguousProfileError` block**

In `eufy_sync/cli.py`, the sync loop currently has:

```python
            except AmbiguousProfileError as e:
                print("")
                print("Multiple profiles were found on this Eufy account:")
                for i, p in enumerate(e.profiles, 1):
                    print(_format_profile(p, i))
                print("")
                print("Nothing was synced. Choose your profile with: eufy-sync --select-profile")
                failures.append((user.name, "multiple Eufy profiles; run eufy-sync --select-profile"))
```

Replace it with:

```python
            except AmbiguousProfileError as e:
                interactive = not args.headless and sys.stdin.isatty()
                if interactive:
                    # _prompt_profile_choice prints the list and asks for a pick.
                    customer_id = _prompt_profile_choice(e.profiles)
                    _save_customer_id(config_path, customer_id)
                    user.eufy.customer_id = customer_id
                    print("Saved. Syncing your profile now...")
                    try:
                        counts = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                        for target_name, count in counts.items():
                            total_counts[target_name] = total_counts.get(target_name, 0) + count
                        logger.info("User %s: synced %s", user.name, counts)
                    except Exception as retry_error:
                        logger.exception("Failed to sync user %s after profile selection", user.name)
                        failures.append((user.name, str(retry_error)))
                else:
                    print("")
                    print("Multiple profiles were found on this Eufy account:")
                    for i, p in enumerate(e.profiles, 1):
                        print(_format_profile(p, i))
                    print("")
                    print("Nothing was synced. Choose your profile with: eufy-sync --select-profile")
                    failures.append((user.name, "multiple Eufy profiles; run eufy-sync --select-profile"))
```

- [ ] **Step 5: Add the dedicated notification branch**

In `eufy_sync/cli.py`, the failure-notification block currently reads:

```python
        if failures:
            reauth_needed = any("re-authenticate" in err for _, err in failures)
            eufy_password = any("changed your Eufy password" in err for _, err in failures)
            if reauth_needed:
                _notify("eufy-sync: re-login needed", "Run: eufy-sync --reauth")
            elif eufy_password:
                _notify("eufy-sync: Eufy login failed", "Run: eufy-sync --update-password")
            else:
                fail_msg = "; ".join(f"{name}: {err[:80]}" for name, err in failures)
                _notify("eufy-sync failed", fail_msg)
            logger.error("Sync failed for: %s", "; ".join(f"{n}: {e[:80]}" for n, e in failures))
```

Add a `multiple_profiles` branch:

```python
        if failures:
            reauth_needed = any("re-authenticate" in err for _, err in failures)
            eufy_password = any("changed your Eufy password" in err for _, err in failures)
            multiple_profiles = any("multiple Eufy profiles" in err for _, err in failures)
            if reauth_needed:
                _notify("eufy-sync: re-login needed", "Run: eufy-sync --reauth")
            elif eufy_password:
                _notify("eufy-sync: Eufy login failed", "Run: eufy-sync --update-password")
            elif multiple_profiles:
                _notify("eufy-sync: choose your profile", "Run: eufy-sync --select-profile")
            else:
                fail_msg = "; ".join(f"{name}: {err[:80]}" for name, err in failures)
                _notify("eufy-sync failed", fail_msg)
            logger.error("Sync failed for: %s", "; ".join(f"{n}: {e[:80]}" for n, e in failures))
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_interactive_ambiguous_profile_resolves_and_syncs tests/test_cli.py::test_noninteractive_ambiguous_profile_bails -v`
Expected: PASS (2 passed).

- [ ] **Step 7: Commit**

```bash
git add eufy_sync/cli.py tests/test_cli.py
git commit -m "$(cat <<'EOF'
Resolve multi-profile sync inline instead of dead-ending

When a sync hits multiple Eufy profiles and a human is present (a TTY and not
--headless), prompt with the existing picker, save the choice, and finish the
sync in the same run. Headless and non-TTY runs keep the safe bail and now fire
a dedicated "choose your profile" notification.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire test suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (114 passed: 110 baseline + 4 new).

- [ ] **Step 2: Confirm the working tree is clean and on the branch**

Run: `git status --short && git branch --show-current`
Expected: no uncommitted source changes (an untracked `uv.lock` is fine); branch `inline-profile-picker`.

---

## Self-Review

**Spec coverage:**
- Part 1 inline picker → Task 3. Interactivity gate `not args.headless and sys.stdin.isatty()` → Task 3 Step 4. Persist choice → Task 2 + Task 3. Retry in same run → Task 3 Step 4. Headless bail + dedicated notification → Task 3 Steps 4 and 5.
- Part 2 quiet 429 → Task 1. `_configure_logging` extraction → Task 1 Steps 3, 5, 6.
- Single-user invariant → relied on in `_save_customer_id` (Task 2) and the loop.
- Tests 1-4 from the spec → Task 2 Step 1, Task 3 Step 2 (two tests), Task 1 Step 1 (two tests).

**Placeholder scan:** none. Every code and command step is concrete.

**Type consistency:** `_configure_logging(verbose: bool)`, `_save_customer_id(config_path: Path, customer_id: str)`, and `_prompt_profile_choice(profiles) -> str` are used with the same names and signatures across tasks. `sync_user(user, state, backfill_days=, headless=, dry_run=)` matches the call in both the original loop and the inline retry.
