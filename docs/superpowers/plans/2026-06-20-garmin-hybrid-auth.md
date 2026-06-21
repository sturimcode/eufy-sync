# Garmin Hybrid Auth Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Try the browser-free curl_cffi login first and fall back to the Playwright browser login when it fails, injecting the browser-obtained tokens into the library session so upload is unchanged.

**Architecture:** All logic lives in `eufy_sync/garmin_auth.py`. Restore the pre-swap `browser_login` + DI-token exchange from git history, add a bridge that builds the library's token blob (`{di_token, di_refresh_token, di_client_id}`) and feeds it to `garmin.client.loads()`, and make `GarminAuth.login()` two-tier: curl_cffi, then browser on any fresh-login failure (interactive only). Chromium installs lazily, only when the fallback fires.

**Tech Stack:** Python 3.12+, garminconnect, curl_cffi, playwright (lazy Chromium), keyring, pytest. Tests mock `garminconnect.Garmin`, `browser_login`, and `_exchange_ticket_for_tokens`; none open a browser or hit the network.

**Source spec:** `docs/superpowers/specs/2026-06-20-garmin-hybrid-auth-design.md`

**Test runner:** `~/.local/bin/uv run pytest`

## Global Constraints

- macOS-only tool; Python >=3.12 (do not change the floor).
- No em-dashes in any docs/copy; no marketing filler words.
- The browser fallback is interactive-only; headless runs never open a browser.
- `garmin_client.py`, `sync.py`, `transform.py`, `config.py`, `credentials.py` must not change.

---

## Task 1: Restore browser-login machinery and re-add Playwright

Bring back the browser login, the DI-token exchange (adapted to return the client id), and the lazy Chromium installer as standalone functions. No wiring into `login()` yet, so the suite stays green.

**Files:**
- Modify: `eufy_sync/pyproject.toml`, `requirements.txt`
- Modify: `eufy_sync/garmin_auth.py`
- Test: `tests/test_garmin_auth.py`

**Interfaces:**
- Produces: `browser_login(email: str, password: str) -> str` (service ticket); `_exchange_ticket_for_tokens(service_ticket: str) -> tuple[str, str, str]` returning `(access_token, refresh_token, client_id)`; `_ensure_chromium() -> None`.

- [ ] **Step 1: Re-add the playwright dependency**

In `pyproject.toml`, the `dependencies` list currently has `garminconnect`, `curl_cffi`, `httpx`, `keyring`, `pyyaml`. Add `playwright>=1.40.0`:
```toml
dependencies = [
    "httpx>=0.27.0",
    "keyring>=25.0.0",
    "garminconnect>=0.3.6",
    "curl_cffi>=0.7.0",
    "playwright>=1.40.0",
    "pyyaml>=6.0",
]
```
In `requirements.txt`, add the `playwright>=1.40.0` line back alongside the others.

- [ ] **Step 2: Sync env**

Run: `~/.local/bin/uv run python -c "import playwright; print('playwright ok')"`
Expected: `playwright ok`.

- [ ] **Step 3: Write the failing test**

Append to `tests/test_garmin_auth.py`:
```python
def test_exchange_ticket_returns_tokens_and_client_id():
    from eufy_sync.garmin_auth import _exchange_ticket_for_tokens
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}
    with patch("eufy_sync.garmin_auth.httpx.post", return_value=resp):
        access, refresh, client_id = _exchange_ticket_for_tokens("ticket123")
    assert access == "acc"
    assert refresh == "ref"
    assert client_id  # the client id the exchange succeeded with
```

- [ ] **Step 4: Run, confirm fail**

Run: `~/.local/bin/uv run pytest tests/test_garmin_auth.py::test_exchange_ticket_returns_tokens_and_client_id -xvs`
Expected: `ImportError` (the function does not exist yet).

- [ ] **Step 5: Add imports and constants to `eufy_sync/garmin_auth.py`**

Add the new stdlib/httpx imports the restored code needs. The current top imports are `json`, `logging`, `os`, `from pathlib import Path`, and `from garminconnect import (Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError)`. Leave the `garminconnect` import unchanged for now (the existing `_fresh_login` still uses the error classes; Task 2 removes them). Add these so the block becomes:
```python
import base64
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
```

