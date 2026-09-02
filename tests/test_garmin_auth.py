from __future__ import annotations

import json
import sys
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
    fake_garmin.client.loads.assert_called_once()


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


def test_login_headless_logs_in_from_the_stored_password(monkeypatch):
    # No blob and nobody watching: the password login normally needs no input,
    # so the run heals itself instead of asking for --reauth.
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    saved = {}
    monkeypatch.setattr(auth, "_save_token", lambda g: saved.setdefault("called", True))
    fake = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.login(interactive=False)
    fake.login.assert_called_once()
    assert result is fake
    assert saved.get("called") is True


def test_login_headless_heals_an_unusable_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake = MagicMock()
    fake.client.loads.side_effect = ValueError("corrupt")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.login(interactive=False)
    fake.login.assert_called_once()
    assert result is fake


def test_token_status_valid_with_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    assert auth.token_status()["state"] == "valid"


def test_force_reauth_clears_token_logs_in_and_saves(monkeypatch):
    auth = _auth()
    calls = []
    monkeypatch.setattr(auth, "_clear_token", lambda: calls.append("clear"))
    monkeypatch.setattr(auth, "_save_token", lambda g: calls.append("save"))
    fake_garmin = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin):
        result = auth.force_reauth()
    assert result is fake_garmin
    fake_garmin.login.assert_called_once()
    assert calls == ["clear", "save"]   # cleared before login, saved after


def test_login_falls_back_to_fresh_when_blob_unusable(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: dict(BLOB))
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake_garmin = MagicMock()
    fake_garmin.client.loads.side_effect = ValueError("corrupt")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake_garmin):
        auth.login(interactive=True)
    fake_garmin.login.assert_called_once()   # fell back to fresh login when restore failed


def test_token_status_no_session_without_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    assert auth.token_status()["state"] == "no_session"


def test_load_token_rejects_old_playwright_session(monkeypatch):
    # Old Playwright session stored di_token as a nested dict; must be rejected
    # so the user re-logs in once under the new library.
    auth = _auth()
    monkeypatch.setattr("eufy_sync.credentials._keyring_available", lambda: True)
    monkeypatch.setattr(
        "eufy_sync.credentials.get_token",
        lambda name: {"di_token": {"access_token": "x"}},
    )
    assert auth._load_token() is None


def test_load_token_accepts_new_library_blob(monkeypatch):
    auth = _auth()
    monkeypatch.setattr("eufy_sync.credentials._keyring_available", lambda: True)
    monkeypatch.setattr("eufy_sync.credentials.get_token", lambda name: dict(BLOB))
    assert auth._load_token() == BLOB


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


def test_login_bad_credentials_skips_browser_and_names_fix(monkeypatch):
    # Definitively rejected credentials must not open the browser fallback:
    # it would autofill the same bad password and fail minutes later.
    from garminconnect import GarminConnectAuthenticationError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectAuthenticationError("bad")
    browser_calls = []
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login",
                        lambda e, p: browser_calls.append(1) or "ticket")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=True)
    assert "update-password" in str(exc_info.value)
    assert browser_calls == []


def test_login_mfa_cancel_skips_browser_and_names_fix(monkeypatch):
    from eufy_sync.garmin_auth import GarminLoginCancelled
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    fake = MagicMock()
    fake.login.side_effect = GarminLoginCancelled("no MFA code entered")
    browser_calls = []
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login",
                        lambda e, p: browser_calls.append(1) or "ticket")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=True)
    assert "update-password" in str(exc_info.value)
    assert browser_calls == []


def test_mfa_prompt_empty_input_cancels(monkeypatch):
    from eufy_sync.garmin_auth import GarminLoginCancelled, _mfa_prompt
    monkeypatch.setattr("builtins.input", lambda prompt: "")
    with pytest.raises(GarminLoginCancelled):
        _mfa_prompt()


def test_mfa_prompt_ctrl_c_cancels(monkeypatch):
    from eufy_sync.garmin_auth import GarminLoginCancelled, _mfa_prompt

    def _interrupt(prompt):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", _interrupt)
    with pytest.raises(GarminLoginCancelled):
        _mfa_prompt()


def test_mfa_prompt_returns_stripped_code(monkeypatch):
    from eufy_sync.garmin_auth import _mfa_prompt
    monkeypatch.setattr("builtins.input", lambda prompt: " 123456 ")
    assert _mfa_prompt() == "123456"


