# Raw Wi-Fi Weight Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the normal Eufy pull returns nothing for the window, recover the weight from the per-device raw Wi-Fi endpoint so a headless run still syncs.

**Architecture:** Add a fallback inside `EufyClient.fetch_measurements`: when the existing normal path yields an empty list, fetch raw records per device and run them through the same parser, profile filter, and window filter. Two new HTTP leaf methods plus one orchestration method, all in `eufy_sync/eufy_client.py`.

**Tech Stack:** Python 3.12, httpx, pytest.

## Global Constraints

- No em-dashes in any copy, UI text, or documentation string.
- One user per installation; the configured profile is `config.customer_id`.
- Run tests with `.venv/bin/python -m pytest`. Baseline before this plan: 115 passed.
- The raw endpoint returns records under a `list` field that can be JSON `null`; `res_code == 1` means success. Weight is decigrams (divide by 10 for kg).
- Reuse the existing `_parse_all`, `transform`, and the `m.customer_id == config.customer_id` filter. Add no config options and no new files.
- Follow the existing test style in `tests/test_eufy_client.py` (the `_client()` and `_record()` helpers, `patch.object` for mocking).

## File Structure

- `eufy_sync/eufy_client.py` (modify): three new methods (`_list_device_ids`, `_get_raw_records`, `_fetch_raw_measurements`) and a restructured `fetch_measurements` that funnels both branches to a single fallback check.
- `tests/test_eufy_client.py` (modify): a small `_resp()` mock helper and the new tests.

---

### Task 1: HTTP leaf methods (`_list_device_ids`, `_get_raw_records`)

**Files:**
- Modify: `eufy_sync/eufy_client.py` (add two methods near `_get_records`)
- Test: `tests/test_eufy_client.py`

**Interfaces:**
- Produces: `_list_device_ids(self) -> list[str]` and `_get_raw_records(self, device_id: str, after_timestamp: int | None) -> list[dict]`. Both read `self._client`, `self.access_token`, `self.user_id`. Both return `[]` on a non-200 status or `res_code != 1`.

- [ ] **Step 1: Add a response-mock helper and the failing tests**

In `tests/test_eufy_client.py`, add `MagicMock` to the mock import at the top so it reads:

```python
from unittest.mock import MagicMock, patch
```

Then append:

```python
def _resp(status_code, json_body):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    return r


def test_list_device_ids_parses_device_v2():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(
        200, {"res_code": 1, "devices": [{"id": "dev1"}, {"id": "dev2"}, {"id": ""}]}
    )
    assert c._list_device_ids() == ["dev1", "dev2"]


def test_list_device_ids_empty_on_error_code():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(200, {"res_code": 0, "devices": []})
    assert c._list_device_ids() == []


def test_get_raw_records_extracts_list():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(200, {"res_code": 1, "list": [_record("a", 800, 100)]})
    recs = c._get_raw_records("dev1", None)
    assert len(recs) == 1
    assert recs[0]["customer_id"] == "a"


def test_get_raw_records_handles_null_list_500_and_bad_code():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(200, {"res_code": 1, "list": None})
    assert c._get_raw_records("d", None) == []
    c._client.get.return_value = _resp(500, {})
    assert c._get_raw_records("d", None) == []
    c._client.get.return_value = _resp(200, {"res_code": 500, "message": "unavailable"})
    assert c._get_raw_records("d", None) == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -k "device_ids or raw_records" -v`
Expected: FAIL with `AttributeError: ... object has no attribute '_list_device_ids'` (and the same for `_get_raw_records`).

- [ ] **Step 3: Implement the two methods**

In `eufy_sync/eufy_client.py`, add these methods to `EufyClient`, just after `_get_records` (around line 222):

```python
    def _list_device_ids(self) -> list[str]:
        """Return the account's device ids from /device/v2. Empty on any error."""
        resp = self._client.get(
            f"{BASE_URL}/device/v2",
            headers={"Token": self.access_token, "Uid": self.user_id},
        )
        if resp.status_code != 200:
            return []
        body = resp.json()
        if body.get("res_code") != 1:
            return []
        return [d["id"] for d in body.get("devices", []) if d.get("id")]

    def _get_raw_records(self, device_id: str, after_timestamp: int | None) -> list[dict]:
        """Fetch a device's raw Wi-Fi weight records. These appear before the
        phone app processes a weigh-in. Records live under a nullable `list`
        field. Empty on any error."""
        params = {}
        if after_timestamp is not None:
            params["after"] = str(after_timestamp)
        resp = self._client.get(
            f"{BASE_URL}/device/wifi_scale/raw_data/{device_id}",
            params=params,
            headers={"Token": self.access_token, "Uid": self.user_id},
        )
        if resp.status_code != 200:
            return []
        body = resp.json()
        if body.get("res_code") != 1:
            return []
        return body.get("list") or []
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -k "device_ids or raw_records" -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full file to confirm no regression**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -q`
Expected: PASS (all existing tests plus the 4 new ones).

