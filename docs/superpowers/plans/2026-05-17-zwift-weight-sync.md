# Zwift Weight Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Zwift as a third sync target so a Eufy weight measurement updates the user's Zwift profile alongside Garmin Connect and Strava.

**Architecture:** New `ZwiftClient` mirrors the existing Strava module (Keycloak password-grant + refresh-token auth, keychain-backed token storage). One `PUT /api/profiles/me` per sync uploads the newest measurement, with per-target failure isolation so a Zwift outage cannot block the other targets. `sync_user` returns `(counts, errors)` instead of just `counts` so the CLI summary can report per-target failures.

**Tech Stack:** Python 3.12+, httpx, keyring, sqlite3, pytest. Same surface as the rest of `eufy-sync`.

**Source spec:** `docs/superpowers/specs/2026-05-17-zwift-weight-sync-design.md`

---

## File Structure

**Create:**
- `eufy_sync/zwift_client.py` — `ZwiftClient` + token I/O helpers (parallel to `strava_client.py`)
- `tests/test_zwift_client.py` — unit tests for the new module

**Modify:**
- `eufy_sync/config.py` — add `ZwiftConfig`, `UserConfig.zwift`, extend `load_config` and the "no sync targets" guard
- `eufy_sync/sync.py` — change `sync_user` return type from `dict[str, int]` to `tuple[dict[str, int], dict[str, str]]`, add Zwift block with per-target try/except
- `eufy_sync/cli.py` — handle new return shape, add wizard prompt, add `--setup-zwift`, extend `--reauth`, `--update-password`, `--status`, `_uninstall`
- `tests/test_sync.py` — three-target ordering test, Zwift isolation test, one-PUT-per-sync test
- `tests/test_config.py` — Zwift parsing test
- `tests/test_summary.py` — update for new return-shape consumers if needed
- `tests/test_cli.py` — `--setup-zwift` interactive test
- `README.md` — short "Sync targets" section noting Zwift is unofficial

---

## Task 1: Refactor sync_user return shape

Lock in the `(counts, errors)` tuple before adding the new target so we never have a half-migrated state.

**Files:**
- Modify: `eufy_sync/sync.py`
- Modify: `eufy_sync/cli.py`
- Modify: `tests/test_sync.py`

- [ ] **Step 1: Update the existing chronological-order test to expect the new return shape**

In `tests/test_sync.py`, change `test_strava_receives_measurements_in_chronological_order` to unpack the tuple:

```python
with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
     patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
     patch("eufy_sync.sync.time.sleep"):
    counts, errors = sync_user(user, state, backfill_days=7)

assert errors == {}, f"expected no errors, got {errors}"
```

- [ ] **Step 2: Run the test, confirm it fails**

```bash
.venv/bin/python -m pytest tests/test_sync.py::test_strava_receives_measurements_in_chronological_order -xvs
```

Expected: `TypeError: cannot unpack non-iterable dict object` (or similar).

- [ ] **Step 3: Change sync_user signature in `eufy_sync/sync.py`**

In `sync.py`, change the function signature and final return:

```python
def sync_user(user: UserConfig, state: SyncState, backfill_days: int | None = None, headless: bool = False, dry_run: bool = False) -> tuple[dict[str, int], dict[str, str]]:
    """Sync one user's Eufy data to configured targets.

    Returns (counts, errors) where counts maps target name to # of synced
    measurements, and errors maps target name to a failure message for
    targets that failed. The dicts are disjoint - a successful target
    appears only in counts, a failed target only in errors.
    """
```

At the bottom of the function, before the existing `return counts`, add an `errors` dict and return both:

```python
        errors: dict[str, str] = {}
        return counts, errors
```

(The Zwift task will populate `errors`; for now it stays empty.)

- [ ] **Step 4: Update cli.py to unpack the new shape**

In `eufy_sync/cli.py`, the sync loop in `main()` calls `sync_user` in TWO places: the main attempt, and the retry inside the `except AmbiguousProfileError` block (the inline profile picker added the second one). Update BOTH.

Main attempt currently reads:

```python
                counts = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                for target_name, count in counts.items():
                    total_counts[target_name] = total_counts.get(target_name, 0) + count
                logger.info("User %s: synced %s", user.name, counts)
```

Replace with:

```python
                counts, errors = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                for target_name, count in counts.items():
                    total_counts[target_name] = total_counts.get(target_name, 0) + count
                for target_name, err in errors.items():
                    failures.append((f"{user.name}/{target_name}", err))
                logger.info("User %s: synced %s, errors %s", user.name, counts, errors)
```

The retry inside `except AmbiguousProfileError` currently reads:

```python
                        counts = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                        for target_name, count in counts.items():
                            total_counts[target_name] = total_counts.get(target_name, 0) + count
                        logger.info("User %s: synced %s", user.name, counts)
```

Replace with:

```python
                        counts, errors = sync_user(user, state, backfill_days=backfill, headless=args.headless, dry_run=args.dry_run)
                        for target_name, count in counts.items():
                            total_counts[target_name] = total_counts.get(target_name, 0) + count
                        for target_name, err in errors.items():
                            failures.append((f"{user.name}/{target_name}", err))
                        logger.info("User %s: synced %s, errors %s", user.name, counts, errors)
```