After `logger = logging.getLogger(__name__)`, add the constants and helper:
```python
# Garmin OAuth2 / SSO endpoints (public app identifiers, not per-user secrets)
DI_TOKEN_URL = "https://diauth.garmin.com/di-oauth2-service/oauth/token"
DI_CLIENT_IDS = [
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI_2024Q4",
    "GARMIN_CONNECT_MOBILE_ANDROID_DI",
]
DI_GRANT_TYPE = "https://connectapi.garmin.com/di-oauth2-service/oauth/grant/service_ticket"
SERVICE_URL = "https://mobile.integration.garmin.com/gcm/android"
SSO_LOGIN_URL = (
    "https://sso.garmin.com/mobile/sso/en_US/sign-in"
    "?clientId=GCM_ANDROID_DARK"
    "&service=https://mobile.integration.garmin.com/gcm/android"
)


def _basic_auth_header(client_id: str) -> str:
    """Garmin uses Basic auth with client_id as username, empty password."""
    encoded = base64.b64encode(f"{client_id}:".encode()).decode()
    return f"Basic {encoded}"
```

- [ ] **Step 6: Add `browser_login`, `_exchange_ticket_for_tokens`, `_ensure_chromium`**

Add these module-level functions (after `_basic_auth_header`). `browser_login` is the verbatim pre-swap implementation:
```python
def browser_login(email: str, password: str) -> str:
    """Open browser, log in to Garmin, intercept serviceTicketId.

    Returns the service ticket string.
    """
    from playwright.sync_api import sync_playwright

    captured_ticket = []

    def handle_login_capture(source, result_json):
        try:
            data = json.loads(result_json) if isinstance(result_json, str) else result_json
            if data.get("responseStatus", {}).get("type") == "SUCCESSFUL":
                ticket = data.get("serviceTicketId")
                if ticket:
                    captured_ticket.append(ticket)
                    logger.info("Captured service ticket from login response")
        except Exception as e:
            logger.warning("Failed to parse login capture: %s", e)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; sdk_gphone64_arm64)"
                " AppleWebKit/537.36 (KHTML, like Gecko)"
                " Chrome/121.0.0.0 Mobile Safari/537.36"
            ),
            viewport={"width": 412, "height": 915},
            is_mobile=True,
        )
        context.expose_binding(
            "pirateGarminCaptureLogin",
            lambda source, data: handle_login_capture(source, data),
        )
        context.add_init_script("""
            (function() {
                const originalFetch = window.fetch;
                window.fetch = async function(...args) {
                    const response = await originalFetch.apply(this, args);
                    const url = typeof args[0] === 'string' ? args[0] : args[0]?.url || '';
                    if (url.includes('/mobile/api/login')) {
                        try {
                            const clone = response.clone();
                            const data = await clone.json();
                            window.pirateGarminCaptureLogin(JSON.stringify(data));
                        } catch(e) {}
                    }
                    return response;
                };
                const origOpen = XMLHttpRequest.prototype.open;
                const origSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url, ...rest) {
                    this._url = url;
                    return origOpen.call(this, method, url, ...rest);
                };
                XMLHttpRequest.prototype.send = function(...args) {
                    this.addEventListener('load', function() {
                        if (this._url && this._url.includes('/mobile/api/login')) {
                            try {
                                window.pirateGarminCaptureLogin(this.responseText);
                            } catch(e) {}
                        }
                    });
                    return origSend.apply(this, args);
                };
            })();
        """)

        page = context.new_page()
        page.goto(SSO_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector(
            "input[name='username'], input[name='email'], #username, #email",
            timeout=30000,
        )
        for selector in ["input[name='username']", "input[name='email']", "#username", "#email"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.fill(email)
                    break
            except Exception:
                continue
        for selector in ["input[name='password']", "#password"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.fill(password)
                    break
            except Exception:
                continue
        for selector in ["button[type='submit']", "#login-btn-signin", "button.btn-primary"]:
            try:
                el = page.query_selector(selector)
                if el and el.is_visible():
                    el.click()
                    break
            except Exception:
                continue

        logger.info("Waiting for Garmin login to complete (check the browser window)...")
        for _ in range(360):
            if captured_ticket:
                break
            page.wait_for_timeout(500)
        browser.close()

    if not captured_ticket:
        raise RuntimeError(
            "Garmin login failed - no service ticket captured. "
            "Check your Garmin email/password, and watch for CAPTCHA or MFA prompts."
        )
    return captured_ticket[0]


def _exchange_ticket_for_tokens(service_ticket: str) -> tuple[str, str, str]:
    """Exchange a service ticket for DI tokens. Returns (access, refresh, client_id)."""
    for client_id in DI_CLIENT_IDS:
        try:
            resp = httpx.post(
                DI_TOKEN_URL,
                headers={
                    "authorization": _basic_auth_header(client_id),
                    "content-type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": DI_GRANT_TYPE,
                    "service_ticket": service_ticket,
                    "service_url": SERVICE_URL,
                    "client_id": client_id,
                },
                timeout=30.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                logger.info("Got DI tokens with client_id: %s", client_id)
                return data["access_token"], data["refresh_token"], client_id
            logger.debug("Client ID %s returned %d, trying next", client_id, resp.status_code)
        except Exception as e:
            logger.debug("Client ID %s failed: %s", client_id, e)
    raise RuntimeError(
        "Failed to exchange Garmin service ticket for tokens. "
        "This is usually a temporary Garmin server issue."
    )


def _ensure_chromium() -> None:
    """Install Playwright Chromium if not already present (lazy, only on fallback)."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            path = p.chromium.executable_path
            if not Path(path).exists():
                raise FileNotFoundError(path)
    except Exception:
        print("Installing Chromium for the Garmin browser login (one-time)...")
        result = subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"])
        if result.returncode != 0:
            from eufy_sync.sync import PermanentSyncError
            raise PermanentSyncError(
                "Failed to install Chromium for the Garmin browser fallback. "
                "Try: playwright install chromium"
            )
```