def test_mfa_prompt_cancels_when_nobody_answers(monkeypatch):
    """An MFA prompt left unanswered used to hold the process open for as long
    as the window stayed on screen. Cancelling routes it into the existing
    "the stored password is likely wrong" advice instead."""
    from eufy_sync.garmin_auth import GarminLoginCancelled, _mfa_prompt
    monkeypatch.setattr("eufy_sync.garmin_auth.input_with_timeout",
                        lambda *args: None)
    with pytest.raises(GarminLoginCancelled) as exc_info:
        _mfa_prompt()
    assert "5 minutes" in str(exc_info.value)


def _fail_on_browser(monkeypatch):
    """Make either half of the browser fallback fail the test outright. A hidden
    scheduled run must never put a Chromium window on screen."""
    def _opened(*args):
        raise AssertionError("browser fallback opened in a headless run")
    monkeypatch.setattr("eufy_sync.garmin_auth._ensure_chromium", _opened)
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login", _opened)


def test_login_headless_never_opens_browser(monkeypatch):
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    _fail_on_browser(monkeypatch)
    with patch("eufy_sync.garmin_auth.Garmin", return_value=MagicMock()) as ctor:
        auth.login(interactive=False)
    # The headless MFA callback, not the interactive one that calls input().
    from eufy_sync.garmin_auth import _headless_mfa_prompt
    assert ctor.call_args.kwargs["prompt_mfa"] is _headless_mfa_prompt


def test_login_headless_mfa_demand_names_reauth(monkeypatch):
    # The library calls prompt_mfa only when Garmin wants a code; headless that
    # raises straight back out of garmin.login().
    from eufy_sync.garmin_auth import GarminLoginCancelled
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    fake.login.side_effect = GarminLoginCancelled("mfa")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=False)
    assert "--reauth garmin" in str(exc_info.value)


def test_login_headless_bad_credentials_names_update_password(monkeypatch):
    from garminconnect import GarminConnectAuthenticationError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectAuthenticationError("bad")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.login(interactive=False)
    message = str(exc_info.value)
    assert "--update-password" in message
    # app.py checks for "--reauth" first, so this message must not carry it or
    # the notification would name the wrong fix.
    assert "--reauth" not in message


def test_login_headless_rate_limit_keeps_its_type(monkeypatch):
    # sync._is_permanent and the quiet-429 handling both switch on the type, so
    # a 429 must not come back wrapped in a PermanentSyncError.
    from garminconnect import GarminConnectTooManyRequestsError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectTooManyRequestsError("429")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(GarminConnectTooManyRequestsError):
            auth.login(interactive=False)


def test_login_headless_propagates_a_network_failure_unchanged(monkeypatch):
    # Upstream classifies transient network trouble by the message text, so the
    # original exception has to survive intact.
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    fake.login.side_effect = ConnectionError("Temporary failure in name resolution")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(ConnectionError, match="name resolution"):
            auth.login(interactive=False)


def test_headless_mfa_prompt_cancels_without_reading_input(monkeypatch):
    from eufy_sync.garmin_auth import GarminLoginCancelled, _headless_mfa_prompt
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": (_ for _ in ()).throw(AssertionError("prompted a headless run")),
    )
    with pytest.raises(GarminLoginCancelled):
        _headless_mfa_prompt()


def test_silent_reauth_clears_token_logs_in_and_saves(monkeypatch):
    auth = _auth()
    calls = []
    monkeypatch.setattr(auth, "_clear_token", lambda: calls.append("clear"))
    monkeypatch.setattr(auth, "_save_token", lambda g: calls.append("save"))
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.silent_reauth()
    assert result is fake
    fake.login.assert_called_once()
    assert calls == ["clear", "save"]


def test_silent_reauth_mfa_demand_names_reauth(monkeypatch):
    from eufy_sync.garmin_auth import GarminLoginCancelled
    auth = _auth()
    monkeypatch.setattr(auth, "_clear_token", lambda: None)
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    fake.login.side_effect = GarminLoginCancelled("mfa")
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(PermanentSyncError) as exc_info:
            auth.silent_reauth()
    assert "--reauth garmin" in str(exc_info.value)


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


# ---------------------------------------------------------------------------
# Persisting tokens the library rotated during the run
# ---------------------------------------------------------------------------


def _dumping(blob) -> MagicMock:
    """A fake Garmin whose client.dumps() returns the given blob as JSON."""
    fake = MagicMock()
    fake.client.dumps.return_value = json.dumps(blob)
    return fake