- [ ] **Step 5: Run the full suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add eufy_sync/sync.py eufy_sync/cli.py tests/test_sync.py
git commit -m "refactor: sync_user returns (counts, errors) for per-target failure reporting"
```

---

## Task 2: Add ZwiftConfig to config.py

**Files:**
- Modify: `eufy_sync/config.py`
- Create test in: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_with_zwift_only(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "zwift": {"email": "z@example.com", "password": "zwiftpw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].zwift is not None
    assert cfg.users[0].zwift.email == "z@example.com"
    assert cfg.users[0].zwift.password == "zwiftpw"
    assert cfg.users[0].garmin is None
    assert cfg.users[0].strava is None


@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_load_config_with_all_three_targets(_keyring, tmp_path: Path):
    path = tmp_path / "config.yaml"
    _write(path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
            "strava": {"client_id": "12345", "client_secret": "ssec"},
            "zwift": {"email": "z@example.com", "password": "zwiftpw"},
        }],
    })
    cfg = load_config(path)
    assert cfg.users[0].garmin is not None
    assert cfg.users[0].strava is not None
    assert cfg.users[0].zwift is not None
```

- [ ] **Step 2: Run, confirm failure**

```bash
.venv/bin/python -m pytest tests/test_config.py::test_load_config_with_zwift_only -xvs
```

Expected: `AttributeError: 'UserConfig' object has no attribute 'zwift'` (or `KeyError`).

- [ ] **Step 3: Add ZwiftConfig dataclass and UserConfig.zwift field**

In `eufy_sync/config.py`, after the `StravaConfig` dataclass, add:

```python
@dataclass
class ZwiftConfig:
    email: str
    password: str
```

In the `UserConfig` dataclass, add the field:

```python
@dataclass
class UserConfig:
    name: str
    eufy: EufyConfig
    garmin: GarminConfig | None = None
    strava: StravaConfig | None = None
    zwift: ZwiftConfig | None = None
```

In `load_config`, after the Strava block and before the "no sync targets" guard, add:

```python
        zwift = None
        if "zwift" in u:
            zwift = ZwiftConfig(
                email=u["zwift"]["email"],
                password=_get_password(name, "zwift", u["zwift"]["email"], u["zwift"].get("password")),
            )

        if not garmin and not strava and not zwift:
            raise ValueError(
                f"User '{name}' has no sync targets configured. "
                f"Add a 'garmin', 'strava', and/or 'zwift' section to your config."
            )
```

Delete the existing `if not garmin and not strava:` guard (replaced by the three-way guard above).

Append `zwift=zwift,` to the `UserConfig(...)` call:

```python
        users.append(UserConfig(
            name=name,
            eufy=EufyConfig(
                email=u["eufy"]["email"],
                password=_get_password(name, "eufy", u["eufy"]["email"], u["eufy"].get("password")),
            ),
            garmin=garmin,
            strava=strava,
            zwift=zwift,
        ))
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_config.py -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/config.py tests/test_config.py
git commit -m "feat(config): add ZwiftConfig and UserConfig.zwift field"
```

---

## Task 3: ZwiftClient skeleton with token I/O helpers

Token persistence + dataclass shape, no network yet.

**Files:**
- Create: `eufy_sync/zwift_client.py`
- Create: `tests/test_zwift_client.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_zwift_client.py`:

```python
from __future__ import annotations

import time
from unittest.mock import patch

from eufy_sync.config import ZwiftConfig
from eufy_sync.zwift_client import ZwiftClient, _load_tokens, _save_tokens


def _make_config():
    return ZwiftConfig(email="z@example.com", password="pw")


def _make_tokens(expired: bool = False):
    return {
        "access_token": "old" if expired else "valid",
        "refresh_token": "rtok",
        "expires_at": time.time() + (-3600 if expired else 3600),
    }


def test_save_and_load_tokens_roundtrip_file_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    tokens = _make_tokens()
    _save_tokens(tokens)
    loaded = _load_tokens()
    assert loaded["access_token"] == "valid"
    assert loaded["refresh_token"] == "rtok"


def test_load_tokens_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _load_tokens() is None
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py -xvs
```

Expected: `ImportError: cannot import name 'ZwiftClient'`.

- [ ] **Step 3: Create the module**

Write `eufy_sync/zwift_client.py`:

```python
"""Zwift weight sync via reverse-engineered Keycloak OAuth2 password grant.

Zwift does not publish a third-party API. This module talks to the same
endpoints the Companion app uses:
- Auth: POST https://secure.zwift.com/auth/realms/zwift/tokens/access/codes
- Profile write: PUT https://us-or-rly101.zwift.com/api/profiles/me

The endpoints are community-reverse-engineered and may break with any
Zwift release. Failures here should not stop Garmin/Strava sync.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import httpx

from eufy_sync.config import ZwiftConfig

logger = logging.getLogger(__name__)

TOKEN_URL = "https://secure.zwift.com/auth/realms/zwift/tokens/access/codes"
PROFILE_URL = "https://us-or-rly101.zwift.com/api/profiles/me"
CLIENT_ID = "Zwift_Mobile_Link"
REFRESH_SAFETY_MARGIN = 300  # seconds before expiry to trigger refresh


def _keyring_available() -> bool:
    from eufy_sync.credentials import _keyring_available as fn
    return fn()


def _token_file_path() -> Path:
    return Path.home() / ".garmin-sync" / "zwift_token.json"


def _load_tokens() -> dict | None:
    """Load Zwift tokens from keychain or file fallback."""
    if _keyring_available():
        from eufy_sync.credentials import get_token
        data = get_token("zwift")
        if data:
            return data
    path = _token_file_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, TypeError):
            return None
    return None


def _save_tokens(tokens: dict) -> None:
    """Save Zwift tokens to keychain or file fallback."""
    if _keyring_available():
        from eufy_sync.credentials import store_token
        store_token("zwift", tokens)
        path = _token_file_path()
        if path.exists():
            path.unlink()
        return

    path = _token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(tokens, indent=2))


class ZwiftClient:
    """Updates Zwift profile weight via reverse-engineered API.

    See module docstring for stability caveats.
    """

    def __init__(self, config: ZwiftConfig):
        self.config = config
        self._client = httpx.Client(timeout=30.0)
        self._tokens: dict | None = None

    def close(self) -> None:
        self._client.close()
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py -x
```

