from __future__ import annotations

from unittest.mock import patch

from eufy_sync.credentials import _keyring_available


def _make_fake_backend(module: str):
    """Build a fake backend instance whose class name is the generic
    'Keyring' (like the real keyring.backends.fail.Keyring / null.Keyring),
    so only the module name can distinguish it from a working backend."""
    cls = type("Keyring", (), {"__module__": module})
    return cls()


def test_fail_backend_is_detected_by_module_even_with_generic_name():
    fake_backend = _make_fake_backend("keyring.backends.fail")
    with patch("keyring.get_keyring", return_value=fake_backend):
        assert _keyring_available() is False


def test_null_backend_is_detected_by_module():
    fake_backend = _make_fake_backend("keyring.backends.null")
    with patch("keyring.get_keyring", return_value=fake_backend):
        assert _keyring_available() is False