- [ ] **Step 6: Commit**

```bash
git add eufy_sync/eufy_client.py tests/test_eufy_client.py
git commit -m "$(cat <<'EOF'
Add raw Wi-Fi endpoint HTTP helpers to EufyClient

_list_device_ids reads /device/v2; _get_raw_records reads the per-device
wifi_scale/raw_data endpoint (records under a nullable `list` field). Both
degrade to an empty list on a non-200 status or res_code != 1.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Fallback orchestration and the `fetch_measurements` trigger

**Files:**
- Modify: `eufy_sync/eufy_client.py` (rewrite `fetch_measurements` at lines 254-269; add `_fetch_raw_measurements`)
- Test: `tests/test_eufy_client.py`

**Interfaces:**
- Consumes: `_list_device_ids()`, `_get_raw_records(device_id, after_timestamp)` (Task 1); `_parse_all`, `_profiles_from`, `transform` (existing).
- Produces: `_fetch_raw_measurements(self, after_timestamp: int | None) -> list[EufyMeasurement]`. `fetch_measurements` now calls it when the normal path is empty.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_eufy_client.py`:

```python
def test_fetch_falls_back_to_raw_when_normal_empty():
    c = _client(customer_id="a")
    raw = _record("a", 800, 2_000_000_000)  # year 2033, passes the window
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=[raw]):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 1
    assert measurements[0].weight_kg == 80.0
    assert measurements[0].customer_id == "a"


def test_fetch_does_not_use_raw_when_normal_has_data():
    c = _client(customer_id="a")
    raw_probe = MagicMock()
    with patch.object(c, "_get_records", return_value=[_record("a", 800, 2_000_000_000)]), \
         patch.object(c, "_list_device_ids", raw_probe):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 1
    raw_probe.assert_not_called()


def test_raw_fallback_drops_other_profiles():
    c = _client(customer_id="a")
    raws = [_record("a", 800, 2_000_000_000), _record("b", 600, 2_000_000_000)]
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=raws):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert {m.customer_id for m in measurements} == {"a"}


def test_raw_fallback_degrades_when_device_list_errors():
    c = _client(customer_id="a")
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", side_effect=RuntimeError("boom")):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert measurements == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -k "fall_back or does_not_use_raw or drops_other_profiles or degrades" -v`
Expected: FAIL. The first three fail because `fetch_measurements` returns `[]` without consulting the raw methods (it has no fallback yet); `degrades` fails because `_fetch_raw_measurements` does not exist.

- [ ] **Step 3: Rewrite `fetch_measurements` to funnel to a fallback check**

In `eufy_sync/eufy_client.py`, replace the current `fetch_measurements` (lines 254-269):

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

with this version:

```python
    def fetch_measurements(self, after_timestamp: int | None = None) -> list[EufyMeasurement]:
        if self.config.customer_id:
            parsed = self._parse_all(self._get_records(after_timestamp))
            measurements = [m for m in parsed if m.customer_id == self.config.customer_id]
        else:
            # No profile selected: read full history so the profile count is
            # reliable even when only one person has weighed in recently.
            parsed = self._parse_all(self._get_records(None))
            distinct = {m.customer_id for m in parsed}
            if len(distinct) > 1:
                raise AmbiguousProfileError(self._profiles_from(parsed))
            if after_timestamp is not None:
                cutoff = datetime.fromtimestamp(after_timestamp, tz=timezone.utc)
                measurements = [m for m in parsed if m.timestamp >= cutoff]
            else:
                measurements = parsed

        if measurements:
            return measurements
        # The normal endpoints had nothing in the window. This is the headless
        # case: a weigh-in that the phone app has not processed yet. Fall back to
        # the per-device raw Wi-Fi endpoint, which exposes the weight earlier.
        return self._fetch_raw_measurements(after_timestamp)
```