Expected: both tests pass.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/zwift_client.py tests/test_zwift_client.py
git commit -m "feat(zwift): add ZwiftClient skeleton and token I/O helpers"
```

---

## Task 4: ZwiftClient.authenticate with fresh password-grant login

**Files:**
- Modify: `eufy_sync/zwift_client.py`
- Modify: `tests/test_zwift_client.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_zwift_client.py`:

```python
from unittest.mock import MagicMock


def test_authenticate_fresh_login_when_no_tokens(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "fresh_access",
        "refresh_token": "fresh_refresh",
        "expires_in": 3600,
    }

    client = ZwiftClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response) as mock_post:
        client.authenticate()

    # Called with password grant
    call = mock_post.call_args
    assert call.args[0] == "https://secure.zwift.com/auth/realms/zwift/tokens/access/codes"
    assert call.kwargs["data"]["grant_type"] == "password"
    assert call.kwargs["data"]["username"] == "z@example.com"
    assert call.kwargs["data"]["password"] == "pw"
    assert "Bearer fresh_access" in client._client.headers.get("Authorization", "")
    client.close()
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py::test_authenticate_fresh_login_when_no_tokens -xvs
```

Expected: `AttributeError: 'ZwiftClient' object has no attribute 'authenticate'`.

- [ ] **Step 3: Implement authenticate + _fresh_login**

Append to `ZwiftClient` class in `eufy_sync/zwift_client.py`:

```python
    def authenticate(self) -> None:
        """Load tokens from cache; refresh or fresh-login as needed."""
        self._tokens = _load_tokens()

        if self._tokens is None:
            self._fresh_login()
        elif time.time() >= (self._tokens["expires_at"] - REFRESH_SAFETY_MARGIN):
            self._refresh_access_token()

        self._client.headers["Authorization"] = f"Bearer {self._tokens['access_token']}"

    def _fresh_login(self) -> None:
        """Exchange email+password for tokens via Keycloak password grant."""
        now = time.time()
        resp = self._client.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "password",
                "username": self.config.email,
                "password": self.config.password,
            },
        )
        if resp.status_code != 200:
            logger.debug("Zwift login failed: %d %s", resp.status_code, resp.text)
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError(
                f"Zwift login failed (HTTP {resp.status_code}). "
                f"If you changed your Zwift password, run: eufy-sync --update-password"
            )

        data = resp.json()
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "expires_at": now + data["expires_in"],
        }
        _save_tokens(self._tokens)
        logger.info("Authenticated to Zwift as %s", self.config.email)
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/zwift_client.py tests/test_zwift_client.py
git commit -m "feat(zwift): add password-grant login flow"
```

---

## Task 5: Refresh-token flow when access token is expired

**Files:**
- Modify: `eufy_sync/zwift_client.py`
- Modify: `tests/test_zwift_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_zwift_client.py`:

```python
def test_authenticate_refreshes_expired_token(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens(expired=True))

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "refreshed",
        "refresh_token": "new_rtok",
        "expires_in": 3600,
    }

    client = ZwiftClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response) as mock_post:
        client.authenticate()

    assert mock_post.call_args.kwargs["data"]["grant_type"] == "refresh_token"
    assert mock_post.call_args.kwargs["data"]["refresh_token"] == "rtok"
    assert "Bearer refreshed" in client._client.headers["Authorization"]
    client.close()


def test_authenticate_falls_back_to_password_if_refresh_fails(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens(expired=True))

    refresh_fail = MagicMock(status_code=401, text="refresh dead")
    fresh_ok = MagicMock(status_code=200)
    fresh_ok.json.return_value = {
        "access_token": "fresh_after_refresh_fail",
        "refresh_token": "fresh_rtok",
        "expires_in": 3600,
    }

    client = ZwiftClient(_make_config())
    with patch.object(client._client, "post", side_effect=[refresh_fail, fresh_ok]):
        client.authenticate()
    assert "Bearer fresh_after_refresh_fail" in client._client.headers["Authorization"]
    client.close()
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py::test_authenticate_refreshes_expired_token -xvs
```

Expected: `AttributeError: 'ZwiftClient' object has no attribute '_refresh_access_token'`.

- [ ] **Step 3: Implement _refresh_access_token with password-grant fallback**

Append to `ZwiftClient`:

```python
    def _refresh_access_token(self) -> None:
        """Refresh the access token using the refresh token. Falls back to fresh login."""
        now = time.time()
        resp = self._client.post(
            TOKEN_URL,
            data={
                "client_id": CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": self._tokens["refresh_token"],
            },
        )
        if resp.status_code != 200:
            logger.warning("Zwift refresh failed (%d), falling back to password login", resp.status_code)
            self._tokens = None
            self._fresh_login()
            return

        data = resp.json()
        self._tokens = {
            "access_token": data["access_token"],
            "refresh_token": data.get("refresh_token", self._tokens["refresh_token"]),
            "expires_at": now + data["expires_in"],
        }
        _save_tokens(self._tokens)
        logger.info("Refreshed Zwift access token")
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/zwift_client.py tests/test_zwift_client.py
git commit -m "feat(zwift): add refresh-token flow with password-grant fallback"
```

---

## Task 6: ZwiftClient.update_weight (the actual write)

**Files:**
- Modify: `eufy_sync/zwift_client.py`
- Modify: `tests/test_zwift_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_zwift_client.py`:

```python
import httpx
import pytest
from eufy_sync.sync import PermanentSyncError


