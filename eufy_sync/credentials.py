"""Secure credential storage: one JSON vault, in the system keychain or a
0o600 file.

All secrets (passwords and OAuth tokens) live together in a single JSON
object: {"passwords": {"<user>:<service>": "<pw>"}, "tokens": {"<name>": {...}}}.
That object is stored in exactly one place at a time:

- keychain backend: one keyring item (account "vault") - one "Always Allow"
  prompt total, instead of one per secret.
- file backend: ~/.garmin-sync/credentials.json, written 0o600 - used when
  there is no keychain (e.g. headless Linux) or when the user opts in with
  --use-file-store.

Credential functions never raise for lack of a keychain: the file backend is
always the fallback, so callers can call get/store/delete unconditionally.

A lazy, one-time migration promotes secrets from the old per-item keychain
layout (one keyring account per password/token) into the vault the first
time each one is looked up.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "eufy-garmin-sync"
VAULT_ACCOUNT = "vault"

CRED_FILE = Path.home() / ".garmin-sync" / "credentials.json"


def _keyring_available() -> bool:
    try:
        import keyring
        # Test that the backend works (not the null/fail backend). The
        # no-backend class is keyring.backends.fail.Keyring, whose __name__
        # is just "Keyring" - checking the name alone misses it, so the
        # module is checked too (that's where "fail"/"null" actually shows).
        backend = keyring.get_keyring()
        backend_cls = type(backend)
        name = backend_cls.__name__.lower()
        module = (backend_cls.__module__ or "").lower()
        if "fail" in name or "null" in name or "fail" in module or "null" in module:
            return False
        return True
    except Exception:
        return False


def _active_backend() -> str:
    """Which backend currently holds the vault.

    A CRED_FILE on disk always wins (the user opted into file storage, or an
    earlier run auto-fell-back to it). Otherwise use the keychain if it
    works; if not, the file backend is where the very next write will land.
    """
    if CRED_FILE.exists():
        return "file"
    if _keyring_available():
        return "keychain"
    return "file"


def active_store_label() -> str:
    """Human-readable description of the active backend, for doctor/status."""
    if _active_backend() == "keychain":
        return "system keychain"
    return "file (~/.garmin-sync/credentials.json)"


def _empty_vault() -> dict:
    return {"passwords": {}, "tokens": {}}


def _normalize_vault(vault: dict | None) -> dict:
    """Tolerate a partially-shaped or missing vault dict."""
    if not isinstance(vault, dict):
        return _empty_vault()
    passwords = vault.get("passwords")
    tokens = vault.get("tokens")
    return {
        "passwords": passwords if isinstance(passwords, dict) else {},
        "tokens": tokens if isinstance(tokens, dict) else {},
    }


def _load_vault_from_keychain() -> dict:
    import keyring
    raw = keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT)
    if raw is None:
        return _empty_vault()
    try:
        return _normalize_vault(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        logger.warning("Keychain vault contained malformed JSON; treating as empty")
        return _empty_vault()


def _save_vault_to_keychain(vault: dict) -> None:
    import keyring
    keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps(vault))


def _load_vault_from_file() -> dict:
    if not CRED_FILE.exists():
        return _empty_vault()
    try:
        return _normalize_vault(json.loads(CRED_FILE.read_text()))
    except (json.JSONDecodeError, TypeError, OSError):
        logger.warning("Credentials file contained malformed JSON; treating as empty")
        return _empty_vault()


def _save_vault_to_file(vault: dict) -> None:
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(CRED_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(vault, f)


def _load_vault() -> dict:
    if _active_backend() == "file":
        return _load_vault_from_file()
    return _load_vault_from_keychain()


def _save_vault(vault: dict) -> None:
    if _active_backend() == "file":
        _save_vault_to_file(vault)
    else:
        _save_vault_to_keychain(vault)


# --- Public API: passwords ---------------------------------------------------


def get_password(account: str) -> str | None:
    """Return a stored password, migrating it from the legacy keychain item
    (one account per password) into the vault on first access."""
    vault = _load_vault()
    if account in vault["passwords"]:
        return vault["passwords"][account]

    if _keyring_available():
        try:
            import keyring
            legacy = keyring.get_password(SERVICE_NAME, account)
        except Exception:
            legacy = None
        if legacy is not None:
            vault["passwords"][account] = legacy
            _save_vault(vault)
            try:
                keyring.delete_password(SERVICE_NAME, account)
            except Exception:
                pass
            return legacy

    return None


def store_password(account: str, password: str) -> None:
    """Store a password in the vault (keychain or file, whichever is active)."""
    vault = _load_vault()
    vault["passwords"][account] = password
    _save_vault(vault)


def delete_password(account: str) -> None:
    """Remove a password from the vault, and best-effort from the legacy
    keychain item if one is still lingering."""
    vault = _load_vault()
    if account in vault["passwords"]:
        del vault["passwords"][account]
        _save_vault(vault)

    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, account)
        except Exception:
            pass


# --- Public API: tokens -------------------------------------------------------


def get_token(name: str) -> dict | None:
    """Return a stored token dict, migrating it from the legacy keychain item
    (`token:<name>`) into the vault on first access. Tolerates malformed
    legacy JSON by treating it as not-found."""
    vault = _load_vault()
    if name in vault["tokens"]:
        return vault["tokens"][name]

    if _keyring_available():
        try:
            import keyring
            legacy_raw = keyring.get_password(SERVICE_NAME, f"token:{name}")
        except Exception:
            legacy_raw = None
        if legacy_raw is not None:
            try:
                legacy = json.loads(legacy_raw)
            except (json.JSONDecodeError, TypeError):
                return None
            vault["tokens"][name] = legacy
            _save_vault(vault)
            try:
                keyring.delete_password(SERVICE_NAME, f"token:{name}")
            except Exception:
                pass
            return legacy

    return None


def store_token(name: str, data: dict) -> None:
    """Store a token dict in the vault (keychain or file, whichever is active)."""
    vault = _load_vault()
    vault["tokens"][name] = data
    _save_vault(vault)


def delete_token(name: str) -> None:
    """Remove a token from the vault, and best-effort from the legacy
    keychain item if one is still lingering."""
    vault = _load_vault()
    if name in vault["tokens"]:
        del vault["tokens"][name]
        _save_vault(vault)

    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, f"token:{name}")
        except Exception:
            pass


# --- Mode switching -----------------------------------------------------------


def use_file_store() -> None:
    """Move the vault into a 0o600 file and stop using the keychain.

    Reads whatever is currently active (forcing a keychain read if that is
    the active backend), writes it to CRED_FILE, then clears the keychain
    vault item. After this, CRED_FILE existing makes it the active backend.
    """
    if _active_backend() == "keychain":
        vault = _load_vault_from_keychain()
    else:
        vault = _load_vault()

    _save_vault_to_file(vault)

    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, VAULT_ACCOUNT)
        except Exception:
            pass


def use_keychain_store() -> None:
    """Move the vault into the system keychain and stop using the file.

    Raises RuntimeError if no working keyring backend is available.
    """
    if not _keyring_available():
        raise RuntimeError(
            "No system keychain is available on this machine, so credentials "
            "cannot be moved into it. Staying on the file store."
        )

    vault = _load_vault_from_file()
    _save_vault_to_keychain(vault)

    if CRED_FILE.exists():
        CRED_FILE.unlink()