- [ ] **Step 4: Add `_fetch_raw_measurements`**

In `eufy_sync/eufy_client.py`, add this method directly after `fetch_measurements`:

```python
    def _fetch_raw_measurements(self, after_timestamp: int | None) -> list[EufyMeasurement]:
        """Recover weight-only measurements from the raw Wi-Fi endpoint, applying
        the same profile and window filters as the normal path. Degrades to an
        empty list on any error so the run is never worse than today."""
        try:
            device_ids = self._list_device_ids()
        except Exception as e:
            logger.warning("Raw Wi-Fi fallback: could not list devices: %s", e)
            return []

        records: list[dict] = []
        for device_id in device_ids:
            try:
                records.extend(self._get_raw_records(device_id, after_timestamp))
            except Exception as e:
                logger.warning("Raw Wi-Fi fallback: fetch failed for %s: %s", device_id, e)

        measurements = self._parse_all(records)
        raw_count = len(measurements)

        if self.config.customer_id:
            measurements = [m for m in measurements if m.customer_id == self.config.customer_id]
        if after_timestamp is not None:
            cutoff = datetime.fromtimestamp(after_timestamp, tz=timezone.utc)
            measurements = [m for m in measurements if m.timestamp >= cutoff]

        if measurements:
            logger.info("Recovered %d weight-only measurement(s) from the raw Wi-Fi endpoint", len(measurements))
        elif raw_count:
            logger.info(
                "Raw Wi-Fi endpoint returned %d record(s) but none passed the profile or window filter "
                "(raw records may not carry a profile id)", raw_count,
            )
        return measurements
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_eufy_client.py -k "fall_back or does_not_use_raw or drops_other_profiles or degrades" -v`
Expected: PASS (4 passed).

- [ ] **Step 6: Run the full suite to confirm no regression**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (123 passed: 115 baseline + 8 new).

- [ ] **Step 7: Commit**

```bash
git add eufy_sync/eufy_client.py tests/test_eufy_client.py
git commit -m "$(cat <<'EOF'
Fall back to the raw Wi-Fi endpoint when the normal pull is empty

When fetch_measurements finds nothing in the window (the headless case, before
the phone app processes a weigh-in), fetch per-device raw records and run them
through the same parser, profile filter, and window filter. Reuses the existing
transform, so weight-only records become clean weight-only uploads and an
implausible weight is dropped, never synced. Degrades to today's behavior on any
error.

Addresses issue #2.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the entire suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (123 passed).

- [ ] **Step 2: Confirm the working tree and branch**

Run: `git status --short && git branch --show-current`
Expected: no uncommitted source changes (an untracked `uv.lock` is fine); branch `raw-wifi-fallback`.

---

## Self-Review

**Spec coverage:**
- Trigger (fallback only when normal is empty) -> Task 2 Step 3.
- `_list_device_ids` -> Task 1. `_get_raw_records` (nullable `list`, res_code handling) -> Task 1. `_fetch_raw_measurements` (per-device loop, reused filters, diagnostic log) -> Task 2 Step 4.
- Reuse of parser/transform/filter -> Task 2 (no new parsing; `_parse_all` + the customer_id filter).
- Dedupe -> unchanged, relies on existing `is_synced` / `has_weight_on_date` (no code in this plan, correct per spec).
- Tests 1-6 from the spec -> Task 1 (device-list parse + error, raw-list extract + null/500/bad-code) and Task 2 (fallback fires only when empty, recovers weight-only, not called when normal has data, profile filter on raw, degrades on device-list error).

**Placeholder scan:** none. Every step has concrete code and commands.

**Type consistency:** `_list_device_ids() -> list[str]`, `_get_raw_records(device_id: str, after_timestamp: int | None) -> list[dict]`, and `_fetch_raw_measurements(after_timestamp: int | None) -> list[EufyMeasurement]` are used with the same names and signatures across Task 1 and Task 2. The `_resp(status_code, json_body)` helper is defined in Task 1 Step 1 and used only there.

## Live test (post-merge, requires the user)

Not a plan task. After merge, the user weighs in, does not open the Eufy app, and runs `eufy-sync --dry-run` from the repo so the raw path is exercised against a real pending record. A reported weight confirms the path works and that raw records carry a usable profile id. Nothing reported means raw records lack `customer_id`, and the follow-up is to relax the filter for the single-device case.
