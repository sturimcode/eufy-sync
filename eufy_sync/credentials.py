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

Every change to stored credentials (store, delete, migration, switching
stores) holds the vault lock, an exclusive OS lock on ~/.garmin-sync/
vault.lock, from the moment it reads the vault until its cleanup is done.
Two processes therefore never interleave a read-modify-write: the second one
waits (up to VAULT_LOCK_TIMEOUT seconds) and then works on the first one's
result. A lock that cannot be created or acquired raises VaultLockError and
nothing is written. Reads do not take the lock.
"""
from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from eufy_sync import file_lock

logger = logging.getLogger(__name__)

SERVICE_NAME = "eufy-garmin-sync"
VAULT_ACCOUNT = "vault"

# Windows Credential Manager caps one entry at ~2,560 bytes stored as UTF-16,
# roughly 1,280 characters. A vault larger than CHUNK_LIMIT characters is split
# across "vault:<tag>:<i>" entries so set_password never fails on Windows; the
# "vault" entry then holds a small header naming the tag, the chunk count and a
# checksum. Each save writes its chunks under a fresh tag (the generation
# number plus a random suffix) and switches the header last, so a save killed
# at any step leaves either the old vault or the new one readable, never a
# splice of the two.
#
# The keychain cannot list entries, so the "vault:journal" entry records every
# tag a save may have left chunks under. Saves only run under the vault lock,
# so no other save can be in progress: every journal tag the live header does
# not name is a leftover of a finished or crashed save, and each save deletes
# them all once its header is in place. Released versions wrote chunks as
# "vault:<i>"; those are still read and are deleted by the next save.
#
# MAX_CHUNKS caps how many chunks a save writes (a larger vault is refused
# before anything is written) and so how many a header of this layout may
# claim. Released versions had no cap, so a "vault:<i>" header of any size is
# still read; its chunks end at the first missing one either way.
CHUNK_LIMIT = 1200
MAX_CHUNKS = 40
JOURNAL_ACCOUNT = "vault:journal"
# Each entry is ~20 characters, so this stays well under CHUNK_LIMIT.
MAX_JOURNAL = 24

CRED_FILE = Path.home() / ".garmin-sync" / "credentials.json"

VAULT_LOCK_NAME = "vault.lock"
VAULT_LOCK_TIMEOUT = 30.0


class VaultLockError(RuntimeError):
    """The vault lock could not be created or was not released in time.
    Nothing was written."""


def vault_lock_path() -> Path:
    # Next to CRED_FILE, read at call time: tests redirect CRED_FILE. The
    # file is never deleted in normal use; only --uninstall removes it.
    return CRED_FILE.parent / VAULT_LOCK_NAME


# One holder per process: the thread lock serializes threads (and makes the
# lock reentrant for nested calls such as a migration inside a store), and
# the first entry takes the OS lock that serializes processes.
_thread_lock = threading.RLock()
_lock_depth = 0
_lock_fd: int | None = None


@contextlib.contextmanager
def vault_lock() -> Iterator[None]:
    """Hold the exclusive vault lock for the block. Reentrant within a
    process. Raises VaultLockError, writing nothing, when the lock file
    cannot be opened or another process keeps the lock past
    VAULT_LOCK_TIMEOUT seconds."""
    global _lock_depth, _lock_fd
    deadline = time.monotonic() + VAULT_LOCK_TIMEOUT
    if not _thread_lock.acquire(timeout=VAULT_LOCK_TIMEOUT):
        raise VaultLockError(_VAULT_BUSY)
    try:
        if _lock_depth == 0:
            path = vault_lock_path()
            try:
                fd = file_lock.acquire(path, max(0.0, deadline - time.monotonic()))
            except OSError as e:
                raise VaultLockError(
                    f"The credential lock file {path} could not be created "
                    f"({e.strerror or e}), so nothing was saved. Check that "
                    f"{path.parent} is writable and retry."
                ) from e
            if fd is None:
                raise VaultLockError(_VAULT_BUSY)
            _lock_fd = fd
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
            if _lock_depth == 0:
                fd, _lock_fd = _lock_fd, None
                file_lock.release(fd)
    finally:
        _thread_lock.release()


_VAULT_BUSY = (
    "Another eufy-sync process kept the stored credentials locked for "
    f"{int(VAULT_LOCK_TIMEOUT)} seconds, so nothing was saved. Retry when it "
    "finishes."
)


def _locked(fn: Callable) -> Callable:
    """Run fn under the vault lock."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with vault_lock():
            return fn(*args, **kwargs)
    return wrapper


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