def test_update_weight_sends_grams_not_kg(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {"weight": 80000}

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response) as mock_put:
        client.update_weight(80.0)

    # Weight must be sent in grams, JSON body
    payload = mock_put.call_args.kwargs["json"]
    assert payload["weight"] == 80000
    assert mock_put.call_args.args[0] == "https://us-or-rly101.zwift.com/api/profiles/me"
    client.close()


def test_update_weight_rounds_to_grams(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=200)
    mock_response.json.return_value = {}

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response) as mock_put:
        client.update_weight(80.456)

    # 80.456 kg = 80456 g
    assert mock_put.call_args.kwargs["json"]["weight"] == 80456
    client.close()


def test_update_weight_4xx_raises_permanent(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=401, text="unauth")

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response):
        with pytest.raises(PermanentSyncError):
            client.update_weight(80.0)
    client.close()


def test_update_weight_5xx_raises_retryable(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())

    mock_response = MagicMock(status_code=503, text="busy")

    client = ZwiftClient(_make_config())
    client.authenticate()
    with patch.object(client._client, "put", return_value=mock_response):
        with pytest.raises(RuntimeError) as exc_info:
            client.update_weight(80.0)
    assert not isinstance(exc_info.value, PermanentSyncError)
    client.close()
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py::test_update_weight_sends_grams_not_kg -xvs
```

Expected: `AttributeError: 'ZwiftClient' object has no attribute 'update_weight'`.

- [ ] **Step 3: Implement update_weight**

Append to `ZwiftClient`:

```python
    def update_weight(self, weight_kg: float) -> dict:
        """Update Zwift profile weight. Sends grams.

        Returns the response JSON. 4xx raises PermanentSyncError so _retry
        skips it; 5xx and network errors raise RuntimeError so _retry retries.
        """
        weight_g = int(round(weight_kg * 1000))
        resp = self._client.put(PROFILE_URL, json={"weight": weight_g})

        if resp.status_code != 200:
            logger.debug("Zwift weight update failed: %d %s", resp.status_code, resp.text)
            if 400 <= resp.status_code < 500 and resp.status_code != 429:
                from eufy_sync.sync import PermanentSyncError
                raise PermanentSyncError(
                    f"Failed to update Zwift weight (HTTP {resp.status_code})"
                )
            raise RuntimeError(
                f"Failed to update Zwift weight (HTTP {resp.status_code})"
            )

        logger.info("Updated Zwift weight to %.2f kg (%d g)", weight_kg, weight_g)
        return resp.json() if resp.text else {"status": resp.status_code}
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/zwift_client.py tests/test_zwift_client.py
git commit -m "feat(zwift): add update_weight with permanent/retryable classification"
```

---

## Task 7: ZwiftClient.token_status

Needed so the CLI status / summary code can report Zwift health alongside Garmin/Strava.

**Files:**
- Modify: `eufy_sync/zwift_client.py`
- Modify: `tests/test_zwift_client.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_zwift_client.py`:

```python
def test_token_status_no_session(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    client = ZwiftClient(_make_config())
    assert client.token_status()["state"] == "no_session"
    client.close()


def test_token_status_valid(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens())
    client = ZwiftClient(_make_config())
    status = client.token_status()
    assert status["state"] == "valid"
    client.close()


def test_token_status_refresh_needed(monkeypatch, tmp_path):
    monkeypatch.setattr("eufy_sync.zwift_client._keyring_available", lambda: False)
    monkeypatch.setenv("HOME", str(tmp_path))
    _save_tokens(_make_tokens(expired=True))
    client = ZwiftClient(_make_config())
    assert client.token_status()["state"] == "refresh_needed"
    client.close()
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py::test_token_status_no_session -xvs
```

Expected: `AttributeError: 'ZwiftClient' object has no attribute 'token_status'`.

- [ ] **Step 3: Implement token_status**

Append to `ZwiftClient`:

```python
    def token_status(self) -> dict:
        """Return token health matching the shape of StravaClient.token_status."""
        tokens = _load_tokens()
        if tokens is None:
            return {"state": "no_session", "days_remaining": None}
        if "refresh_token" not in tokens or not tokens["refresh_token"]:
            return {"state": "expired", "days_remaining": 0}
        if time.time() >= (tokens["expires_at"] - REFRESH_SAFETY_MARGIN):
            return {"state": "refresh_needed", "days_remaining": None}
        hours = int((tokens["expires_at"] - time.time()) / 3600)
        return {"state": "valid", "days_remaining": None, "hours_remaining": hours}
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/test_zwift_client.py -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/zwift_client.py tests/test_zwift_client.py
git commit -m "feat(zwift): add token_status matching Strava/Garmin shape"
```

---

## Task 8: Wire Zwift into sync_user with failure isolation

**Files:**
- Modify: `eufy_sync/sync.py`
- Modify: `tests/test_sync.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_sync.py`:

```python
from eufy_sync.config import ZwiftConfig


def test_zwift_gets_exactly_one_put_per_sync(tmp_path: Path):
    """Zwift's profile endpoint is heavy - we update once per sync, not once per measurement."""
    state = SyncState(tmp_path / "test.db")

    measurements = [
        _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)),
        _measurement(85.5, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc)),
        _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)),
    ]

    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        zwift=ZwiftConfig(email="z@example.com", password="zpw"),
    )

    fake_eufy = MagicMock()
    fake_eufy.fetch_measurements.return_value = list(measurements)

    fake_zwift = MagicMock()
    fake_zwift.update_weight.return_value = {"weight": 86000}

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=fake_zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert fake_zwift.update_weight.call_count == 1, "Zwift should be PUT exactly once per sync"
    assert fake_zwift.update_weight.call_args.args[0] == 86.0, "Should send the newest weight"
    assert counts["zwift"] == 1
    assert errors == {}
    # All three measurements must be marked synced for Zwift
    for m in measurements:
        assert state.is_synced("default", m.measurement_id, "zwift")
    state.close()