- [ ] **Step 7: Run the test and the suite**

Run: `~/.local/bin/uv run pytest tests/test_garmin_auth.py -x`
Expected: the new test passes; existing tests still pass. `login()` and the old `_fresh_login` are unchanged in this task, and the `garminconnect` error imports they use are still present (Step 5 kept them), so nothing breaks.

Then run the full suite: `~/.local/bin/uv run pytest -q` -> green.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml requirements.txt eufy_sync/garmin_auth.py tests/test_garmin_auth.py
git commit -m "feat(garmin): restore browser login + DI exchange as fallback machinery"
```

---

## Task 2: Two-tier login with browser fallback

Rework `_fresh_login` so it tries curl_cffi then falls back to the browser, add the token bridge, and update the tests that asserted the old rate-limit-message behavior.

**Files:**
- Modify: `eufy_sync/garmin_auth.py`
- Test: `tests/test_garmin_auth.py`

**Interfaces:**
- Consumes: `browser_login`, `_exchange_ticket_for_tokens`, `_ensure_chromium` (Task 1); `garmin.client.loads(json_str)` and `garmin.client.dumps()` (library).
- Produces: `GarminAuth.login(interactive)` and `force_reauth()` with curl_cffi-then-browser behavior; `GarminAuth._browser_fallback(garmin)`.

- [ ] **Step 1: Update the existing rate-limit/credential tests for the new behavior**

In `tests/test_garmin_auth.py`, DELETE these three now-obsolete tests (the hybrid no longer raises on a 429; it falls back): `test_login_rate_limited_raises_clear_error`, `test_login_bad_credentials_raises_clear_error`, `test_force_reauth_rate_limited_raises_clear_error`.

Add these replacements:
```python
def test_login_curl_cffi_success_skips_browser(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake = MagicMock()  # garmin.login() succeeds (no side_effect)
    browser_calls = []
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login",
                        lambda e, p: browser_calls.append(1) or "ticket")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.login(interactive=True)
    fake.login.assert_called_once()
    assert browser_calls == []  # browser never opened
    assert result is fake


def test_login_falls_back_to_browser_when_curl_cffi_fails(monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectTooManyRequestsError("429")
    monkeypatch.setattr("eufy_sync.garmin_auth._ensure_chromium", lambda: None)
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login", lambda e, p: "ticket")
    monkeypatch.setattr("eufy_sync.garmin_auth._exchange_ticket_for_tokens",
                        lambda t: ("acc", "ref", "cid"))
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.login(interactive=True)
    fake.client.loads.assert_called_once()
    loaded = json.loads(fake.client.loads.call_args.args[0])
    assert loaded == {"di_token": "acc", "di_refresh_token": "ref", "di_client_id": "cid"}
    assert result is fake


def test_login_bad_credentials_fails_both_tiers(monkeypatch):
    from garminconnect import GarminConnectAuthenticationError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectAuthenticationError("bad")
    monkeypatch.setattr("eufy_sync.garmin_auth._ensure_chromium", lambda: None)

    def _no_ticket(email, password):
        raise RuntimeError("no service ticket captured")
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login", _no_ticket)
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=True)
    assert "update-password" in str(exc_info.value)


