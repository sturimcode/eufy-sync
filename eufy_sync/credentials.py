"""Secure credential storage: one JSON vault, in the system keychain or a
0o600 file.

All secrets (passwords and OAuth tokens) live together in a single JSON
object: {"passwords": {"<user>:<service>": "<pw>"}, "tokens": {"<name>": {...}}}.
That object is stored in exactly one place at a time:

- keychain backend: one keyring item (account "vault") - one "Always Allow"
  prompt total, instead of one per secret.
- file backend: ~/.garmin-sync/credentials.json, written 0o600.

Which backend is active follows a three-state rule:

1. A credentials file carrying the "explicit" marker (written only by
   use_file_store, i.e. --use-file-store) always wins.
2. Otherwise a working keychain wins. A stray unmarked file is ignored but
   never deleted, so a file that was not created on purpose cannot silently
   pin the tool to file mode.
3. With no working keychain (e.g. headless Linux), the file is the automatic
   fallback, marker or not.

Credential functions never raise for lack of a keychain: the file backend is
always the fallback, so callers can call get/store/delete unconditionally.
A keychain that exists but cannot be read (locked, access denied) does
raise, so a failed read can never be saved back over the real vault, and
--use-file-store aborts rather than write an empty marker file that would
orphan the unread keychain secrets.

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

# Windows Credential Manager caps one entry at ~2,560 bytes stored as UTF-16,
# roughly 1,280 characters. A vault larger than CHUNK_LIMIT characters is split
# across numbered "vault:i" entries so set_password never fails on Windows.
# MAX_CHUNKS bounds how far a save probes for stale leftover chunks to delete,
# so a corrupt store can never make that scan run away.
CHUNK_LIMIT = 1200
MAX_CHUNKS = 40

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


def _file_store_is_explicit() -> bool:
    """True when CRED_FILE carries the opt-in marker that only
    use_file_store() writes. Malformed content counts as no marker.
    ValueError covers both bad JSON and non-UTF-8 bytes in the file."""
    try:
        data = json.loads(CRED_FILE.read_text())
    except (ValueError, TypeError, OSError):
        return False
    return isinstance(data, dict) and bool(data.get("explicit"))


def _active_backend() -> str:
    """Which backend currently holds the vault (the three-state rule from
    the module docstring). Re-evaluated on every call: the file can appear,
    disappear, or gain the marker between calls."""
    if CRED_FILE.exists():
        if _file_store_is_explicit():
            return "file"
        if not _keyring_available():
            return "file"
        # Unmarked file next to a working keychain: a stray leftover, not an
        # opt-in. Ignore it (never delete it) and stay on the keychain.
        return "keychain"
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
    normalized = {
        "passwords": passwords if isinstance(passwords, dict) else {},
        "tokens": tokens if isinstance(tokens, dict) else {},
    }
    # The opt-in marker must survive every load/save round trip of the file
    # backend, or the first store after --use-file-store would drop it and
    # silently flip the backend back to the keychain.
    if vault.get("explicit"):
        normalized["explicit"] = True
    return normalized


def _load_vault_from_keychain() -> dict:
    import keyring
    try:
        raw = keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT)
    except Exception as e:
        # Returning an empty vault here would let the next read-modify-write
        # save a near-empty vault over the real one. Raising keeps every
        # caller safe; sync/doctor/startup already report exceptions cleanly.
        raise RuntimeError(
            "The system keychain could not be read (it may be locked or "
            "access was denied). Unlock it and retry, or run: "
            "eufy-sync --use-file-store"
        ) from e
    if raw is None:
        return _empty_vault()
    try:
        parsed = json.loads(raw)
        # An oversized vault is stored as a header pointing at numbered chunks;
        # reassemble the payload before normalizing. A missing chunk means the
        # header outlived its payload, which is as unusable as malformed JSON.
        if isinstance(parsed, dict) and "__chunks__" in parsed:
            count = parsed["__chunks__"]
            pieces = []
            for i in range(1, count + 1):
                piece = keyring.get_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{i}")
                if piece is None:
                    raise ValueError("missing vault chunk")
                pieces.append(piece)
            parsed = json.loads("".join(pieces))
        return _normalize_vault(parsed)
    except (ValueError, TypeError):
        logger.warning("Keychain vault contained malformed JSON; treating as empty")
        return _empty_vault()


def _delete_stale_chunks(start: int) -> None:
    # A previous save may have used more chunks than this one. Delete numbered
    # entries from `start` upward until the first gap, so a later read can never
    # reassemble a stale tail. Bounded by MAX_CHUNKS.
    import keyring
    for i in range(start, MAX_CHUNKS + 1):
        account = f"{VAULT_ACCOUNT}:{i}"
        if keyring.get_password(SERVICE_NAME, account) is None:
            break
        try:
            keyring.delete_password(SERVICE_NAME, account)
        except Exception:
            pass


def _save_vault_to_keychain(vault: dict) -> None:
    import keyring
    payload = json.dumps(vault)
    if len(payload) <= CHUNK_LIMIT:
        keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, payload)
        _delete_stale_chunks(1)
        return
    chunks = [payload[i:i + CHUNK_LIMIT] for i in range(0, len(payload), CHUNK_LIMIT)]
    # Chunks first, header last: a reader that races the write sees either the
    # old vault or a complete new one, never a header pointing at a chunk that
    # has not been written yet.
    for i, chunk in enumerate(chunks, start=1):
        keyring.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{i}", chunk)
    keyring.set_password(
        SERVICE_NAME, VAULT_ACCOUNT, json.dumps({"__chunks__": len(chunks)})
    )
    _delete_stale_chunks(len(chunks) + 1)


def _load_vault_from_file() -> dict:
    if not CRED_FILE.exists():
        return _empty_vault()
    try:
        return _normalize_vault(json.loads(CRED_FILE.read_text()))
    except (ValueError, TypeError, OSError):
        logger.warning("Credentials file contained malformed JSON; treating as empty")
        return _empty_vault()


def _save_vault_to_file(vault: dict) -> None:
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Temp file + atomic rename: an interrupted in-place write would truncate
    # the vault, destroying the secrets and the opt-in marker (which would
    # silently flip the backend to an empty keychain on the next run). The
    # temp name carries the pid so two concurrent writers (e.g. the 4-hourly
    # Launch Agent and an interactive command) never share one temp inode and
    # truncate each other's partial write before the rename.
    tmp = CRED_FILE.with_name(f"{CRED_FILE.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(vault, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CRED_FILE)
    except Exception:
        # Leave the previous CRED_FILE untouched; drop the partial temp file.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
    """Adopt the 0o600 file as the permanent credential store.

    Merges the keychain vault with any existing CRED_FILE vault (union of
    both; the currently active store's value wins key conflicts), writes the
    result with the "explicit" opt-in marker, then clears the keychain vault
    item. Idempotent: running it on an already-marked file store rewrites the
    same content.

    Raises RuntimeError, changing nothing, if a keychain exists but cannot be
    read. Writing the marker with an unread keychain would permanently switch
    to a file that does not hold the keychain's secrets, orphaning them; it is
    safer to stop and let the user unlock the keychain and retry.
    """
    active = _active_backend()

    keychain_vault = _empty_vault()
    if _keyring_available():
        try:
            keychain_vault = _load_vault_from_keychain()
        except Exception as e:
            raise RuntimeError(
                "The system keychain could not be read (it may be locked or "
                "access was denied), so its secrets cannot be copied into the "
                "file. Nothing was changed. Unlock the keychain and retry: "
                "eufy-sync --use-file-store"
            ) from e
    file_vault = _load_vault_from_file()

    if active == "keychain":
        winner, loser = keychain_vault, file_vault
    else:
        winner, loser = file_vault, keychain_vault
    merged = {
        "passwords": {**loser["passwords"], **winner["passwords"]},
        "tokens": {**loser["tokens"], **winner["tokens"]},
        "explicit": True,
    }

    # File first, keychain delete second: if the write fails, the keychain
    # copy is still intact. The keychain read above succeeded (or there is no
    # keychain), so deleting the vault item now cannot strand an uncopied
    # secret.
    _save_vault_to_file(merged)

    if _keyring_available():
        try:
            import keyring
            keyring.delete_password(SERVICE_NAME, VAULT_ACCOUNT)
        except Exception:
            pass


def use_keychain_store() -> None:
    """Move the vault into the system keychain and stop using the file.

    Merges the file vault with any existing keychain vault; the currently
    active store's values win conflicts. A marked file is the active store
    being left, so its values win; a stray unmarked file next to a working
    keychain was never active, so it must not overwrite real keychain
    secrets. Strips the "explicit" marker, which only ever belongs in the
    file.

    Raises RuntimeError if no working keyring backend is available.
    """
    if not _keyring_available():
        raise RuntimeError(
            "No system keychain is available on this machine, so credentials "
            "cannot be moved into it. Staying on the file store."
        )

    active = _active_backend()

    file_vault = _load_vault_from_file()
    keychain_vault = _load_vault_from_keychain()
    if active == "file":
        winner, loser = file_vault, keychain_vault
    else:
        winner, loser = keychain_vault, file_vault
    merged = {
        "passwords": {**loser["passwords"], **winner["passwords"]},
        "tokens": {**loser["tokens"], **winner["tokens"]},
    }
    # Keychain first, unlink second: the file is only removed once the
    # keychain holds everything.
    _save_vault_to_keychain(merged)

    if CRED_FILE.exists():
        CRED_FILE.unlink()