def test_zwift_failure_does_not_block_strava(tmp_path: Path):
    """A Zwift exception must not prevent Strava sync from completing."""
    state = SyncState(tmp_path / "test.db")

    measurement = _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc))

    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        strava=StravaConfig(client_id="cid", client_secret="csec"),
        zwift=ZwiftConfig(email="z@example.com", password="zpw"),
    )

    fake_eufy = MagicMock()
    fake_eufy.fetch_measurements.return_value = [measurement]

    fake_strava = MagicMock()
    fake_strava.update_weight.return_value = {"weight": 86}

    fake_zwift = MagicMock()
    fake_zwift.authenticate.side_effect = RuntimeError("Zwift exploded")

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=fake_zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert counts.get("strava") == 1, "Strava must have succeeded"
    assert "zwift" in errors, f"Zwift error must be reported; got {errors}"
    assert "Zwift exploded" in errors["zwift"]
    state.close()
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_sync.py::test_zwift_gets_exactly_one_put_per_sync -xvs
```

Expected: `AssertionError: assert 0 == 1` (no Zwift call made) or similar.

- [ ] **Step 3: Add Zwift block to sync_user**

In `eufy_sync/sync.py`, modify `sync_user`. The current return block at the end of the function looks like:

```python
        errors: dict[str, str] = {}
        return counts, errors
```

Replace with a Zwift block immediately before the `return`:

```python
        errors: dict[str, str] = {}

        if user.zwift:
            try:
                from eufy_sync.zwift_client import ZwiftClient
                zwift = ZwiftClient(user.zwift)
                try:
                    zwift.authenticate()
                    unsynced = [
                        m for m in measurements
                        if transform(m) is not None
                        and not state.is_synced(user.name, m.measurement_id, "zwift")
                    ]
                    if unsynced:
                        newest = unsynced[-1]  # measurements sorted ascending earlier
                        if dry_run:
                            logger.info("[DRY RUN] Would sync to zwift: %.1f kg", newest.weight_kg)
                            counts["zwift"] = 1
                        else:
                            result = _retry(
                                lambda: zwift.update_weight(newest.weight_kg),
                                f"Zwift update ({newest.measurement_id})",
                            )
                            for m in unsynced:
                                state.record_sync(
                                    user_name=user.name,
                                    measurement_id=m.measurement_id,
                                    measurement_timestamp=m.timestamp.isoformat(),
                                    weight_kg=m.weight_kg,
                                    synced_at=datetime.now(timezone.utc).isoformat(),
                                    target="zwift",
                                    response=json.dumps(result) if m is newest else None,
                                )
                            counts["zwift"] = 1
                            lb = newest.weight_kg * 2.20462
                            logger.info(
                                "Synced %.2f kg (%.1f lb) -> Zwift (weight only)",
                                newest.weight_kg, lb,
                            )
                finally:
                    zwift.close()
            except Exception as e:
                logger.exception("Zwift sync failed; continuing")
                errors["zwift"] = str(e)

        return counts, errors
```

- [ ] **Step 4: Run tests**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/sync.py tests/test_sync.py
git commit -m "feat(sync): wire Zwift into sync_user with failure isolation"
```

---

## Task 9: First-run wizard prompt for Zwift

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Add the Zwift block to _first_run_setup**

In `eufy_sync/cli.py`, find the Strava block in `_first_run_setup`:

```python
    # Strava setup (optional)
    print("")
    strava_answer = input("Connect Strava? [y/N] ").strip()
    strava_config = None
    if strava_answer.lower().startswith("y"):
        strava_config = _prompt_strava_credentials()
```

After it, add the Zwift block:

```python
    # Zwift setup (optional, unofficial)
    print("")
    zwift_answer = input("Connect Zwift? [y/N] ").strip()
    zwift_email = None
    zwift_password = None
    if zwift_answer.lower().startswith("y"):
        print("")
        print("  Note: Zwift has no official API. eufy-sync uses a")
        print("  community-reverse-engineered endpoint and may break with any Zwift update.")
        print("")
        zwift_email = input("Zwift email (Enter if same as Eufy): ").strip()
        if not zwift_email:
            zwift_email = eufy_email
        zwift_password = getpass.getpass("Zwift password: ")
        if not zwift_password:
            print("Error: Zwift password is required.")
            sys.exit(1)
```

Find the "at least one sync target" guard:

```python
    if not garmin_email and not strava_config:
        print("Error: You must configure at least one sync target (Garmin or Strava).")
        sys.exit(1)
```

Replace with:

```python
    if not garmin_email and not strava_config and not zwift_email:
        print("Error: You must configure at least one sync target (Garmin, Strava, or Zwift).")
        sys.exit(1)
```

Find the keychain-storage call:

```python
    keychain_ok = _store_passwords_in_keychain(user_name, eufy_password, garmin_password)
```

Change `_store_passwords_in_keychain` signature to accept zwift_password. Find the function definition:

```python
def _store_passwords_in_keychain(user_name: str, eufy_password: str, garmin_password: str | None = None) -> bool:
```

