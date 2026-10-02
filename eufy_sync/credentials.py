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
orphan the unread keychain secrets. A vault that is present but damaged
(unparseable JSON, a missing chunk) raises VaultCorruptError, a RuntimeError,
for the same reason: reading it as empty would let the next save wipe it.

A lazy, one-time migration promotes secrets from the old per-item keychain
layout (one keyring account per password/token) into the vault the first
time each one is looked up.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

SERVICE_NAME = "eufy-garmin-sync"
VAULT_ACCOUNT = "vault"

# Windows Credential Manager caps one entry at ~2,560 bytes stored as UTF-16,
# roughly 1,280 characters. A vault larger than CHUNK_LIMIT characters is split
# across "vault:<gen>:<i>" entries so set_password never fails on Windows; the
# "vault" entry then holds a small header naming the generation, the chunk
# count and a checksum. Each save writes a fresh generation and switches the
# header last, so an interrupted save never splices two vaults together.
# Released versions wrote chunks as "vault:<i>"; those are still read and are
# replaced on the next save. MAX_CHUNKS bounds the chunk count a header may
# claim and how far a save probes for leftovers to delete, so a corrupt store
# can never make either run away.
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


_KEYCHAIN_UNREADABLE = (
    "The system keychain could not be read (it may be locked or "
    "access was denied). Unlock it and retry, or run: "
    "eufy-sync --use-file-store"
)


class VaultCorruptError(RuntimeError):
    """The stored vault exists but cannot be parsed or reassembled.

    Raised instead of returning an empty vault: an empty result would be
    saved back by the next store_*() call, wiping every stored secret. It is
    a RuntimeError, so callers that already handle an unreadable keychain
    handle this the same way."""


def _keychain_corrupt(detail: str) -> VaultCorruptError:
    return VaultCorruptError(
        f"The credential vault in the system keychain is damaged ({detail}). "
        "It was left untouched so nothing is saved over it. To start over, "
        f'delete the "{VAULT_ACCOUNT}" item for "{SERVICE_NAME}" in your '
        "keychain app, then run eufy-sync to sign in again."
    )


def _keychain_get(account: str) -> str | None:
    import keyring
    try:
        return keyring.get_password(SERVICE_NAME, account)
    except Exception as e:
        # Returning an empty vault here would let the next read-modify-write
        # save a near-empty vault over the real one. Raising keeps every
        # caller safe; sync/doctor/startup already report exceptions cleanly.
        raise RuntimeError(_KEYCHAIN_UNREADABLE) from e


def _valid_count(value) -> bool:
    # bool is an int subclass; a header saying {"__chunks__": true} is junk.
    return type(value) is int and 1 <= value <= MAX_CHUNKS