def _file_corrupt(detail: str) -> VaultCorruptError:
    return VaultCorruptError(
        f"The credentials file {CRED_FILE} is damaged ({detail}). "
        "It was left untouched so nothing is saved over it. Repair it or "
        "move it aside, then run eufy-sync to sign in again."
    )


def _read_cred_file() -> dict | None:
    """The parsed CRED_FILE object, or None when there is no file.

    Raises RuntimeError when the file exists but cannot be read, and
    VaultCorruptError when it is not a JSON object. Treating either as
    empty would let the next save replace the file with an empty vault,
    and treating it as unmarked would silently switch to the keychain and
    hide every secret in the file."""
    try:
        text = CRED_FILE.read_text()
    except FileNotFoundError:
        return None
    except ValueError:
        # UnicodeDecodeError: non-UTF-8 bytes.
        raise _file_corrupt("not a JSON object") from None
    except OSError as e:
        raise RuntimeError(
            f"The credentials file {CRED_FILE} could not be read "
            f"({e.strerror or e}). Check its permissions and retry."
        ) from e
    try:
        parsed = json.loads(text)
    except ValueError:
        raise _file_corrupt("not a JSON object") from None
    if not isinstance(parsed, dict):
        raise _file_corrupt("not a JSON object")
    return parsed


def _file_store_is_explicit() -> bool:
    """True when CRED_FILE carries the opt-in marker that only
    use_file_store() writes. Only a file that parses and has no marker
    counts as unmarked; an existing file that cannot be read or parsed
    raises (see _read_cred_file)."""
    data = _read_cred_file()
    return data is not None and bool(data.get("explicit"))


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


def _normalize_vault(vault: dict | None, corrupt: Callable[[str], Exception] | None = None) -> dict:
    """Fill in absent sections of a vault dict.

    A section that is present but not an object raises (via `corrupt`, the
    store's own VaultCorruptError factory): reading it as empty would let the
    next save overwrite whatever is still recoverable in it."""
    if not isinstance(vault, dict):
        return _empty_vault()
    corrupt = corrupt or _keychain_corrupt
    normalized = {}
    for section in ("passwords", "tokens"):
        value = vault.get(section, {})
        if not isinstance(value, dict):
            raise corrupt(f'its "{section}" section is not a JSON object')
        normalized[section] = value
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


def _valid_count(value, limit: int | None = MAX_CHUNKS) -> bool:
    # bool is an int subclass; a header saying {"__chunks__": true} is junk.
    return type(value) is int and value >= 1 and (limit is None or value <= limit)


# "<gen>" (written by pre-release builds of the generation layout) or
# "<gen>.<8 hex>". Anything else is never used to build an account name, so a
# damaged header or journal cannot point a sweep at unrelated entries.
_TAG_RE = re.compile(r"[0-9]{1,9}(\.[0-9a-f]{8})?")


def _valid_tag(value) -> bool:
    return isinstance(value, str) and _TAG_RE.fullmatch(value) is not None