Replace with:

```python
def _store_passwords_in_keychain(
    user_name: str,
    eufy_password: str,
    garmin_password: str | None = None,
    zwift_password: str | None = None,
) -> bool:
    """Store passwords in keychain. Returns True if successful."""
    from eufy_sync.credentials import store_password, _keyring_available
    if not _keyring_available():
        return False
    store_password(f"{user_name}:eufy", eufy_password)
    if garmin_password:
        store_password(f"{user_name}:garmin", garmin_password)
    if zwift_password:
        store_password(f"{user_name}:zwift", zwift_password)
    return True
```

Update the call inside `_first_run_setup`:

```python
    keychain_ok = _store_passwords_in_keychain(user_name, eufy_password, garmin_password, zwift_password)
```

Find the config-building block:

```python
    user_config: dict = {
        "name": user_name,
        "eufy": {"email": eufy_email},
    }
    if garmin_email:
        user_config["garmin"] = {"email": garmin_email}
    if strava_config:
        user_config["strava"] = strava_config
    if not keychain_ok:
        # Fallback: store passwords in config file (with 0o600 permissions)
        user_config["eufy"]["password"] = eufy_password
        if garmin_password:
            user_config["garmin"]["password"] = garmin_password
        print("Warning: keychain not available, passwords stored in config file.")
    else:
        print("Passwords saved to system keychain.")
```

Add Zwift to both branches:

```python
    user_config: dict = {
        "name": user_name,
        "eufy": {"email": eufy_email},
    }
    if garmin_email:
        user_config["garmin"] = {"email": garmin_email}
    if strava_config:
        user_config["strava"] = strava_config
    if zwift_email:
        user_config["zwift"] = {"email": zwift_email}
    if not keychain_ok:
        # Fallback: store passwords in config file (with 0o600 permissions)
        user_config["eufy"]["password"] = eufy_password
        if garmin_password:
            user_config["garmin"]["password"] = garmin_password
        if zwift_password:
            user_config["zwift"]["password"] = zwift_password
        print("Warning: keychain not available, passwords stored in config file.")
    else:
        print("Passwords saved to system keychain.")
```

Update the "targets:" list:

```python
    targets = []
    if garmin_email:
        targets.append("Garmin")
    if strava_config:
        targets.append("Strava")
    if zwift_email:
        targets.append("Zwift")
    print(f"Saved. Running first sync to {' and '.join(targets)} (last 7 days)...")
```

- [ ] **Step 2: Run the suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green (no test changes needed for wizard prose).

