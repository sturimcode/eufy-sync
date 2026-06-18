# Garmin Auth Swap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Playwright browser login with the browser-free `python-garminconnect` library, letting it own Garmin login, token refresh, and the body-composition upload while keeping the same-date duplicate check.

**Architecture:** `garmin_auth.py` becomes a thin manager around `garminconnect.Garmin` that persists the library's token blob to the keychain. `garmin_client.py` keeps its public surface but delegates upload to `add_body_composition` and dedup to the library's read API. `fit.py` and the Playwright code are deleted. `sync.py` changes one kwarg.

**Tech Stack:** Python 3.12+, `garminconnect>=0.3.6`, `curl_cffi`, keyring, pytest. The library is mocked in all tests (no network).

**Source spec:** `docs/superpowers/specs/2026-06-18-garmin-auth-curl-cffi-design.md`

**Test runner:** `~/.local/bin/uv run pytest`. Run a single file with e.g. `~/.local/bin/uv run pytest tests/test_garmin_auth.py -v`.

---

## File Structure

**Delete:**
- `eufy_sync/fit.py` — the library builds the FIT file inside `add_body_composition`
- `tests/test_fit.py`

**Rewrite:**
- `eufy_sync/garmin_auth.py` — `GarminAuth` around `garminconnect.Garmin` + keychain token persistence
- `tests/test_garmin_auth.py` — mock `garminconnect.Garmin`

**Modify:**
- `eufy_sync/garmin_client.py` — delegate upload/dedup to the library, rename `allow_browser` to `allow_interactive`
- `eufy_sync/sync.py` — one kwarg rename at the Garmin authenticate call
- `eufy_sync/__init__.py` — drop the `FitEncoder` export
- `eufy_sync/cli.py` — remove `_ensure_chromium` + call + wizard browser text; update the Garmin branches of `_reauth`, `_show_status`, `_print_summary`
- `pyproject.toml`, `requirements.txt` — swap dependencies
- `README.md` — rewrite "How Garmin login works"

**Create:**
- `tests/test_garmin_client.py` — mock the library, assert field mapping and dedup

---

## Task 1: Swap dependencies

**Files:**
- Modify: `pyproject.toml`, `requirements.txt`

- [ ] **Step 1: Edit `pyproject.toml` dependencies**

The `[project]` `dependencies` list currently contains:
```toml
dependencies = [
    "httpx>=0.27.0",
    "keyring>=25.0.0",
    "playwright>=1.40.0",
    "pyyaml>=6.0",
]
```
Replace the `playwright` line so the list reads:
```toml
dependencies = [
    "httpx>=0.27.0",
    "keyring>=25.0.0",
    "garminconnect>=0.3.6",
    "curl_cffi>=0.7.0",
    "pyyaml>=6.0",
]
```

- [ ] **Step 2: Edit `requirements.txt`**

It currently reads:
```
httpx>=0.27.0
playwright>=1.40.0
pyyaml>=6.0
```
Replace the `playwright` line:
```
httpx>=0.27.0
garminconnect>=0.3.6
curl_cffi>=0.7.0
pyyaml>=6.0
```

- [ ] **Step 3: Sync the environment and confirm the library imports**

Run:
```bash
~/.local/bin/uv run python -c "from garminconnect import Garmin; print('garminconnect ok')"
```
Expected: prints `garminconnect ok` (uv installs the new deps on first run). If it fails, run `~/.local/bin/uv sync` and retry.

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml requirements.txt
git commit -m "build: swap playwright for garminconnect + curl_cffi"
```

---

## Task 2: Rewrite garmin_auth.py around garminconnect

**Files:**
- Rewrite: `eufy_sync/garmin_auth.py`
- Rewrite: `tests/test_garmin_auth.py`

- [ ] **Step 1: Verify the library's auth + token-persistence API on the installed version**

The exact attribute that holds the token dump/load methods varies by version. Confirm it before writing code:
```bash
~/.local/bin/uv run python -c "import garminconnect, inspect; g=garminconnect.Garmin; print([m for m in dir(g) if not m.startswith('__')])"
~/.local/bin/uv run python -c "import garminconnect, inspect; print(inspect.signature(garminconnect.Garmin.__init__)); print(inspect.signature(garminconnect.Garmin.login))"
```
CONFIRMED on the installed garminconnect 0.3.6 (verified during planning): the token serialize/deserialize methods are on `garmin.client` — `garmin.client.dumps()` returns a JSON string, `garmin.client.loads(json_str)` restores. (`garmin.garth` does NOT exist on 0.3.6.) The constructor takes `prompt_mfa`, and `login()` does a fresh credential login when called with no `tokenstore`. The code below already uses `garmin.client`; just confirm it still holds on the installed version before relying on it.

- [ ] **Step 2: Write the failing tests**

Replace the entire contents of `tests/test_garmin_auth.py` with:

```python
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.sync import PermanentSyncError

