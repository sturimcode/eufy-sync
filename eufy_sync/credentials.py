"""Secure credential storage using macOS Keychain (via keyring).

Passwords and OAuth tokens are stored in the system keychain instead of
plaintext files on disk. Falls back to file-based storage if keyring
is unavailable (e.g., headless Linux).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

SERVICE_NAME = "eufy-garmin-sync"


def _keyring_available() -> bool:
    try:
        import keyring
        # Test that the backend works (not the null/fail backend)
        backend = keyring.get_keyring()
        name = type(backend).__name__
        if "fail" in name.lower() or "null" in name.lower():
            return False
        return True
    except Exception:
        return False


def store_password(account: str, password: str) -> None:
    """Store a password in the system keychain."""
    import keyring
    keyring.set_password(SERVICE_NAME, account, password)


def get_password(account: str) -> str | None:
    """Retrieve a password from the system keychain."""
    import keyring
    return keyring.get_password(SERVICE_NAME, account)


def delete_password(account: str) -> None:
    """Remove a password from the system keychain."""
    import keyring
    try:
        keyring.delete_password(SERVICE_NAME, account)
    except keyring.errors.PasswordDeleteError:
        pass


def store_token(name: str, data: dict) -> None:
    """Store a token dict in the keychain as JSON."""
    import keyring
    keyring.set_password(SERVICE_NAME, f"token:{name}", json.dumps(data))


def get_token(name: str) -> dict | None:
    """Retrieve a token dict from the keychain."""
    import keyring
    raw = keyring.get_password(SERVICE_NAME, f"token:{name}")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def delete_token(name: str) -> None:
    """Remove a token from the keychain."""
    import keyring
    try:
        keyring.delete_password(SERVICE_NAME, f"token:{name}")
    except keyring.errors.PasswordDeleteError:
        pass
