# Eufy Profile Selection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop syncing every household member's weigh-in by letting the user select which Eufy profile is theirs, and refusing to sync when the profile is ambiguous.

**Architecture:** Add an optional `customer_id` to `EufyConfig`. `EufyClient` learns to list the profiles on an account and to filter `fetch_measurements` to the selected one, raising a non-retryable `AmbiguousProfileError` when several profiles exist and none is chosen. The CLI gains a `--select-profile` command and a wizard prompt that share one picker, and the sync loop renders the picker when it hits the ambiguous case.

**Tech Stack:** Python 3.12+, httpx, pytest. Same surface as the rest of `eufy-sync`.

**Source spec:** `docs/superpowers/specs/2026-06-18-eufy-profile-selection-design.md`

---

## File Structure

**Modify:**
- `eufy_sync/config.py` — add `EufyConfig.customer_id`, parse it in `load_config`
- `eufy_sync/eufy_client.py` — add `EufyProfile`, `AmbiguousProfileError`, `_get_records`, `_parse_all`, `_profiles_from`, `list_profiles`; rework `fetch_measurements` to filter / detect ambiguity
- `eufy_sync/sync.py` — teach `_is_permanent` to treat `AmbiguousProfileError` as non-retryable
- `eufy_sync/cli.py` — add `_format_profile`, `_prompt_profile_choice`, `_select_profile`, the `--select-profile` flag and dispatch, wizard profile detection, and the sync-loop ambiguous handler
- `README.md` — document `--select-profile` and the shared-account behavior
- `tests/test_config.py`, `tests/test_eufy_client.py`, `tests/test_sync.py`, `tests/test_cli.py` — coverage

---

## Task 1: Add customer_id to EufyConfig

**Files:**
- Modify: `eufy_sync/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_parses_customer_id(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw", "customer_id": "cust-42"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id == "cust-42"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_customer_id_defaults_none(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].eufy.customer_id is None
```

- [ ] **Step 2: Run, confirm fail**

Run: `.venv/bin/python -m pytest tests/test_config.py::test_load_config_parses_customer_id -xvs`
Expected: FAIL with `TypeError: __init__() got an unexpected keyword argument 'customer_id'` (after Step 3 wiring) or `AssertionError`/`AttributeError` before it.

- [ ] **Step 3: Add the field and parse it**

In `eufy_sync/config.py`, change the `EufyConfig` dataclass:

```python
@dataclass
class EufyConfig:
    email: str
    password: str
    customer_id: str | None = None
```

In `load_config`, change the `EufyConfig(...)` construction inside the `users.append(...)` call to pass `customer_id`:

```python
            eufy=EufyConfig(
                email=u["eufy"]["email"],
                password=_get_password(name, "eufy", u["eufy"]["email"], u["eufy"].get("password")),
                customer_id=u["eufy"].get("customer_id"),
            ),
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_config.py -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/config.py tests/test_config.py
git commit -m "feat(config): add optional eufy.customer_id for profile selection"
```

---

## Task 2: EufyProfile, AmbiguousProfileError, and profile listing

Split the network read out of `fetch_measurements` so profile detection and measurement fetching share one path, and add the listing helper.

**Files:**
- Modify: `eufy_sync/eufy_client.py`
- Test: `tests/test_eufy_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eufy_client.py`:

```python
from unittest.mock import patch

from eufy_sync.config import EufyConfig
from eufy_sync.eufy_client import AmbiguousProfileError, EufyProfile


def _client(customer_id=None):
    c = EufyClient.__new__(EufyClient)
    c.config = EufyConfig(email="e@example.com", password="pw", customer_id=customer_id)
    return c


def _record(customer_id, weight_dg, update_time):
    return {
        "customer_id": customer_id,
        "device_id": "d",
        "update_time": update_time,
        "scale_data": {"weight": weight_dg},
    }


def test_list_profiles_groups_by_customer_id_newest_first():
    c = _client()
    records = [_record("a", 800, 100), _record("a", 810, 200), _record("b", 600, 150)]
    with patch.object(c, "_get_records", return_value=records):
        profiles = c.list_profiles()
    assert {p.customer_id for p in profiles} == {"a", "b"}
    a = next(p for p in profiles if p.customer_id == "a")
    assert a.last_weight_kg == 81.0  # most recent record for "a" (810 -> 81.0)
    assert profiles[0].last_measured >= profiles[1].last_measured  # newest first
    assert isinstance(profiles[0], EufyProfile)
```