BLOB = {"di_token": "tok", "di_refresh_token": "rtok", "di_client_id": "cid"}


def _auth():
    return GarminAuth("test@example.com", "pw")


def test_login_restores_saved_blob_without_fresh_login(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    fake_garmin = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin) as ctor:
        result = auth.login(interactive=True)
    assert result is fake_garmin
    fake_garmin.login.assert_not_called()       # restored from blob, no fresh login
    assert ctor.call_count == 1


def test_login_fresh_when_no_blob_and_interactive(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    saved = {}
    monkeypatch.setattr(auth, "_save_token", lambda g: saved.setdefault("called", True))
    fake_garmin = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin):
        auth.login(interactive=True)
    fake_garmin.login.assert_called_once()
    assert saved.get("called") is True


def test_login_raises_when_no_blob_and_not_interactive(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    with patch("eufy_sync.garmin_auth.Garmin", return_value=MagicMock()):
        with pytest.raises(PermanentSyncError):
            auth.login(interactive=False)


def test_token_status_valid_with_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    assert auth.token_status()["state"] == "valid"


def test_token_status_no_session_without_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    assert auth.token_status()["state"] == "no_session"
```

- [ ] **Step 3: Run, confirm failure**

Run: `~/.local/bin/uv run pytest tests/test_garmin_auth.py -x`
Expected: import/attribute errors (the old `TokenPair`/`GarminSession` are gone; `login`/`_load_token` not yet defined).

- [ ] **Step 4: Replace the entire contents of `eufy_sync/garmin_auth.py`**

```python
"""Garmin authentication via the python-garminconnect library (browser-free).

The library logs in with curl_cffi TLS impersonation, handles MFA via a
callback, auto-refreshes the access token, and uploads body composition.
We persist its token blob to the system keychain so logins are rare.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from garminconnect import Garmin

logger = logging.getLogger(__name__)


def _mfa_prompt() -> str:
    return input("Garmin MFA code (check your email): ").strip()


class GarminAuth:
    """Manages a garminconnect.Garmin client and its persisted token blob."""

    def __init__(self, email: str, password: str, session_path: Path | None = None):
        self.email = email
        self.password = password
        self.session_path = session_path or Path.home() / ".garmin-sync" / "session.json"

    def login(self, interactive: bool = True) -> Garmin:
        """Return an authenticated Garmin client.

        Restores the saved token blob if present; otherwise does a fresh
        login (prompting for MFA) when interactive, or raises when not.
        """
        garmin = Garmin(self.email, self.password, prompt_mfa=_mfa_prompt)

        blob = self._load_token()
        if blob is not None:
            try:
                garmin.client.loads(json.dumps(blob))
                logger.info("Restored saved Garmin session")
                return garmin
            except Exception as e:
                logger.warning("Saved Garmin token unusable (%s); re-logging in", e)

        if not interactive:
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError("Garmin login needed; run: eufy-sync --reauth")

        garmin.login()
        self._save_token(garmin)
        logger.info("Authenticated to Garmin as %s", self.email)
        return garmin

    def force_reauth(self) -> Garmin:
        """Clear the stored token and do a fresh interactive login."""
        self._clear_token()
        garmin = Garmin(self.email, self.password, prompt_mfa=_mfa_prompt)
        garmin.login()
        self._save_token(garmin)
        logger.info("Re-authenticated to Garmin as %s", self.email)
        return garmin

    def token_status(self) -> dict:
        """Return token health. The library auto-refreshes, so the states
        collapse to valid (a blob exists) or no_session."""
        if self._load_token() is not None:
            return {"state": "valid", "days_remaining": None}
        return {"state": "no_session", "days_remaining": None}

    def _load_token(self) -> dict | None:
        from eufy_sync.credentials import get_token, _keyring_available
        # A valid library blob has di_token as a STRING. The old Playwright
        # session also had a "di_token" key but as a nested dict; reject it so
        # those users migrate cleanly to a fresh login.
        if _keyring_available():
            data = get_token("garmin")
            if data and isinstance(data.get("di_token"), str):
                return data
            return None
        if not self.session_path.exists():
            return None
        try:
            data = json.loads(self.session_path.read_text())
            return data if isinstance(data.get("di_token"), str) else None
        except Exception:
            return None

    def _save_token(self, garmin: Garmin) -> None:
        blob = json.loads(garmin.client.dumps())
        from eufy_sync.credentials import store_token, _keyring_available
        if _keyring_available():
            store_token("garmin", blob)
            if self.session_path.exists():
                self.session_path.unlink()
            return
        self.session_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(self.session_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(blob, indent=2))

    def _clear_token(self) -> None:
        from eufy_sync.credentials import delete_token, _keyring_available
        if _keyring_available():
            delete_token("garmin")
        if self.session_path.exists():
            self.session_path.unlink()
```

Note on the import: `sync.py` imports from `garmin_client` (which imports `garmin_auth`), so importing `PermanentSyncError` from `sync` at module top would risk a cycle. Import it lazily inside `login()` at the point of `raise`, matching how `eufy_client.py` already does it.

- [ ] **Step 5: Run tests**

Run: `~/.local/bin/uv run pytest tests/test_garmin_auth.py -x`
Expected: all 5 pass.

- [ ] **Step 6: Commit**

```bash
git add eufy_sync/garmin_auth.py tests/test_garmin_auth.py
git commit -m "feat(garmin): rewrite GarminAuth around python-garminconnect"
```

---

## Task 3: Rewrite garmin_client.py to delegate upload + dedup

**Files:**
- Modify: `eufy_sync/garmin_client.py`
- Create: `tests/test_garmin_client.py`

- [ ] **Step 1: Confirm the upload/read method signatures on the installed library**

```bash
~/.local/bin/uv run python -c "import garminconnect, inspect; print(inspect.signature(garminconnect.Garmin.add_body_composition))"
~/.local/bin/uv run python -c "import garminconnect; print([m for m in dir(garminconnect.Garmin) if 'body' in m or 'weigh' in m])"
```
Expected: `add_body_composition` takes `timestamp, weight, percent_fat, percent_hydration, visceral_fat_mass, bone_mass, muscle_mass, basal_met, active_met, physique_rating, metabolic_age, visceral_fat_rating, bmi`. Confirm the read method name for dedup (`get_body_composition(startdate, enddate)`); use whatever the installed version exposes for reading weigh-ins by date.

Also confirm the library's authentication exception name (used below to detect a dead restored session):
```bash
~/.local/bin/uv run python -c "import garminconnect; print([n for n in dir(garminconnect) if 'Error' in n or 'Exception' in n])"
```
Expected: `GarminConnectAuthenticationError` is present. Use that exact name in the upload guard below; if the installed version names it differently, use that name.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_garmin_client.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from eufy_sync.config import GarminConfig
from eufy_sync.garmin_client import GarminClient
from eufy_sync.transform import GarminBodyComposition


def _client_with_fake_garmin(fake_garmin):
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    client._garmin = fake_garmin
    return client


def test_upload_maps_fields_to_add_body_composition():
    fake = MagicMock()
    fake.add_body_composition.return_value = {"ok": True}
    client = _client_with_fake_garmin(fake)
    bc = GarminBodyComposition(
        timestamp="2026-06-10T08:00:00+00:00",
        weight=86.2,
        percent_fat=18.5,
        percent_hydration=55.3,
        visceral_fat_rating=8.0,
        bone_mass=3.2,
        muscle_mass=45.2,
        basal_met=1650,
        metabolic_age=28,
        bmi=None,
    )
    client.upload_body_composition(bc)
    kwargs = fake.add_body_composition.call_args.kwargs
    assert kwargs["weight"] == 86.2
    assert kwargs["timestamp"] == "2026-06-10T08:00:00+00:00"
    assert kwargs["percent_fat"] == 18.5
    assert kwargs["visceral_fat_rating"] == 8.0
    assert kwargs["basal_met"] == 1650


def test_has_weight_on_date_true_when_entry_exists():
    fake = MagicMock()
    fake.get_body_composition.return_value = {"dateWeightList": [{"weight": 86000}]}
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True


def test_has_weight_on_date_false_when_empty():
    fake = MagicMock()
    fake.get_body_composition.return_value = {"dateWeightList": []}
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False


def test_has_weight_on_date_false_on_read_error():
    fake = MagicMock()
    fake.get_body_composition.side_effect = RuntimeError("boom")
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False


def test_authenticate_uses_auth_login():
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    fake_garmin = MagicMock()
    with patch.object(client._auth, "login", return_value=fake_garmin) as login:
        client.authenticate(allow_interactive=False)
    login.assert_called_once_with(interactive=False)
    assert client._garmin is fake_garmin


def test_upload_reauths_and_retries_on_auth_error_when_interactive():
    from garminconnect import GarminConnectAuthenticationError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    fresh = MagicMock()
    fresh.add_body_composition.return_value = {"ok": True}
    client._garmin = dead
    client._allow_interactive = True
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth", return_value=fresh) as reauth:
        client.upload_body_composition(bc)
    reauth.assert_called_once()
    fresh.add_body_composition.assert_called_once()   # retried on the fresh client
    assert client._garmin is fresh


def test_upload_raises_permanent_on_auth_error_when_headless():
    from garminconnect import GarminConnectAuthenticationError
    from eufy_sync.sync import PermanentSyncError
    import pytest
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with pytest.raises(PermanentSyncError):
        client.upload_body_composition(bc)
```

- [ ] **Step 3: Run, confirm failure**

Run: `~/.local/bin/uv run pytest tests/test_garmin_client.py -x`
Expected: failures (`_garmin`, new method bodies not present; old code imports `FitEncoder`).

- [ ] **Step 4: Replace the entire contents of `eufy_sync/garmin_client.py`**

```python
"""Garmin Connect client. Delegates login, refresh, and upload to
python-garminconnect; keeps a same-date duplicate check.
"""
from __future__ import annotations

import logging
from datetime import datetime

from garminconnect import GarminConnectAuthenticationError

from eufy_sync.config import GarminConfig
from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.transform import GarminBodyComposition

logger = logging.getLogger(__name__)


class GarminClient:
    def __init__(self, config: GarminConfig):
        self.config = config
        self._auth = GarminAuth(config.email, config.password)
        self._garmin = None
        self._allow_interactive = True

    def authenticate(self, allow_interactive: bool = True) -> None:
        self._allow_interactive = allow_interactive
        self._garmin = self._auth.login(interactive=allow_interactive)
        logger.info("Authenticated to Garmin Connect as %s", self.config.email)

    def has_weight_on_date(self, dt: datetime) -> bool:
        """Whether Garmin already has a weight entry for the date."""
        date_str = dt.strftime("%Y-%m-%d")
        try:
            data = self._garmin.get_body_composition(date_str, date_str)
            entries = data.get("dateWeightList", data.get("dailyWeightSummaries", []))
            return len(entries) > 0
        except Exception as e:
            # Fail open: let the upload proceed; Garmin de-dupes by timestamp.
            logger.warning("Garmin duplicate-check failed for %s: %s", date_str, e)
            return False

    def _add_body_composition(self, body_comp: GarminBodyComposition):
        return self._garmin.add_body_composition(
            timestamp=body_comp.timestamp,
            weight=body_comp.weight,
            percent_fat=body_comp.percent_fat,
            percent_hydration=body_comp.percent_hydration,
            visceral_fat_rating=body_comp.visceral_fat_rating,
            bone_mass=body_comp.bone_mass,
            muscle_mass=body_comp.muscle_mass,
            basal_met=body_comp.basal_met,
            metabolic_age=body_comp.metabolic_age,
            bmi=body_comp.bmi,
        )

    def upload_body_composition(self, body_comp: GarminBodyComposition) -> dict:
        try:
            result = self._add_body_composition(body_comp)
        except GarminConnectAuthenticationError:
            # Restored session is dead (refresh token expired or revoked).
            if not self._allow_interactive:
                from eufy_sync.sync import PermanentSyncError
                raise PermanentSyncError(
                    "Garmin session expired; run: eufy-sync --reauth"
                )
            logger.info("Garmin session expired; re-authenticating")
            self._garmin = self._auth.force_reauth()
            result = self._add_body_composition(body_comp)
        logger.info(
            "Uploaded body comp to Garmin: %.1f kg at %s",
            body_comp.weight, body_comp.timestamp,
        )
        return result if isinstance(result, dict) else {"status": "ok"}

    def close(self) -> None:
        self._garmin = None
```

- [ ] **Step 5: Run tests**

Run: `~/.local/bin/uv run pytest tests/test_garmin_client.py -x`
Expected: all 5 pass.

- [ ] **Step 6: Commit**

```bash
git add eufy_sync/garmin_client.py tests/test_garmin_client.py
git commit -m "feat(garmin): delegate upload + dedup to python-garminconnect"
```

---

## Task 4: Delete fit.py and drop its export

**Files:**
- Delete: `eufy_sync/fit.py`, `tests/test_fit.py`
- Modify: `eufy_sync/__init__.py`

- [ ] **Step 1: Delete the FIT encoder and its tests**

```bash
git rm eufy_sync/fit.py tests/test_fit.py
```

- [ ] **Step 2: Remove the FitEncoder export from `eufy_sync/__init__.py`**

Delete the line:
```python
from eufy_sync.fit import FitEncoder
```
and remove `"FitEncoder",` from the `__all__` list.

- [ ] **Step 3: Confirm nothing else imports fit**

Run: `~/.local/bin/uv run python -c "import eufy_sync, eufy_sync.cli, eufy_sync.garmin_client"`
Expected: no `ModuleNotFoundError`. Also run `grep -rn "from eufy_sync.fit\|import fit\|FitEncoder" eufy_sync tests` and confirm no remaining references.

- [ ] **Step 4: Run the full suite**

Run: `~/.local/bin/uv run pytest -q`
Expected: green (sync tests still pass; they mock GarminClient).

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "refactor: delete hand-rolled FIT encoder (library builds it now)"
```

---

## Task 5: Rename the authenticate kwarg in sync.py

**Files:**
- Modify: `eufy_sync/sync.py`

- [ ] **Step 1: Update the Garmin authenticate call**

In `eufy_sync/sync.py`, the Garmin branch of the authenticate loop reads:
```python
            if target_name == "garmin":
                client.authenticate(allow_browser=not headless)
            else:
                client.authenticate()
```
Change the Garmin line to:
```python
            if target_name == "garmin":
                client.authenticate(allow_interactive=not headless)
            else:
                client.authenticate()
```

- [ ] **Step 2: Run the full suite**

Run: `~/.local/bin/uv run pytest -q`
Expected: green. The sync tests mock `GarminClient`, so the kwarg rename flows through.

- [ ] **Step 3: Commit**

```bash
git add eufy_sync/sync.py
git commit -m "refactor(sync): rename allow_browser to allow_interactive"
```

---

## Task 6: Update the CLI (remove Chromium, fix Garmin status text)

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Delete `_ensure_chromium` and its call**

Delete the entire `_ensure_chromium` function (the `def _ensure_chromium() -> None:` block, currently around lines 120-137).

Find its call site, currently:
```python
    # Only install Chromium if Garmin is configured
    has_garmin = any(u.garmin for u in config.users)
    if has_garmin:
        _ensure_chromium()
```
Delete those four lines. (`has_garmin` is recomputed later where the first-run summary needs it; verify with `grep -n "has_garmin" eufy_sync/cli.py` and, if a later use remains, leave that later definition intact. If `has_garmin` is referenced only after this point, add `has_garmin = any(u.garmin for u in config.users)` at the spot it is first used.)

- [ ] **Step 2: Remove the wizard browser message**

Delete the line (currently ~line 233):
```python
        print("A browser window will open for Garmin login.")
```
and the surrounding `if garmin_email:` if that print was its only body. (Check: `sed -n '230,236p' eufy_sync/cli.py`. Keep any unrelated lines.)

- [ ] **Step 3: Fix the Garmin branch of `_reauth`**

The block currently reads:
```python
        if force:
            status = auth.token_status()
            if status["state"] == "valid":
                print(f"Garmin tokens are still valid ({status['days_remaining']}d remaining). Re-authenticate anyway? [y/N] ", end="")
                if sys.stdin.isatty():
                    answer = input().strip()
                    if not answer.lower().startswith("y"):
                        print("Garmin re-auth skipped.")
                        do_garmin = False

        if do_garmin:
            _ensure_chromium()
            auth.force_reauth()
            print("Done - Garmin tokens saved.")
```
Replace it with (no Chromium, no days_remaining reference):
```python
        if force:
            status = auth.token_status()
            if status["state"] == "valid":
                print("Garmin is already connected. Re-authenticate anyway? [y/N] ", end="")
                if sys.stdin.isatty():
                    answer = input().strip()
                    if not answer.lower().startswith("y"):
                        print("Garmin re-auth skipped.")
                        do_garmin = False

        if do_garmin:
            auth.force_reauth()
            print("Done - Garmin tokens saved.")
```

- [ ] **Step 4: Fix the Garmin branch of `_print_summary`**

The block currently reads:
```python
    if user.garmin:
        from eufy_sync.garmin_auth import GarminAuth
        status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
        if status["state"] == "expired":
            parts.append("Garmin token EXPIRED")
        elif status["state"] == "refresh_needed":
            parts.append(f"token refresh pending ({status['days_remaining']}d until re-login)")
        elif status["days_remaining"] is not None:
            parts.append(f"Garmin token valid {status['days_remaining']}d")
```
Replace it with:
```python
    if user.garmin:
        from eufy_sync.garmin_auth import GarminAuth
        status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
        if status["state"] == "valid":
            parts.append("Garmin connected")
        else:
            parts.append("Garmin not connected")
```

- [ ] **Step 5: Fix the Garmin branch of `_show_status`**

The block currently reads:
```python
        # Garmin token health
        if user.garmin:
            from eufy_sync.garmin_auth import GarminAuth
            status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
            if status["state"] == "expired":
                print("Garmin auth: EXPIRED - browser re-login needed")
            elif status["state"] == "refresh_needed":
                print(f"Garmin auth: access token expired, will refresh on next sync ({status['days_remaining']}d until re-login)")
            elif status["state"] == "valid":
                print(f"Garmin auth: valid ({status['days_remaining']} days until re-login needed)")
            else:
                print("Garmin auth: no saved session - first run will open browser")
```
Replace it with:
```python
        # Garmin token health
        if user.garmin:
            from eufy_sync.garmin_auth import GarminAuth
            status = GarminAuth(user.garmin.email, user.garmin.password).token_status()
            if status["state"] == "valid":
                print("Garmin auth: valid (auto-refreshes; re-login only if it expires)")
            else:
                print("Garmin auth: not connected - run: eufy-sync --reauth")
```

- [ ] **Step 6: Run the suite and a smoke check**

Run:
```bash
~/.local/bin/uv run pytest -q
~/.local/bin/uv run python -c "import eufy_sync.cli"
~/.local/bin/uv run eufy-sync --help
```
Expected: tests green; import clean; `--help` runs (no `_ensure_chromium` reference error). Confirm no stray `_ensure_chromium` remains: `grep -n "_ensure_chromium" eufy_sync/cli.py` returns nothing.

- [ ] **Step 7: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): drop Chromium install and browser-login text; simplify Garmin status"
```

---

## Task 7: Rewrite the README Garmin section

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Rewrite "How Garmin login works"**

Writing rules: no em-dashes; no marketing filler ("delve", "showcase", "seamless", "robust", "leverage", "enhance", "ensure"); plain, specific prose; sentence case.

Replace the current section:
```
## How Garmin login works

Garmin has no official API for writing body composition into Connect, and in March 2026 it put Cloudflare in front of its login, which broke the Python libraries that talked to it. [garth](https://github.com/matin/garth) was [deprecated](https://github.com/matin/garth/discussions/222) and stays that way. [python-garminconnect](https://github.com/cyberjunky/python-garminconnect) has since recovered with a browser-free workaround.

eufy-sync currently handles the login with Playwright: on first run a Chromium window opens and you log in normally. OAuth2 tokens are saved to your system keychain and refresh on their own for about a year, so after that first login no browser is needed. Body composition then goes up as FIT files through Garmin's upload endpoint.
```
with:
```
## How Garmin login works

Garmin has no official API for writing body composition into Connect. In March 2026 it put Cloudflare in front of its login, which broke the Python libraries that talked to it; [garth](https://github.com/matin/garth) was [deprecated](https://github.com/matin/garth/discussions/222) and stays that way.

eufy-sync logs in through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), which gets past Cloudflare without a browser. On first run you enter your Garmin email and password in the terminal, and a code if your account uses two-factor. Tokens are saved to your system keychain and refresh on their own, so later runs need no login. Body composition is uploaded through the same library.
```

- [ ] **Step 2: Check the macOS-only note still fits**

`grep -n "macOS only\|Chromium\|browser" README.md`. If any remaining line implies a browser is needed for Garmin, update it to match the terminal login. Leave unrelated content alone.

- [ ] **Step 3: Commit**

```bash
git add README.md
git commit -m "docs: describe the browser-free Garmin login"
```

---

## Final Verification

- [ ] **Step 1: Full suite**

Run: `~/.local/bin/uv run pytest -v`
Expected: all pass. `test_fit.py` is gone; `test_garmin_auth.py` and `test_garmin_client.py` are green.

- [ ] **Step 2: No Playwright or FIT references remain**

Run:
```bash
grep -rn "playwright\|Playwright\|FitEncoder\|from eufy_sync.fit\|_ensure_chromium" eufy_sync tests
```
Expected: no matches.

- [ ] **Step 3: CLI smoke**

```bash
~/.local/bin/uv run eufy-sync --help
~/.local/bin/uv run eufy-sync --version
```
Expected: both print without traceback.

- [ ] **Step 4: Push and open a PR**

```bash
git push -u origin garmin-auth-swap
gh pr create --title "Swap Garmin auth from Playwright to python-garminconnect" --body "$(cat <<'EOF'
## Summary
Replaces the Playwright browser login with the browser-free python-garminconnect library (curl_cffi). The library owns login, token refresh, and the body-composition upload; eufy-sync keeps the same-date duplicate check.

- Removes Playwright/Chromium; adds garminconnect + curl_cffi.
- Deletes the hand-rolled FIT encoder (the library builds the FIT file).
- Terminal MFA prompt for interactive runs; headless runs that need a fresh login fail with "run --reauth" instead of hanging (also fixes the latent headless 401 bug).
- One-time re-login for existing users (old session is treated as absent).

See docs/superpowers/specs/2026-06-18-garmin-auth-curl-cffi-design.md.

## Test plan
- [x] pytest green; library mocked, no network
- [ ] Manual: fresh `eufy-sync` login against a real Garmin account (incl. MFA), confirm a weigh-in lands in Garmin Connect
- [ ] Manual: `--headless` with no token fails cleanly with the reauth message
EOF
)"
```

- [ ] **Step 5: Watch CI, squash-merge, clean up**

```bash
gh pr checks <N> --watch
gh api -X PUT /repos/sturimcode/eufy-sync/pulls/<N>/merge -f merge_method=squash
gh api -X DELETE /repos/sturimcode/eufy-sync/git/refs/heads/garmin-auth-swap
```

> Note: this branch keeps the package version unchanged. Cutting the release (and the PyPI README refresh) is a separate decision after merge.