def test_login_headless_never_opens_browser(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    browser_calls = []
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login",
                        lambda e, p: browser_calls.append(1) or "ticket")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=MagicMock()):
        with pytest.raises(PermanentSyncError):
            auth.login(interactive=False)
    assert browser_calls == []


def test_force_reauth_falls_back_to_browser(monkeypatch):
    from garminconnect import GarminConnectTooManyRequestsError
    auth = _auth()
    monkeypatch.setattr(auth, "_clear_token", lambda: None)
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectTooManyRequestsError("429")
    monkeypatch.setattr("eufy_sync.garmin_auth._ensure_chromium", lambda: None)
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login", lambda e, p: "ticket")
    monkeypatch.setattr("eufy_sync.garmin_auth._exchange_ticket_for_tokens",
                        lambda t: ("a", "r", "c"))
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.force_reauth()
    fake.client.loads.assert_called_once()
    assert result is fake
```

- [ ] **Step 2: Run, confirm the new fallback test fails**

Run: `~/.local/bin/uv run pytest tests/test_garmin_auth.py::test_login_falls_back_to_browser_when_curl_cffi_fails -xvs`
Expected: FAIL (current `_fresh_login` raises `PermanentSyncError` on 429 instead of falling back).

- [ ] **Step 3: Replace `_fresh_login` and add `_browser_fallback`**

In `eufy_sync/garmin_auth.py`, replace the entire `_fresh_login` method with:
```python
    def _fresh_login(self, garmin: Garmin) -> None:
        """Try the browser-free login; fall back to the browser on any failure."""
        try:
            garmin.login()
            return
        except Exception as e:
            logger.warning(
                "Browser-free Garmin login failed (%s); opening browser fallback", e
            )
        self._browser_fallback(garmin)

    def _browser_fallback(self, garmin: Garmin) -> None:
        """Log in via the Playwright browser and inject the tokens into the session."""
        from eufy_sync.sync import PermanentSyncError
        _ensure_chromium()
        try:
            ticket = browser_login(self.email, self.password)
        except Exception as e:
            raise PermanentSyncError(
                "Garmin login failed. If you changed your password, run: "
                "eufy-sync --update-password. Otherwise re-run: eufy-sync --reauth"
            ) from e
        try:
            access, refresh, client_id = _exchange_ticket_for_tokens(ticket)
        except Exception as e:
            raise PermanentSyncError(
                "Garmin token exchange failed; try: eufy-sync --reauth"
            ) from e
        blob = {"di_token": access, "di_refresh_token": refresh, "di_client_id": client_id}
        garmin.client.loads(json.dumps(blob))
```

The rewritten `_fresh_login` no longer references the `garminconnect` error classes, so simplify the import at the top of the file from `from garminconnect import (Garmin, GarminConnectAuthenticationError, GarminConnectTooManyRequestsError)` to just `from garminconnect import Garmin`. (`sync.py` keeps its own `GarminConnectTooManyRequestsError` import; that is unaffected.)

- [ ] **Step 4: Run the auth tests**

Run: `~/.local/bin/uv run pytest tests/test_garmin_auth.py -x`
Expected: all pass (the restore test, the new fallback/success/headless/bad-creds/force_reauth tests, and the unchanged `_load_token`/`token_status` tests).

- [ ] **Step 5: Run the full suite**

Run: `~/.local/bin/uv run pytest -q`
Expected: green. (`sync._is_permanent` 429 rule and `test_sync.py::test_rate_limit_error_is_permanent` are unchanged and still pass; they cover the mid-sync refresh case.)

- [ ] **Step 6: Confirm no import cycle and CLI still imports**

Run: `~/.local/bin/uv run python -c "import eufy_sync.garmin_auth, eufy_sync.cli, eufy_sync.sync"`
Expected: no error.

- [ ] **Step 7: Commit**

```bash
git add eufy_sync/garmin_auth.py tests/test_garmin_auth.py
git commit -m "feat(garmin): two-tier login - curl_cffi first, browser fallback"
```

---

## Task 3: Wizard text and README

**Files:**
- Modify: `eufy_sync/cli.py`
- Modify: `README.md`

- [ ] **Step 1: Update the first-run wizard line**

In `eufy_sync/cli.py`, in `_first_run_setup`, find the line that prints the first-sync message (it reads `print(f"Saved. Running first sync to {' and '.join(targets)} (last 7 days)...")`). Immediately after it, add a Garmin note:
```python
    if garmin_email:
        print("Logging in to Garmin (a browser may open if the direct login is rate-limited).")