- [ ] **Step 2: Run, confirm fail**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py::test_list_profiles_groups_by_customer_id_newest_first -xvs`
Expected: FAIL with `ImportError: cannot import name 'AmbiguousProfileError'` (or `EufyProfile`).

- [ ] **Step 3: Add the dataclass, exception, and helpers**

In `eufy_sync/eufy_client.py`, after the `EufyMeasurement` dataclass, add:

```python
@dataclass
class EufyProfile:
    customer_id: str
    last_measured: datetime
    last_weight_kg: float
    name: str | None = None


class AmbiguousProfileError(Exception):
    """Several Eufy profiles exist on the account and none has been selected.

    Carries the detected profiles so the CLI can show a picker. Not retryable.
    """

    def __init__(self, profiles: list["EufyProfile"]):
        self.profiles = profiles
        super().__init__(
            f"{len(profiles)} Eufy profiles found but none selected. "
            "Run: eufy-sync --select-profile"
        )
```

Inside `EufyClient`, add the read/parse/group helpers and `list_profiles`. Place them just above the existing `fetch_measurements`:

```python
    def _get_records(self, after_timestamp: int | None) -> list[dict]:
        if not self.access_token or not self.user_id:
            raise RuntimeError("Must authenticate before fetching measurements")

        params = {}
        if after_timestamp is not None:
            params["after"] = str(after_timestamp)

        resp = self._client.get(
            f"{BASE_URL}/device/data",
            params=params,
            headers={"Token": self.access_token, "Uid": self.user_id},
        )

        needs_reauth = resp.status_code in (401, 403)
        if not needs_reauth and resp.status_code == 200:
            try:
                needs_reauth = resp.json().get("res_code") not in (1, None)
            except Exception:
                pass
        if needs_reauth:
            logger.warning("Eufy token rejected, re-authenticating...")
            self._clear_cached_token()
            self._fresh_login()
            resp = self._client.get(
                f"{BASE_URL}/device/data",
                params=params,
                headers={"Token": self.access_token, "Uid": self.user_id},
            )

        resp.raise_for_status()
        body = resp.json()
        if body.get("res_code") != 1:
            raise RuntimeError(f"Eufy fetch failed: {body.get('message', 'unknown error')}")

        raw_records = body.get("data", [])
        logger.info("Fetched %d raw measurements from Eufy", len(raw_records))
        return raw_records

    def _parse_all(self, records: list[dict]) -> list[EufyMeasurement]:
        out = []
        for record in records:
            m = self._parse_record(record)
            if m is not None:
                out.append(m)
        return out

    def _profiles_from(self, measurements: list[EufyMeasurement]) -> list[EufyProfile]:
        latest: dict[str, EufyMeasurement] = {}
        for m in measurements:
            cur = latest.get(m.customer_id)
            if cur is None or m.timestamp > cur.timestamp:
                latest[m.customer_id] = m
        profiles = [
            EufyProfile(
                customer_id=m.customer_id,
                last_measured=m.timestamp,
                last_weight_kg=m.weight_kg,
            )
            for m in latest.values()
        ]
        profiles.sort(key=lambda p: p.last_measured, reverse=True)
        return profiles

    def list_profiles(self) -> list[EufyProfile]:
        """Return one profile per customer_id seen in the full history, newest first."""
        records = self._get_records(None)
        return self._profiles_from(self._parse_all(records))
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -x`
Expected: PASS (new test green, existing parse tests still green).

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/eufy_client.py tests/test_eufy_client.py
git commit -m "feat(eufy): add EufyProfile, AmbiguousProfileError, and list_profiles"
```

---

## Task 3: Filter fetch_measurements by profile, raise when ambiguous

**Files:**
- Modify: `eufy_sync/eufy_client.py`
- Test: `tests/test_eufy_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eufy_client.py`:

```python
import pytest


def test_fetch_filters_to_configured_profile():
    c = _client(customer_id="a")
    records = [_record("a", 800, 100), _record("b", 600, 150)]
    with patch.object(c, "_get_records", return_value=records):
        measurements = c.fetch_measurements()
    assert {m.customer_id for m in measurements} == {"a"}


def test_fetch_single_profile_returns_all_when_unconfigured():
    c = _client()
    records = [_record("a", 800, 100), _record("a", 810, 200)]
    with patch.object(c, "_get_records", return_value=records):
        measurements = c.fetch_measurements()
    assert len(measurements) == 2


def test_fetch_raises_ambiguous_when_multiple_profiles_unconfigured():
    c = _client()
    records = [_record("a", 800, 100), _record("b", 600, 150)]
    with patch.object(c, "_get_records", return_value=records):
        with pytest.raises(AmbiguousProfileError) as exc_info:
            c.fetch_measurements()
    assert {p.customer_id for p in exc_info.value.profiles} == {"a", "b"}


def test_fetch_single_profile_windowed_by_after_timestamp():
    c = _client()
    old = _record("a", 800, 1_000)              # long ago
    new = _record("a", 810, 2_000_000_000)      # year 2033
    with patch.object(c, "_get_records", return_value=[old, new]):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 1
    assert measurements[0].weight_kg == 81.0
```

- [ ] **Step 2: Run, confirm fail**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py::test_fetch_raises_ambiguous_when_multiple_profiles_unconfigured -xvs`
Expected: FAIL — the current `fetch_measurements` returns all records and never raises.

- [ ] **Step 3: Replace fetch_measurements**

In `eufy_sync/eufy_client.py`, replace the entire existing `fetch_measurements` method body with:

```python
    def fetch_measurements(self, after_timestamp: int | None = None) -> list[EufyMeasurement]:
        if self.config.customer_id:
            measurements = self._parse_all(self._get_records(after_timestamp))
            return [m for m in measurements if m.customer_id == self.config.customer_id]

        # No profile selected: read full history so the profile count is reliable
        # even when only one person has weighed in recently.
        measurements = self._parse_all(self._get_records(None))
        distinct = {m.customer_id for m in measurements}
        if len(distinct) > 1:
            raise AmbiguousProfileError(self._profiles_from(measurements))

        if after_timestamp is not None:
            cutoff = datetime.fromtimestamp(after_timestamp, tz=timezone.utc)
            measurements = [m for m in measurements if m.timestamp >= cutoff]
        return measurements
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -x`
Expected: PASS (all five fetch/list tests plus the original parse tests).

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/eufy_client.py tests/test_eufy_client.py
git commit -m "feat(eufy): filter fetch by profile, raise AmbiguousProfileError when unset"
```

---

## Task 4: Treat AmbiguousProfileError as non-retryable

`fetch_measurements` runs inside `_retry`, so the ambiguous signal must surface immediately instead of retrying three times.

**Files:**
- Modify: `eufy_sync/sync.py`
- Test: `tests/test_sync.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_sync.py`:

```python
def test_ambiguous_profile_error_is_permanent():
    from eufy_sync.sync import _is_permanent
    from eufy_sync.eufy_client import AmbiguousProfileError
    assert _is_permanent(AmbiguousProfileError([])) is True
```

- [ ] **Step 2: Run, confirm fail**

Run: `.venv/bin/python -m pytest tests/test_sync.py::test_ambiguous_profile_error_is_permanent -xvs`
Expected: FAIL with `assert None is True` (or `False is True`).

- [ ] **Step 3: Recognize the exception**

In `eufy_sync/sync.py`, change the top import:

```python
from eufy_sync.eufy_client import EufyClient
```

to:

```python
from eufy_sync.eufy_client import AmbiguousProfileError, EufyClient
```

Then update `_is_permanent`:

```python
def _is_permanent(exc: BaseException) -> bool:
    if isinstance(exc, (PermanentSyncError, AmbiguousProfileError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # 4xx is client error - won't recover. 429 is the exception (rate-limited).
        return 400 <= status < 500 and status != 429
    return False
```

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_sync.py -x`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/sync.py tests/test_sync.py
git commit -m "fix(sync): treat AmbiguousProfileError as permanent so it isn't retried"
```

---

## Task 5: CLI picker helpers and --select-profile command

**Files:**
- Modify: `eufy_sync/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py` (it already imports `_write_config` from `eufy_sync.cli` and `MagicMock`, `patch`, `yaml`, `Path`; add any missing imports at the top of the file):