def test_save_if_changed_stores_a_rotated_blob(monkeypatch):
    rotated = dict(BLOB, di_refresh_token="rotated")
    stored = {}
    monkeypatch.setattr("eufy_sync.credentials.get_token", lambda name: dict(BLOB))
    monkeypatch.setattr("eufy_sync.credentials.store_token",
                        lambda name, data: stored.update({name: data}))
    _auth().save_if_changed(_dumping(rotated))
    assert stored == {"garmin": rotated}


def test_save_if_changed_skips_an_identical_blob(monkeypatch):
    calls = []
    monkeypatch.setattr("eufy_sync.credentials.get_token", lambda name: dict(BLOB))
    monkeypatch.setattr("eufy_sync.credentials.store_token",
                        lambda name, data: calls.append(name))
    _auth().save_if_changed(_dumping(dict(BLOB)))
    assert calls == []   # nothing rotated, nothing written


def test_save_if_changed_rejects_a_blob_load_token_would_refuse(monkeypatch):
    # di_token as a dict is the old Playwright shape; storing it would leave a
    # blob the next run refuses to restore.
    calls = []
    monkeypatch.setattr("eufy_sync.credentials.get_token", lambda name: None)
    monkeypatch.setattr("eufy_sync.credentials.store_token",
                        lambda name, data: calls.append(name))
    _auth().save_if_changed(_dumping({"di_token": {"access_token": "x"}}))
    assert calls == []


def test_save_if_changed_swallows_a_dumps_failure(monkeypatch):
    calls = []
    monkeypatch.setattr("eufy_sync.credentials.store_token",
                        lambda name, data: calls.append(name))
    fake = MagicMock()
    fake.client.dumps.side_effect = RuntimeError("no session")
    _auth().save_if_changed(fake)   # must not raise
    assert calls == []


def test_exchange_ticket_returns_tokens_and_client_id():
    from eufy_sync.garmin_auth import _exchange_ticket_for_tokens
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"access_token": "acc", "refresh_token": "ref", "expires_in": 3600}
    with patch("eufy_sync.garmin_auth.httpx.post", return_value=resp):
        access, refresh, client_id = _exchange_ticket_for_tokens("ticket123")
    assert access == "acc"
    assert refresh == "ref"
    assert client_id  # the client id the exchange succeeded with


def test_interactive_login_network_blip_does_not_open_browser(monkeypatch):
    # A Wi-Fi hiccup during a first-run login used to trigger the browser
    # fallback: a Chromium download and a window, when retrying the same
    # login a moment later is the right move. The error must leave as it
    # arrived, so the caller's transient-network classification still works.
    from garminconnect import GarminConnectConnectionError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    _fail_on_browser(monkeypatch)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectConnectionError(
        "Connection error: [Errno 8] nodename nor servname provided, or not known"
    )
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        with pytest.raises(GarminConnectConnectionError, match="nodename nor servname"):
            auth.login(interactive=True)


def test_interactive_login_non_network_failure_still_opens_browser(monkeypatch):
    # The fallback exists for logins the direct path cannot complete (a
    # Cloudflare block reads as a 403 connection error); those keep it.
    from garminconnect import GarminConnectConnectionError
    auth = _auth()
    monkeypatch.setattr(auth, "_load_token", lambda: None)
    monkeypatch.setattr(auth, "_save_token", lambda g: None)
    fake = MagicMock()
    fake.login.side_effect = GarminConnectConnectionError("Login failed: 403 Forbidden")
    monkeypatch.setattr("eufy_sync.garmin_auth._ensure_chromium", lambda: None)
    monkeypatch.setattr("eufy_sync.garmin_auth.browser_login", lambda e, p: "ticket")
    monkeypatch.setattr("eufy_sync.garmin_auth._exchange_ticket_for_tokens",
                        lambda t: ("acc", "ref", "cid"))
    with patch("eufy_sync.garmin_auth.Garmin", return_value=fake):
        result = auth.login(interactive=True)
    assert result is fake
    fake.client.loads.assert_called_once()


def test_ensure_chromium_names_the_extra_when_playwright_is_missing(monkeypatch):
    # Playwright is an optional extra. A user without it who reaches the
    # browser fallback needs the one install command, not an ImportError.
    from eufy_sync.garmin_auth import _ensure_chromium
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with patch("eufy_sync.garmin_auth.install_command", return_value="uv tool install --force 'eufy-sync[browser]'"):
        with pytest.raises(PermanentSyncError, match=r"eufy-sync\[browser\]"):
            _ensure_chromium()