```
(`garmin_email` is in scope in `_first_run_setup`. Confirm with `grep -n "garmin_email" eufy_sync/cli.py`.)

- [ ] **Step 2: Rewrite the README "How Garmin login works" section**

Writing rules: no em-dashes; no marketing filler; plain prose; sentence case.

Replace the current section body with:
```
## How Garmin login works

Garmin has no official API for writing body composition into Connect. In March 2026 it put Cloudflare in front of its login, which broke the Python libraries that talked to it; [garth](https://github.com/matin/garth) was [deprecated](https://github.com/matin/garth/discussions/222) and stays that way.

eufy-sync logs in through [python-garminconnect](https://github.com/cyberjunky/python-garminconnect), which gets past Cloudflare without a browser. On first run you enter your Garmin email and password in the terminal, and a code if your account uses two-factor. If that direct login is rate-limited or temporarily blocked, eufy-sync falls back to a one-time browser login: a Chromium window opens, you sign in, and it continues. Either way the tokens are saved to your system keychain and refresh on their own, so later runs need no login. Headless setups (a server or scheduled job) use the direct login only, since there is no screen for a browser.
```

- [ ] **Step 3: Run the suite and a smoke check**

Run: `~/.local/bin/uv run pytest -q` -> green.
Run: `~/.local/bin/uv run eufy-sync --help` -> prints without error.

- [ ] **Step 4: Commit**

```bash
git add eufy_sync/cli.py README.md
git commit -m "docs: describe the hybrid Garmin login (browser fallback)"
```

---

## Final Verification

- [ ] **Step 1: Full suite**

Run: `~/.local/bin/uv run pytest -v` -> all pass.

- [ ] **Step 2: Confirm the pieces are wired**

Run:
```bash
~/.local/bin/uv run python -c "from eufy_sync.garmin_auth import browser_login, _exchange_ticket_for_tokens, _ensure_chromium; print('fallback machinery present')"
grep -n "playwright" pyproject.toml requirements.txt
```
Expected: prints the confirmation; playwright is in both dependency files.

- [ ] **Step 3: Push and open a PR**

```bash
git push -u origin garmin-hybrid-auth
gh pr create --title "Hybrid Garmin auth: curl_cffi primary, browser fallback" --body "$(cat <<'EOF'
## Summary
Makes Garmin login resilient: try the browser-free curl_cffi login first, fall back to the Playwright browser login on any fresh-login failure (interactive only). Browser-obtained DI tokens are injected into the library session via client.loads(), so upload is unchanged and the browser path sidesteps the library's open #369 validation bug.

- Two-tier GarminAuth.login() and force_reauth().
- Restores browser_login + DI exchange from git history; adds the token bridge.
- Chromium installs lazily, only when the fallback fires.
- Headless stays curl_cffi-only and fails with a reauth message.
- Re-adds playwright as a dependency.

See docs/superpowers/specs/2026-06-20-garmin-hybrid-auth-design.md.

## Test plan
- [x] pytest green; both login paths mocked, no browser or network
- [ ] Manual: force a curl_cffi failure (or rate limit) and confirm the browser fallback completes and a weigh-in lands in Garmin
- [ ] Manual: a normal login uses curl_cffi with no browser

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 4: Watch CI, squash-merge, clean up**

```bash
gh pr checks <N> --watch
gh api -X PUT /repos/sturimcode/eufy-sync/pulls/<N>/merge -f merge_method=squash
gh api -X DELETE /repos/sturimcode/eufy-sync/git/refs/heads/garmin-hybrid-auth
```

> Version stays at 1.7.3 on this branch. Cutting 1.7.4 (which ships the swap, the 429 handling, the hybrid, and the doc fixes together) is a separate step after this merges and you confirm the fallback works live.