```python
def test_prompt_profile_choice_returns_selected_customer_id():
    from datetime import datetime, timezone
    from unittest.mock import patch
    from eufy_sync.cli import _prompt_profile_choice
    from eufy_sync.eufy_client import EufyProfile
    profiles = [
        EufyProfile("cid-a", datetime(2026, 6, 1, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 2, tzinfo=timezone.utc), 62.0),
    ]
    with patch("builtins.input", return_value="2"):
        assert _prompt_profile_choice(profiles) == "cid-b"


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_select_profile_writes_chosen_customer_id(_keyring, tmp_path: Path):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock, patch
    from eufy_sync.cli import _select_profile, _write_config
    from eufy_sync.eufy_client import EufyProfile

    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })

    fake = MagicMock()
    fake.list_profiles.return_value = [
        EufyProfile("cid-a", datetime(2026, 6, 2, tzinfo=timezone.utc), 80.0),
        EufyProfile("cid-b", datetime(2026, 6, 1, tzinfo=timezone.utc), 62.0),
    ]

    with patch("eufy_sync.eufy_client.EufyClient", return_value=fake), \
         patch("builtins.input", return_value="1"):
        _select_profile(cfg_path)

    written = yaml.safe_load(cfg_path.read_text())
    assert written["users"][0]["eufy"]["customer_id"] == "cid-a"
```

- [ ] **Step 2: Run, confirm fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py::test_prompt_profile_choice_returns_selected_customer_id -xvs`
Expected: FAIL with `ImportError: cannot import name '_prompt_profile_choice'`.

- [ ] **Step 3: Add the helpers and command**

In `eufy_sync/cli.py`, after `_setup_strava` (around line 277), add:

```python
def _format_profile(profile, index: int) -> str:
    lb = profile.last_weight_kg * 2.20462
    when = profile.last_measured.strftime("%Y-%m-%d")
    label = profile.name or f"profile ...{profile.customer_id[-4:]}"
    return f"  {index}. {label}  -  {profile.last_weight_kg:.1f} kg ({lb:.1f} lb), last weigh-in {when}"