- [ ] **Step 3: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): add Zwift prompt to first-run wizard"
```

---

## Task 10: --setup-zwift flag

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Add _setup_zwift function**

In `eufy_sync/cli.py`, after `_setup_strava`, add:

```python
def _setup_zwift(config_path: Path) -> None:
    """Add or update Zwift configuration."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    with open(config_path) as f:
        config = yaml.safe_load(f)

    print("")
    print("  Note: Zwift has no official API. eufy-sync uses a")
    print("  community-reverse-engineered endpoint and may break with any Zwift update.")
    print("")

    user = config["users"][0]
    user_name = user.get("name", "default")
    eufy_email = user["eufy"]["email"]

    zwift_email = input("Zwift email (Enter if same as Eufy): ").strip() or eufy_email
    zwift_password = getpass.getpass("Zwift password: ")
    if not zwift_password:
        print("Error: Zwift password is required.")
        sys.exit(1)

    from eufy_sync.credentials import store_password, _keyring_available
    if _keyring_available():
        store_password(f"{user_name}:zwift", zwift_password)
        user["zwift"] = {"email": zwift_email}
    else:
        user["zwift"] = {"email": zwift_email, "password": zwift_password}

    _write_config(config_path, config)
    print("Zwift connected. Future syncs will update your Zwift profile weight.")
```

- [ ] **Step 2: Wire the flag into argparse and main dispatch**

Find the argparse block in `main()`:

```python
    parser.add_argument("--setup-strava", action="store_true", help="Connect Strava to your account")
```

Add right after it:

```python
    parser.add_argument("--setup-zwift", action="store_true", help="Connect Zwift to your account")
```

Find the dispatch block:

```python
    # Handle Strava setup
    if args.setup_strava:
        _setup_strava(config_path)
        return
```

Add right after it:

```python
    # Handle Zwift setup
    if args.setup_zwift:
        _setup_zwift(config_path)
        return
```

- [ ] **Step 3: Run the suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): add --setup-zwift flag"
```

---

## Task 11: Extend --reauth to handle zwift target

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Add do_zwift alongside do_garmin/do_strava and update the guard**

In `eufy_sync/cli.py`, find this block in `_reauth`:

```python
    do_garmin = (target is None or target == "garmin") and "garmin" in user
    do_strava = (target is None or target == "strava") and "strava" in user

    if target and not do_garmin and not do_strava:
        print(f"Target '{target}' is not configured. Check your config.")
        return
```

Replace with:

```python
    do_garmin = (target is None or target == "garmin") and "garmin" in user
    do_strava = (target is None or target == "strava") and "strava" in user
    do_zwift = (target is None or target == "zwift") and "zwift" in user

    if target and not do_garmin and not do_strava and not do_zwift:
        print(f"Target '{target}' is not configured. Check your config.")
        return
```

- [ ] **Step 2: Add the Zwift re-auth branch at the bottom of _reauth**

After the existing Strava block:

```python
    if do_strava:
        from eufy_sync.config import StravaConfig
        from eufy_sync.strava_client import authorize_strava
        strava_cfg = StravaConfig(
            client_id=str(user["strava"]["client_id"]),
            client_secret=user["strava"]["client_secret"],
        )
        authorize_strava(strava_cfg)
        print("Done - Strava tokens saved.")
```

Add:

```python
    if do_zwift:
        from eufy_sync.config import ZwiftConfig, _get_password
        from eufy_sync.credentials import _keyring_available, delete_token
        from eufy_sync.zwift_client import ZwiftClient
        zwift_email = user["zwift"]["email"]
        zwift_pw = _get_password(user_name, "zwift", zwift_email, user["zwift"].get("password"))
        # Force fresh password-grant login by clearing cached tokens
        if _keyring_available():
            delete_token("zwift")
        token_file = DATA_DIR / "zwift_token.json"
        if token_file.exists():
            token_file.unlink()
        client = ZwiftClient(ZwiftConfig(email=zwift_email, password=zwift_pw))
        client.authenticate()
        client.close()
        print("Done - Zwift tokens saved.")
```

- [ ] **Step 3: Run the suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 4: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): extend --reauth to handle zwift target"
```

---

## Task 12: Extend --update-password for Zwift

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Update _update_password to prompt for Zwift**

In `eufy_sync/cli.py`, find `_update_password`. After the Garmin prompt:

```python
    eufy_pw = getpass.getpass("New Eufy password: ")
    garmin_pw = getpass.getpass("New Garmin password: ")
```

Change to include Zwift:

```python
    eufy_pw = getpass.getpass("New Eufy password: ")
    garmin_pw = getpass.getpass("New Garmin password: ") if "garmin" in user else ""
    zwift_pw = getpass.getpass("New Zwift password: ") if "zwift" in user else ""
```

Update the early-exit:

```python
    if not eufy_pw and not garmin_pw:
        print("No changes made.")
        return
```

To:

```python
    if not eufy_pw and not garmin_pw and not zwift_pw:
        print("No changes made.")
        return
```

After the Garmin block:

```python
    if garmin_pw:
        if keychain_ok:
            store_password(f"{user_name}:garmin", garmin_pw)
        else:
            user["garmin"]["password"] = garmin_pw
```

Add the Zwift block:

```python
    if zwift_pw:
        if keychain_ok:
            store_password(f"{user_name}:zwift", zwift_pw)
        else:
            user["zwift"]["password"] = zwift_pw
```

After the Garmin token-clearing block:

```python
    if garmin_pw:
        if keychain_ok:
            delete_token("garmin")
        garmin_session = DATA_DIR / "session.json"
        if garmin_session.exists():
            garmin_session.unlink()
```

Add the Zwift block:

```python
    if zwift_pw:
        if keychain_ok:
            delete_token("zwift")
        zwift_token = DATA_DIR / "zwift_token.json"
        if zwift_token.exists():
            zwift_token.unlink()
```

Update the "changed" list:

```python
    changed = []
    if eufy_pw:
        changed.append("Eufy")
    if garmin_pw:
        changed.append("Garmin")
```

To:

```python
    changed = []
    if eufy_pw:
        changed.append("Eufy")
    if garmin_pw:
        changed.append("Garmin")
    if zwift_pw:
        changed.append("Zwift")
```

- [ ] **Step 2: Run the suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): extend --update-password to include Zwift"
```

---

## Task 13: Extend --status to report Zwift health

**Files:**
- Modify: `eufy_sync/cli.py`

- [ ] **Step 1: Update _show_status and _print_summary**

In `eufy_sync/cli.py`, find the Strava section of `_show_status`:

```python
        # Strava token health
        if user.strava:
            from eufy_sync.strava_client import StravaClient
            strava_status = StravaClient(user.strava).token_status()
            if strava_status["state"] == "expired":
                print("Strava auth: EXPIRED - re-authorize with --reauth strava")
            elif strava_status["state"] == "no_session":
                print("Strava auth: not authorized - run --setup-strava")
            elif strava_status["state"] == "refresh_needed":
                print("Strava auth: access token expired, will refresh on next sync")
            else:
                print("Strava auth: valid (refresh token active)")
```

Add the Zwift block right after:

```python
        # Zwift token health (unofficial)
        if user.zwift:
            from eufy_sync.zwift_client import ZwiftClient
            zwift_status = ZwiftClient(user.zwift).token_status()
            if zwift_status["state"] == "no_session":
                print("Zwift auth: not authorized - first sync will log in")
            elif zwift_status["state"] == "expired":
                print("Zwift auth: EXPIRED - re-authorize with --reauth zwift")
            elif zwift_status["state"] == "refresh_needed":
                print("Zwift auth: access token expired, will refresh on next sync")
            else:
                print("Zwift auth: valid (refresh token active)")
```

In `_print_summary`, find the Strava section:

```python
    if user.strava:
        from eufy_sync.strava_client import StravaClient
        strava_status = StravaClient(user.strava).token_status()
        if strava_status["state"] == "expired":
            parts.append("Strava token EXPIRED")
        elif strava_status["state"] == "no_session":
            parts.append("Strava: not authorized")
        elif strava_status["state"] == "refresh_needed":
            parts.append("Strava token refresh pending")
        else:
            parts.append("Strava connected")
```

Add the Zwift block right after:

```python
    if user.zwift:
        from eufy_sync.zwift_client import ZwiftClient
        zwift_status = ZwiftClient(user.zwift).token_status()
        if zwift_status["state"] == "no_session":
            parts.append("Zwift: not authorized")
        elif zwift_status["state"] == "expired":
            parts.append("Zwift token EXPIRED")
        elif zwift_status["state"] == "refresh_needed":
            parts.append("Zwift token refresh pending")
        else:
            parts.append("Zwift connected")
```

In the success-notification block of `main()`, find:

```python
        if total > 0:
            target_label = " and ".join(n.capitalize() for n in total_counts if total_counts[n] > 0)
            _notify("eufy-sync", f"Synced {total} measurement{'s' if total != 1 else ''} to {target_label}")
```

No change needed here — it already iterates whatever is in `total_counts`. Zwift entries appear automatically.

- [ ] **Step 2: Run the suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 3: Commit**

```bash
git add eufy_sync/cli.py
git commit -m "feat(cli): report Zwift health in --status and sync summary"
```

---

## Task 14: --uninstall clears Zwift keychain + token file

**Files:**
- Modify: `eufy_sync/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Update the existing _uninstall test to expect Zwift keychain cleanup**

In `tests/test_cli.py`, find `test_uninstall_clears_keychain_for_configured_user_name`. Add Zwift to the config:

```python
    _write_config(config_path, {
        "users": [{
            "name": "elias",
            "eufy": {"email": "e@example.com"},
            "garmin": {"email": "g@example.com"},
            "zwift": {"email": "z@example.com"},
        }],
    })
```

And assert Zwift is deleted:

```python
    assert "elias:eufy" in deleted_accounts
    assert "elias:garmin" in deleted_accounts
    assert "elias:zwift" in deleted_accounts
```

- [ ] **Step 2: Run, confirm fail**

```bash
.venv/bin/python -m pytest tests/test_cli.py::test_uninstall_clears_keychain_for_configured_user_name -xvs
```

Expected: `AssertionError: assert 'elias:zwift' in {'elias:eufy', 'elias:garmin'}`.

- [ ] **Step 3: Update _uninstall**

In `eufy_sync/cli.py`, find the keychain cleanup in `_uninstall`:

```python
    from eufy_sync.credentials import delete_password, delete_token, _keyring_available
    if _keyring_available():
        for name in user_names:
            for suffix in ["eufy", "garmin"]:
                delete_password(f"{name}:{suffix}")
        delete_token("eufy")
        delete_token("garmin")
        delete_token("strava")
```

Replace with:

```python
    from eufy_sync.credentials import delete_password, delete_token, _keyring_available
    if _keyring_available():
        for name in user_names:
            for suffix in ["eufy", "garmin", "zwift"]:
                delete_password(f"{name}:{suffix}")
        delete_token("eufy")
        delete_token("garmin")
        delete_token("strava")
        delete_token("zwift")
```

- [ ] **Step 4: Run the suite**

```bash
.venv/bin/python -m pytest tests/ -x
```

Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add eufy_sync/cli.py tests/test_cli.py
git commit -m "feat(cli): clear Zwift keychain entries on --uninstall"
```

---

## Task 15: README update

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add Zwift to the opening description and a "Sync targets" section**

In `README.md`, find the opening description:

```
Syncs body composition data from a Eufy smart scale to Garmin Connect and/or Strava. Weight, body fat %, muscle mass, bone mass, hydration, BMR, visceral fat, and metabolic age all come through to Garmin. Strava gets weight updates.
```

Replace with:

```
Syncs body composition data from a Eufy smart scale to Garmin Connect, Strava, and Zwift. Weight, body fat %, muscle mass, bone mass, hydration, BMR, visceral fat, and metabolic age all come through to Garmin. Strava and Zwift get weight updates.
```

After the "## The problem" section, add a new section:

```
## Sync targets

| Target | What gets synced | API stability |
|--------|------------------|---------------|
| Garmin Connect | Full body composition (FIT upload) | Stable |
| Strava | Current weight (`PUT /athlete`) | Stable |
| Zwift | Current weight (`PUT /api/profiles/me`) | Unofficial, may break |

Zwift has no public API for third-party tools. eufy-sync uses a community-reverse-engineered endpoint that could change with any Zwift release. If Zwift breaks, the other two targets keep working.
```

In the "## Usage" section, find the existing command list and add `--setup-zwift`:

```
eufy-sync --setup-strava     # connect Strava (add to existing setup)
```

Add right after it:

```
eufy-sync --setup-zwift      # connect Zwift (add to existing setup)
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add Zwift to README with unofficial-API caveat"
```

---

## Final Verification

- [ ] **Step 1: Run full suite**

```bash
.venv/bin/python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 2: Sanity-check the CLI doesn't crash**

```bash
.venv/bin/eufy-sync --help
.venv/bin/eufy-sync --version
```

Expected: both print without traceback.

- [ ] **Step 3: Push branch and open PR**

```bash
git push -u origin <branch-name>
gh pr create --title "feat: add Zwift weight sync" --body "$(cat <<'EOF'
## Summary
Adds Zwift as a third sync target alongside Garmin and Strava. Reverse-engineered Keycloak password-grant auth + PUT /api/profiles/me, one write per sync, per-target failure isolation.

See [the design doc](docs/superpowers/specs/2026-05-17-zwift-weight-sync-design.md) for context.

## Test plan
- [x] Unit tests for ZwiftClient (auth, refresh, update_weight, token_status)
- [x] Sync integration tests (one-PUT-per-sync, failure isolation)
- [x] Config parsing tests for Zwift section
- [ ] Manual: run \`eufy-sync --setup-zwift\` against a real Zwift account and verify weight updates on the Zwift website

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Wait for CI, merge, clean up**

```bash
gh pr checks <N> --watch
gh api -X PUT /repos/sturimcode/eufy-sync/pulls/<N>/merge -f merge_method=squash
gh api -X DELETE /repos/sturimcode/eufy-sync/git/refs/heads/<branch-name>
```