def _parse_header(raw: str) -> tuple:
    """Classify the "vault" entry. Returns one of:

    ("single", vault)                 the whole vault in one entry
    ("legacy", count)                 released-version chunks "vault:1".."vault:<count>"
    ("gen", tag, count, sha256, gen)  chunks "vault:<tag>:1".."vault:<tag>:<count>"

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
            # Headers written before chunk names carried a random suffix
            # have no "tag"; their chunks are named by the generation alone.
            tag = meta.get("tag", str(meta["gen"]))
            if _valid_tag(tag):
                return ("gen", tag, meta["chunks"], meta["sha256"], meta["gen"])
        raise _keychain_corrupt("the chunk header is malformed")
    if "__chunks__" in parsed:
        # Released versions wrote any number of chunks; reading stops at the
        # first missing one, so no cap is needed to keep this bounded.
        if _valid_count(parsed["__chunks__"], limit=None):
            return ("legacy", parsed["__chunks__"])
        raise _keychain_corrupt("the chunk header is malformed")
    return ("single", parsed)


def _chunk_account(tag: str | None, i: int) -> str:
    # tag None is the layout released versions wrote ("vault:1", "vault:2").
    if tag is None:
        return f"{VAULT_ACCOUNT}:{i}"
    return f"{VAULT_ACCOUNT}:{tag}:{i}"


def _assemble(layout: tuple) -> dict:
    """Turn a parsed header into a vault dict, reading chunks as needed."""
    if layout[0] == "single":
        return _normalize_vault(layout[1])
    if layout[0] == "legacy":
        tag, count, digest = None, layout[1], None
    else:
        _, tag, count, digest, _ = layout
    pieces = []
    for i in range(1, count + 1):
        # A keyring failure here (locked, access denied) raises the
        # unreadable-keychain RuntimeError, same as the header read. A chunk
        # that comes back None is genuinely missing: the vault is damaged.
        piece = _keychain_get(_chunk_account(tag, i))
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
        # Reads do not take the vault lock, so a save in another process may
        # have switched the header and deleted the chunks this read was
        # partway through. If the header moved on, read the new vault once;
        # if it did not, the vault really is damaged.
        fresh = _keychain_get(VAULT_ACCOUNT)
        if fresh is None or fresh == raw:
            raise
        return _assemble(_parse_header(fresh))


def _delete_chunk_run(tag: str | None) -> None:
    # Delete one tag's chunk entries from 1 up to the first gap. Chunks are
    # always written 1..N in order, so leftovers form an unbroken run.
    # Deleting from the top down, and stopping at the first failed delete,
    # keeps what is left unbroken, so a later sweep still finds all of it.
    # Raises on that failure so the caller keeps the tag in the journal.
    # Tagged runs are bounded by MAX_CHUNKS; released-version runs had no
    # cap and end at their first gap.
    import keyring
    run = []
    i = 1
    while tag is None or i <= MAX_CHUNKS:
        account = _chunk_account(tag, i)
        if keyring.get_password(SERVICE_NAME, account) is None:
            break
        run.append(account)
        i += 1
    for account in reversed(run):
        keyring.delete_password(SERVICE_NAME, account)


def _read_header() -> tuple[str | None, tuple | None]:
    """The raw "vault" entry and its parsed layout. The layout is None when
    the entry is absent or this module cannot parse it."""
    raw = _keychain_get(VAULT_ACCOUNT)
    if raw is None:
        return None, None
    try:
        return raw, _parse_header(raw)
    except VaultCorruptError:
        return raw, None


def _layout_tag(layout: tuple | None) -> str | None:
    """The chunk tag a parsed header points at, if any. A single-entry vault
    written by a pre-release build may carry "__gen__", the generation whose
    chunks it had just replaced; those may still need sweeping."""
    if layout is None:
        return None
    if layout[0] == "gen":
        return layout[1]
    if layout[0] == "single":
        gen = layout[1].get("__gen__")
        if type(gen) is int and gen >= 1:
            return str(gen)
    return None


def _layout_gen(layout: tuple | None) -> int:
    if layout is not None and layout[0] == "gen":
        return layout[4]
    return 0


def _read_journal() -> list[str]:
    """Tags that may still have chunk entries, oldest first. Best-effort: an
    unreadable or malformed journal reads as empty. Pre-release builds
    stored {tag: timestamp}; its keys are read the same way."""
    import keyring
    try:
        raw = keyring.get_password(SERVICE_NAME, JOURNAL_ACCOUNT)
        parsed = json.loads(raw) if raw else []
    except Exception:
        return []
    if isinstance(parsed, dict):
        parsed = list(parsed)
    if not isinstance(parsed, list):
        return []
    return list(dict.fromkeys(tag for tag in parsed if _valid_tag(tag)))


def _write_journal(tags: list[str]) -> None:
    import keyring
    if not tags:
        try:
            keyring.delete_password(SERVICE_NAME, JOURNAL_ACCOUNT)
        except Exception:
            pass
        return
    keyring.set_password(SERVICE_NAME, JOURNAL_ACCOUNT, json.dumps(tags[-MAX_JOURNAL:]))


def _journal_add(tags: list[str]) -> None:
    journal = _read_journal()
    added = [tag for tag in tags if tag not in journal]
    if added:
        _write_journal(journal + added)


def _sweep_chunks() -> None:
    """Best-effort removal of chunk entries that no header points at.

    Runs under the vault lock after a save has switched the header. No other
    save can be running, so every journal tag except the one the header
    names is a leftover and its chunks are deleted, along with the
    released-version "vault:i" chunks unless the header still uses them. A
    tag whose chunks could not all be deleted stays in the journal for the
    next save."""
    try:
        raw, layout = _read_header()
        if layout is None and raw is not None:
            # A header this module cannot parse might still point somewhere;
            # deleting nothing is the safe answer.
            return
        current = layout[1] if layout is not None and layout[0] == "gen" else None
        if layout is None or layout[0] != "legacy":
            _delete_chunk_run(None)
        remaining = []
        for tag in _read_journal():
            if tag == current:
                continue
            try:
                _delete_chunk_run(tag)
            except Exception:
                remaining.append(tag)
        _write_journal(remaining)
    except Exception:
        # The new vault is already in place; leftovers cost tidiness, not
        # data, so a failed cleanup must not fail the save.
        pass


class VaultTooLargeError(RuntimeError):
    """The vault is too large to store in the keychain. Nothing was written."""


@_locked
def _save_vault_to_keychain(vault: dict) -> None:
    import keyring
    # json.dumps escapes non-ASCII by default, so each character is one
    # UTF-16 unit and a CHUNK_LIMIT-character entry stays under the Windows cap.
    payload = json.dumps(vault)
    chunks = []
    if len(payload) > CHUNK_LIMIT:
        chunks = [payload[i:i + CHUNK_LIMIT] for i in range(0, len(payload), CHUNK_LIMIT)]
    if len(chunks) > MAX_CHUNKS:
        # Checked before any write: a header claiming more chunks than the
        # reader accepts would commit a vault nobody can read.
        raise VaultTooLargeError(
            f"The stored credentials are too large for the system keychain "
            f"({len(payload)} characters; the limit is "
            f"{MAX_CHUNKS * CHUNK_LIMIT}). Nothing was changed. Run "
            "eufy-sync --use-file-store to keep them in a file instead."
        )

    _, start_layout = _read_header()
    pending = []
    prev_tag = _layout_tag(start_layout)
    if prev_tag is not None:
        # Recorded before the switch, so a sweep a crash cuts short is
        # finished by a later save.
        pending.append(prev_tag)
        if "." not in prev_tag:
            # Pre-release generation layout: a crashed save there left its
            # chunks at the neighbouring generation numbers.
            gen = int(prev_tag)
            pending.extend(str(n) for n in (gen - 1, gen + 1) if n >= 1)

    if not chunks:
        if pending:
            _journal_add(pending)
        keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, payload)
        _sweep_chunks()
        return

    # The new chunks go under a tag no header references, and the header
    # write is the commit point. A save killed before it leaves the old vault
    # readable; one killed after it leaves the new vault readable. Either way
    # the journal names the leftovers for the next save to delete.
    gen = _layout_gen(start_layout) + 1
    tag = f"{gen}.{secrets.token_hex(4)}"
    _journal_add(pending + [tag])
    try:
        for i, chunk in enumerate(chunks, start=1):
            keyring.set_password(SERVICE_NAME, _chunk_account(tag, i), chunk)
    except Exception:
        try:
            _delete_chunk_run(tag)
            _write_journal([t for t in _read_journal() if t != tag])
        except Exception:
            pass
        raise
    header = {
        "__vault__": {
            "gen": gen,
            "tag": tag,
            "chunks": len(chunks),
            "sha256": hashlib.sha256(payload.encode()).hexdigest(),
        }
    }
    keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps(header))
    _sweep_chunks()


@_locked
def _delete_keychain_vault() -> None:
    """Best-effort removal of the vault header, every chunk entry this
    module can find, and the journal."""
    import keyring
    try:
        _, layout = _read_header()
    except Exception:
        layout = None
    try:
        keyring.delete_password(SERVICE_NAME, VAULT_ACCOUNT)
    except Exception:
        pass
    tags = set()
    tag = _layout_tag(layout)
    if tag is not None:
        tags.add(tag)
        if "." not in tag:
            tags.update(str(g) for g in (int(tag) - 1, int(tag) + 1) if g >= 1)
    try:
        tags.update(_read_journal())
    except Exception:
        pass
    for tag in tags:
        try:
            _delete_chunk_run(tag)
        except Exception:
            pass
    try:
        # Released-version chunks an earlier install left behind.
        _delete_chunk_run(None)
    except Exception:
        pass
    try:
        keyring.delete_password(SERVICE_NAME, JOURNAL_ACCOUNT)
    except Exception:
        pass


def _load_vault_from_file() -> dict:
    parsed = _read_cred_file()
    if parsed is None:
        return _empty_vault()
    return _normalize_vault(parsed, _file_corrupt)


@_locked
def _save_vault_to_file(vault: dict) -> None:
    CRED_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Temp file + atomic rename: an interrupted in-place write would truncate
    # the vault, destroying the secrets and the opt-in marker (which would
    # silently flip the backend to an empty keychain on the next run). The
    # pid in the temp name is a second guard behind the vault lock, which
    # already keeps two writers from running at once.
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


@_locked
def _save_vault(vault: dict) -> None:
    if _active_backend() == "file":
        _save_vault_to_file(vault)
    else:
        _save_vault_to_keychain(vault)


# --- Public API: passwords ---------------------------------------------------


def get_password(account: str, migrate: bool = True) -> str | None:
    """Return a stored password, migrating it from the legacy keychain item
    (one account per password) into the vault on first access.

    migrate=False still returns a legacy item but leaves it where it is, so a
    caller that does not hold the sync lock never writes the vault."""
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
            if not migrate:
                return legacy
            with vault_lock():
                # Re-read under the lock: another process may have migrated
                # (or changed) this password since the read above.
                vault = _load_vault()
                if account not in vault["passwords"]:
                    vault["passwords"][account] = legacy
                    _save_vault(vault)
                try:
                    keyring.delete_password(SERVICE_NAME, account)
                except Exception:
                    pass
                return vault["passwords"][account]

    return None


@_locked
def store_password(account: str, password: str) -> None:
    """Store a password in the vault (keychain or file, whichever is active)."""
    vault = _load_vault()
    vault["passwords"][account] = password
    _save_vault(vault)


@_locked
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
            with vault_lock():
                # Re-read under the lock, as in get_password.
                vault = _load_vault()
                if name not in vault["tokens"]:
                    vault["tokens"][name] = legacy
                    _save_vault(vault)
                try:
                    keyring.delete_password(SERVICE_NAME, f"token:{name}")
                except Exception:
                    pass
                return vault["tokens"][name]

    return None


@_locked
def store_token(name: str, data: dict) -> None:
    """Store a token dict in the vault (keychain or file, whichever is active)."""
    vault = _load_vault()
    vault["tokens"][name] = data
    _save_vault(vault)


@_locked
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


@_locked
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


@_locked
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