def _prompt_profile_choice(profiles: list) -> str:
    """Print the profiles and return the customer_id the user picks."""
    print("")
    print("Multiple profiles were found on this Eufy account:")
    for i, p in enumerate(profiles, 1):
        print(_format_profile(p, i))
    print("")
    while True:
        choice = input(f"Which profile is yours? [1-{len(profiles)}] ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1].customer_id
        print("Enter a number from the list.")


def _select_profile(config_path: Path) -> None:
    """Choose which Eufy profile to sync, for an existing install."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    from eufy_sync.config import load_config
    from eufy_sync.eufy_client import EufyClient

    cfg = load_config(config_path)
    eufy = EufyClient(cfg.users[0].eufy)
    try:
        eufy.authenticate()
        profiles = eufy.list_profiles()
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

- [ ] **Step 4: Wire the flag into argparse and dispatch**

In `main()`, after the `--setup-strava` argument (line 799), add:

```python
    parser.add_argument("--select-profile", action="store_true", help="Choose which Eufy profile to sync")
```

After the Strava setup dispatch block (lines 831-834), add:

```python
    # Handle profile selection
    if args.select_profile:
        _select_profile(config_path)
        return
```

- [ ] **Step 5: Run tests**

Run: `.venv/bin/python -m pytest tests/test_cli.py -x`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add eufy_sync/cli.py tests/test_cli.py
git commit -m "feat(cli): add --select-profile and the shared profile picker"
```

---

## Task 6: Detect profiles during first-run setup

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Add profile detection to the wizard**

In `_first_run_setup`, the config dict is built into `user_config` and then written at:

```python
    config = {"users": [user_config]}
    _write_config(config_path, config)
```

Immediately BEFORE that `config = {"users": [user_config]}` line, insert:

```python
    # On a shared account, pick the right person before the first sync.
    try:
        from eufy_sync.config import EufyConfig
        from eufy_sync.eufy_client import EufyClient
        probe = EufyClient(EufyConfig(email=eufy_email, password=eufy_password))
        try:
            probe.authenticate()
            profiles = probe.list_profiles()
        finally:
            probe.close()
        if len(profiles) > 1:
            user_config["eufy"]["customer_id"] = _prompt_profile_choice(profiles)
    except Exception as e:
        # Non-fatal: if this fails, the first sync safely stops and prompts.
        print(f"Note: could not check Eufy profiles right now ({e}).")
```

- [ ] **Step 2: Run the suite**

Run: `.venv/bin/python -m pytest tests/ -x`
Expected: PASS (no test changes; the wizard's network calls are not exercised by the suite).

- [ ] **Step 3: Sanity-check the CLI imports cleanly**

Run: `.venv/bin/python -c "import eufy_sync.cli"`
Expected: no output, no traceback.

- [ ] **Step 4: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): detect and pick Eufy profile during first-run setup"
```

---

## Task 7: Render the picker when a sync hits the ambiguous case

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Add the handler to the sync loop**

In `main()`, find the per-user sync loop:

```python
        for user in config.users:
            try:
                counts = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                for target_name, count in counts.items():
                    total_counts[target_name] = total_counts.get(target_name, 0) + count
                logger.info("User %s: synced %s", user.name, counts)
            except Exception as e:
                logger.exception("Failed to sync user %s", user.name)
                failures.append((user.name, str(e)))
```

Insert a specific handler BEFORE the `except Exception` branch:

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

At the top of `main()`, alongside the other sync imports (the `from eufy_sync.sync import sync_user` line around line 884), add:

```python
    from eufy_sync.eufy_client import AmbiguousProfileError
```

- [ ] **Step 2: Run the suite**

Run: `.venv/bin/python -m pytest tests/ -x`
Expected: PASS.

- [ ] **Step 3: Sanity-check imports**

Run: `.venv/bin/python -c "import eufy_sync.cli"`
Expected: no output, no traceback.

- [ ] **Step 4: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): show profile picker guidance when a sync is ambiguous"
```

---

## Task 8: README

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the command**

In `README.md`, in the `## Usage` command list, after the `--setup-strava` line, add:

```
eufy-sync --select-profile   # choose which Eufy profile to sync (shared scale)
```

- [ ] **Step 2: Add a shared-account note**

In `README.md`, in the `## Known quirks` section, add a paragraph:

```
If more than one person uses the same Eufy account, the tool asks which profile is yours during setup, so only your weigh-ins sync. If you set it up before this was added, run `eufy-sync --select-profile` once. Until you choose, a sync that sees several profiles stops and shows them rather than guessing.
```

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: document --select-profile and shared-account behavior"
```

---

## Final Verification

- [ ] **Step 1: Run the full suite**

Run: `.venv/bin/python -m pytest tests/ -v`
Expected: all tests pass, including the new config, eufy_client, sync, and cli tests.

- [ ] **Step 2: Sanity-check the CLI**

```bash
.venv/bin/eufy-sync --help
.venv/bin/eufy-sync --version
```

Expected: `--help` lists `--select-profile`; both print without a traceback.

- [ ] **Step 3: Push the branch and open a PR**

```bash
git push -u origin fix-profile-selection
gh pr create --title "Fix issue #1: sync only the selected Eufy profile" --body "$(cat <<'EOF'
## Summary
On a shared Eufy account the tool synced every household member's weigh-in, so a user got their partner's weight written to their own Garmin/Strava. This adds an optional profile selection.

- New optional `eufy.customer_id` filters sync to one profile.
- `EufyClient.list_profiles()` powers a picker; `fetch_measurements` filters to the selected profile.
- When several profiles exist and none is selected, the sync stops with a non-retryable `AmbiguousProfileError` instead of guessing.
- `--select-profile` lets existing installs choose; the first-run wizard prompts automatically.

Closes #1. See `docs/superpowers/specs/2026-06-18-eufy-profile-selection-design.md`.

## Test plan
- [x] `pytest tests/` green
- [ ] Manual: run `--select-profile` against a real shared account and confirm only the chosen profile syncs
EOF
)"
```

- [ ] **Step 4: Watch CI, merge, clean up**

```bash
gh pr checks <N> --watch
gh api -X PUT /repos/sturimcode/eufy-sync/pulls/<N>/merge -f merge_method=squash
gh api -X DELETE /repos/sturimcode/eufy-sync/git/refs/heads/fix-profile-selection
```