def _parse_header(raw: str) -> tuple:
    """Classify the "vault" entry. Returns one of:

    ("single", vault)            the whole vault in one entry
    ("legacy", count)            released-version chunks "vault:1".."vault:<count>"
    ("gen", gen, count, sha256)  chunks "vault:<gen>:1".."vault:<gen>:<count>"

    Raises VaultCorruptError for anything else."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        raise _keychain_corrupt("the vault entry is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise _keychain_corrupt("the vault entry is not a JSON object")
    if "__vault__" in parsed:
        meta = parsed["__vault__"]
        if (
            isinstance(meta, dict)
            and type(meta.get("gen")) is int
            and meta["gen"] >= 1
            and _valid_count(meta.get("chunks"))
            and isinstance(meta.get("sha256"), str)
        ):
            return ("gen", meta["gen"], meta["chunks"], meta["sha256"])
        raise _keychain_corrupt("the chunk header is malformed")
    if "__chunks__" in parsed:
        if _valid_count(parsed["__chunks__"]):
            return ("legacy", parsed["__chunks__"])
        raise _keychain_corrupt("the chunk header is malformed")
    return ("single", parsed)


def _chunk_account(gen: int | None, i: int) -> str:
    # gen None is the layout released versions wrote ("vault:1", "vault:2").
    if gen is None:
        return f"{VAULT_ACCOUNT}:{i}"
    return f"{VAULT_ACCOUNT}:{gen}:{i}"


def _assemble(layout: tuple) -> dict:
    """Turn a parsed header into a vault dict, reading chunks as needed."""
    if layout[0] == "single":
        return _normalize_vault(layout[1])
    if layout[0] == "legacy":
        gen, count, digest = None, layout[1], None
    else:
        _, gen, count, digest = layout
    pieces = []
    for i in range(1, count + 1):
        # A keyring failure here (locked, access denied) raises the
        # unreadable-keychain RuntimeError, same as the header read. A chunk
        # that comes back None is genuinely missing: the vault is damaged.
        piece = _keychain_get(_chunk_account(gen, i))
        if piece is None:
            raise _keychain_corrupt(f"chunk {i} of {count} is missing")
        pieces.append(piece)
    payload = "".join(pieces)
    if digest is not None and hashlib.sha256(payload.encode()).hexdigest() != digest:
        raise _keychain_corrupt("the chunks do not match their header")
    try:
        parsed = json.loads(payload)
    except (ValueError, TypeError):
        raise _keychain_corrupt("the reassembled vault is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise _keychain_corrupt("the reassembled vault is not a JSON object")
    return _normalize_vault(parsed)


def _load_vault_from_keychain() -> dict:
    raw = _keychain_get(VAULT_ACCOUNT)
    if raw is None:
        return _empty_vault()
    try:
        return _assemble(_parse_header(raw))
    except VaultCorruptError:
        # A concurrent save may have switched the header and deleted the
        # chunks this read was partway through. If the header moved on, read
        # the new vault once; if it did not, the vault really is damaged.
        fresh = _keychain_get(VAULT_ACCOUNT)
        if fresh is None or fresh == raw:
            raise
        return _assemble(_parse_header(fresh))


def _delete_chunk_run(gen: int | None, start: int) -> None:
    # Delete one generation's chunk entries from `start` up to the first gap.
    # Chunks are always written 1..N in order, so leftovers from an
    # interrupted save form an unbroken run. Deleting from the top down keeps
    # it unbroken if this sweep is itself cut short, so the next sweep still
    # finds the rest. Bounded by MAX_CHUNKS.
    import keyring
    run = []
    for i in range(start, MAX_CHUNKS + 1):
        account = _chunk_account(gen, i)
        if keyring.get_password(SERVICE_NAME, account) is None:
            break
        run.append(account)
    for account in reversed(run):
        try:
            keyring.delete_password(SERVICE_NAME, account)
        except Exception:
            pass


def _sweep_chunks(prev_gen: int, keep_gen: int | None = None, keep_count: int = 0) -> None:
    """Best-effort removal of chunk entries that no header points at.

    prev_gen is the generation the header named before this change (0 when
    it named none). Its neighbours are swept too: prev_gen - 1 catches a
    cleanup a crash cut short, and prev_gen + 1 catches chunks a crashed save
    wrote before it could switch the header. keep_gen/keep_count protect the
    chunks the header now references. The released-version "vault:i" names
    are probed until a save after generation 1 (the first one written after
    them) has swept them."""
    try:
        if prev_gen <= 1:
            _delete_chunk_run(None, 1)
        for gen in (prev_gen - 1, prev_gen, prev_gen + 1):
            if gen < 1:
                continue
            _delete_chunk_run(gen, keep_count + 1 if gen == keep_gen else 1)
    except Exception:
        # The new vault is already in place; leftovers cost tidiness, not
        # data, so a failed cleanup must not fail the save.
        pass


def _current_gen() -> int:
    """The generation the stored header names, or 0 for none. A header this
    module cannot parse counts as 0: saves only ever follow a successful
    load, so the next sweep still covers whatever it can identify."""
    raw = _keychain_get(VAULT_ACCOUNT)
    if raw is None:
        return 0
    try:
        layout = _parse_header(raw)
    except VaultCorruptError:
        return 0
    if layout[0] == "gen":
        return layout[1]
    if layout[0] == "single":
        gen = layout[1].get("__gen__")
        return gen if type(gen) is int and gen >= 1 else 0
    return 0


def _save_vault_to_keychain(vault: dict) -> None:
    import keyring
    prev_gen = _current_gen()
    # json.dumps escapes non-ASCII by default, so each character is one
    # UTF-16 unit and a CHUNK_LIMIT-character entry stays under the Windows cap.
    payload = json.dumps(vault)
    # A vault that shrinks back into one entry keeps the last chunk generation
    # as "__gen__" (dropped on load), so a sweep that a crash cuts short can
    # still find that generation's leftovers on the next save.
    single = json.dumps({**vault, "__gen__": prev_gen}) if prev_gen else payload
    if len(single) <= CHUNK_LIMIT:
        keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, single)
        _sweep_chunks(prev_gen)
        return
    # The new chunks go under a generation no header references yet, and the
    # single-entry header write is the commit point. A save killed before it
    # leaves the old vault readable; one killed after it leaves the new vault
    # readable plus leftovers that the next save sweeps.
    gen = prev_gen + 1
    chunks = [payload[i:i + CHUNK_LIMIT] for i in range(0, len(payload), CHUNK_LIMIT)]
    for i, chunk in enumerate(chunks, start=1):
        keyring.set_password(SERVICE_NAME, _chunk_account(gen, i), chunk)
    header = {
        "__vault__": {
            "gen": gen,
            "chunks": len(chunks),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
    }
    keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps(header))
    _sweep_chunks(prev_gen, keep_gen=gen, keep_count=len(chunks))


def _delete_keychain_vault() -> None:
    """Best-effort removal of the vault header and every chunk entry."""
    import keyring
    try:
        prev_gen = _current_gen()
    except Exception:
        prev_gen = 0
    try:
        keyring.delete_password(SERVICE_NAME, VAULT_ACCOUNT)
    except Exception:
        pass
    _sweep_chunks(prev_gen)
    if prev_gen:
        # Released-version chunks an earlier install left behind.
        try:
            _delete_chunk_run(None, 1)
        except Exception:
            pass


def _load_vault_from_file() -> dict:
    try:
        text = CRED_FILE.read_text()
    except FileNotFoundError:
        return _empty_vault()
    except ValueError:
        parsed = None  # non-UTF-8 bytes
    except OSError as e:
        raise RuntimeError(
            f"The credentials file {CRED_FILE} could not be read "
            f"({e.strerror or e}). Check its permissions and retry."
        ) from e
    else:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
    if not isinstance(parsed, dict):
        # Treating this as empty would let the next save replace the file
        # with an empty vault, destroying whatever is still recoverable in it.
        raise VaultCorruptError(
            f"The credentials file {CRED_FILE} is damaged (not a JSON object). "
            "It was left untouched so nothing is saved over it. Repair it or "
            "move it aside, then run eufy-sync to sign in again."
        )
    return _normalize_vault(parsed)


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
        except VaultCorruptError:
            # Already says what is damaged and that nothing was changed.
            raise
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
        # An oversized vault also has chunk entries, each holding a slice of
        # the same plaintext secrets. Deleting only the header hides them from
        # every reader but leaves full copies in the keychain the user just
        # opted out of, so the chunks go too. Best-effort: the file already
        # holds the merged vault, so a failure here must not fail the switch.
        _delete_keychain_vault()


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
