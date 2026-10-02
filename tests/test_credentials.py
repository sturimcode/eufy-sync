from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from unittest.mock import patch

import pytest

from eufy_sync import credentials
from eufy_sync.credentials import (
    CHUNK_LIMIT,
    SERVICE_NAME,
    VAULT_ACCOUNT,
    _keyring_available,
    get_token,
    store_token,
)


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


# --- Fake in-memory keyring backend -----------------------------------------
#
# A dict-backed fake standing in for the real `keyring` module. Every test
# that needs "a keychain" installs this via the `fake_keyring` fixture below,
# which patches keyring.get_password/set_password/delete_password AND makes
# _keyring_available() report True (a working backend).


class _FakeKeyringStore:
    """In-memory (service, account) -> password store, mimicking keyring's API."""

    def __init__(self):
        self.data: dict[tuple[str, str], str] = {}

    def set_password(self, service, account, password):
        self.data[(service, account)] = password

    def get_password(self, service, account):
        return self.data.get((service, account))

    def delete_password(self, service, account):
        import keyring
        try:
            del self.data[(service, account)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError("not found") from None

    def accounts_written(self) -> set[str]:
        """Every distinct account name ever set_password'd, service-agnostic."""
        return {account for (_service, account) in self.data.keys()}


@pytest.fixture
def fake_keyring(monkeypatch):
    """Install a working fake keyring backend and make _keyring_available() True."""
    store = _FakeKeyringStore()
    monkeypatch.setattr("keyring.set_password", store.set_password)
    monkeypatch.setattr("keyring.get_password", store.get_password)
    monkeypatch.setattr("keyring.delete_password", store.delete_password)
    monkeypatch.setattr("eufy_sync.credentials._keyring_available", lambda: True)
    return store


@pytest.fixture
def no_keyring(monkeypatch):
    """Make _keyring_available() False, as on headless Linux with no backend."""
    monkeypatch.setattr("eufy_sync.credentials._keyring_available", lambda: False)


@pytest.fixture
def cred_file(tmp_path, monkeypatch):
    """Point CRED_FILE at a throwaway path under tmp_path (not created yet)."""
    path = tmp_path / ".garmin-sync" / "credentials.json"
    monkeypatch.setattr("eufy_sync.credentials.CRED_FILE", path)
    return path


# --- 1. keychain backend round-trip; only ONE keyring account written -------


def test_keychain_backend_round_trip_password(fake_keyring, cred_file):
    from eufy_sync.credentials import delete_password, get_password, store_password

    store_password("default:eufy", "hunter2")
    assert get_password("default:eufy") == "hunter2"

    delete_password("default:eufy")
    assert get_password("default:eufy") is None


def test_keychain_backend_round_trip_token(fake_keyring, cred_file):
    from eufy_sync.credentials import delete_token, get_token, store_token

    store_token("garmin", {"di_token": "abc"})
    assert get_token("garmin") == {"di_token": "abc"}

    delete_token("garmin")
    assert get_token("garmin") is None


def test_only_one_keyring_account_is_ever_written(fake_keyring, cred_file):
    """The whole point of the consolidation: no matter how many passwords or
    tokens are stored, exactly one keyring account ("vault") receives writes -
    never a separate account per secret."""
    from eufy_sync.credentials import store_password, store_token

    store_password("default:eufy", "pw1")
    store_password("default:garmin", "pw2")
    store_token("eufy", {"access_token": "a"})
    store_token("garmin", {"di_token": "b"})
    store_token("strava", {"access_token": "c"})

    assert fake_keyring.accounts_written() == {"vault"}


def test_storing_a_second_secret_does_not_clobber_the_first(fake_keyring, cred_file):
    """Read-modify-write integrity: every secret shares one vault, so a store
    must load, add one key, and save the whole object. If it wrote only the new
    key, the earlier secrets would vanish."""
    from eufy_sync.credentials import get_password, get_token, store_password, store_token

    store_password("default:eufy", "pw1")
    store_token("garmin", {"di_token": "b"})
    store_password("default:garmin", "pw2")   # later writes must not drop the earlier keys

    assert get_password("default:eufy") == "pw1"
    assert get_password("default:garmin") == "pw2"
    assert get_token("garmin") == {"di_token": "b"}


# --- 2. file backend round-trip; 0o600; keyring never touched ---------------


def test_file_backend_round_trip(no_keyring, cred_file):
    from eufy_sync.credentials import delete_password, get_password, get_token, store_password, store_token

    store_password("default:eufy", "hunter2")
    assert get_password("default:eufy") == "hunter2"
    store_token("eufy", {"access_token": "tok"})
    assert get_token("eufy") == {"access_token": "tok"}

    delete_password("default:eufy")
    assert get_password("default:eufy") is None


def test_file_backend_creates_0o600_file_with_json(no_keyring, cred_file):
    from eufy_sync.credentials import store_password

    store_password("default:eufy", "hunter2")

    assert cred_file.exists()
    # POSIX modes only; Windows reports 666/777 regardless of the mode passed.
    if os.name != "nt":
        mode = stat.S_IMODE(cred_file.stat().st_mode)
        assert mode == 0o600

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["passwords"]["default:eufy"] == "hunter2"


def test_file_backend_never_touches_keyring(no_keyring, cred_file):
    """When the file backend is active, keyring.set_password/get_password
    must never be called - prevents a hybrid state where secrets leak into
    both places."""
    from eufy_sync.credentials import get_password, get_token, store_password, store_token

    with patch("keyring.set_password") as mock_set, patch("keyring.get_password") as mock_get:
        store_password("default:eufy", "hunter2")
        get_password("default:eufy")
        store_token("garmin", {"di_token": "x"})
        get_token("garmin")

    mock_set.assert_not_called()
    mock_get.assert_not_called()


# --- 3. lazy migration -------------------------------------------------------


def test_lazy_migration_promotes_legacy_password_then_reads_vault_only(fake_keyring, cred_file):
    import keyring

    from eufy_sync.credentials import SERVICE_NAME, get_password

    # Seed the legacy single-item layout directly via the fake keyring.
    keyring.set_password(SERVICE_NAME, "default:eufy", "legacy-pw")

    # First get(): promotes into the vault and deletes the legacy item.
    assert get_password("default:eufy") == "legacy-pw"
    assert keyring.get_password(SERVICE_NAME, "default:eufy") is None
    assert fake_keyring.accounts_written() == {"vault"}

    # Second get(): reads only the vault (legacy item already gone).
    assert get_password("default:eufy") == "legacy-pw"


def test_lazy_migration_promotes_legacy_token(fake_keyring, cred_file):
    import keyring

    from eufy_sync.credentials import SERVICE_NAME, get_token

    keyring.set_password(SERVICE_NAME, "token:garmin", json.dumps({"di_token": "abc"}))

    assert get_token("garmin") == {"di_token": "abc"}
    assert keyring.get_password(SERVICE_NAME, "token:garmin") is None

    # Second call: legacy item gone, still resolves from the vault.
    assert get_token("garmin") == {"di_token": "abc"}


# --- 4. use_file_store() -----------------------------------------------------


def test_use_file_store_moves_vault_from_keychain_to_file(fake_keyring, cred_file):
    import keyring

    from eufy_sync.credentials import (
        SERVICE_NAME,
        _active_backend,
        get_password,
        get_token,
        store_password,
        store_token,
        use_file_store,
    )

    store_password("default:eufy", "pw1")
    store_token("garmin", {"di_token": "abc"})

    use_file_store()

    assert cred_file.exists()
    # POSIX modes only; Windows reports 666/777 regardless of the mode passed.
    if os.name != "nt":
        mode = stat.S_IMODE(cred_file.stat().st_mode)
        assert mode == 0o600
    on_disk = json.loads(cred_file.read_text())
    assert on_disk["passwords"]["default:eufy"] == "pw1"
    assert on_disk["tokens"]["garmin"] == {"di_token": "abc"}

    # Keychain vault item cleared.
    assert keyring.get_password(SERVICE_NAME, "vault") is None

    # The file carries the opt-in marker: only use_file_store writes it, and
    # it is what makes the file win over a working keychain from now on.
    assert on_disk["explicit"] is True

    assert _active_backend() == "file"
    # Reads now come from the file, unaffected by the (cleared) keychain.
    assert get_password("default:eufy") == "pw1"
    assert get_token("garmin") == {"di_token": "abc"}


# --- 5. use_keychain_store() -------------------------------------------------


def test_use_keychain_store_moves_vault_from_file_to_keychain(fake_keyring, cred_file):
    from eufy_sync.credentials import (
        _active_backend,
        get_password,
        get_token,
        store_password,
        store_token,
        use_file_store,
        use_keychain_store,
    )

    store_password("default:eufy", "pw1")
    store_token("garmin", {"di_token": "abc"})
    use_file_store()
    assert _active_backend() == "file"

    use_keychain_store()

    assert not cred_file.exists()
    assert _active_backend() == "keychain"
    assert get_password("default:eufy") == "pw1"
    assert get_token("garmin") == {"di_token": "abc"}


def test_use_keychain_store_raises_cleanly_when_keyring_unavailable(no_keyring, cred_file):
    from eufy_sync.credentials import use_keychain_store

    with pytest.raises(RuntimeError):
        use_keychain_store()


# --- 6. auto-fallback (headless token persistence) --------------------------


def test_auto_fallback_creates_file_and_persists_token_with_no_keychain(no_keyring, cred_file):
    """The headless-Linux fix: with no keychain and no CRED_FILE yet,
    store_token must still persist (to a 0o600 file), not silently no-op."""
    from eufy_sync.credentials import _active_backend, get_token, store_token

    assert not cred_file.exists()
    assert _active_backend() == "file"

    store_token("eufy", {"access_token": "headless-tok"})

    assert cred_file.exists()
    # POSIX modes only; Windows reports 666/777 regardless of the mode passed.
    if os.name != "nt":
        mode = stat.S_IMODE(cred_file.stat().st_mode)
        assert mode == 0o600
    assert get_token("eufy") == {"access_token": "headless-tok"}


# --- 7. malformed vault JSON --------------------------------------------------


def test_malformed_file_vault_raises_and_is_never_overwritten(no_keyring, cred_file):
    """Reading a damaged file as empty would let the next store save an
    empty vault over it. The read raises instead, and the file is untouched."""
    from eufy_sync.credentials import VaultCorruptError, get_password, get_token

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("{not valid json::")

    with pytest.raises(VaultCorruptError, match="damaged"):
        get_password("default:eufy")
    with pytest.raises(RuntimeError):
        get_token("eufy")
    with pytest.raises(RuntimeError):
        store_token("eufy", {"access_token": "new"})

    assert cred_file.read_text() == "{not valid json::"


def test_non_object_file_vault_raises(no_keyring, cred_file):
    from eufy_sync.credentials import VaultCorruptError

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("[1, 2]")

    with pytest.raises(VaultCorruptError):
        get_token("eufy")


def test_malformed_keychain_vault_raises_and_is_never_overwritten(fake_keyring, cred_file):
    import keyring

    from eufy_sync.credentials import SERVICE_NAME, VaultCorruptError, get_password

    keyring.set_password(SERVICE_NAME, "vault", "{not valid json::")

    with pytest.raises(VaultCorruptError, match="damaged"):
        get_password("default:eufy")
    with pytest.raises(VaultCorruptError):
        store_token("garmin", {"di_token": "new"})

    assert fake_keyring.get_password(SERVICE_NAME, "vault") == "{not valid json::"


# --- 8. backward compat: legacy config.yaml inline password -----------------


def test_config_get_password_falls_back_to_yaml_password(no_keyring, cred_file):
    from eufy_sync.config import _get_password

    result = _get_password("default", "eufy", "e@example.com", "yaml-inline-pw")
    assert result == "yaml-inline-pw"


def test_config_get_password_prefers_vault_over_yaml(fake_keyring, cred_file):
    from eufy_sync.config import _get_password
    from eufy_sync.credentials import store_password

    store_password("default:eufy", "vault-pw")
    result = _get_password("default", "eufy", "e@example.com", "yaml-inline-pw")
    assert result == "vault-pw"


# --- 9. CLI wiring: --use-file-store / --use-keychain -----------------------


def test_cli_use_file_store_exits_zero_and_prints_confirmation(tmp_path, capsys):
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--use-file-store"]

    with patch("sys.argv", argv), \
         patch("eufy_sync.credentials.use_file_store") as mock_use, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    mock_use.assert_called_once()
    out = capsys.readouterr().out
    assert out.strip() != ""


def test_cli_use_keychain_exits_zero_and_prints_confirmation(tmp_path, capsys):
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--use-keychain"]

    with patch("sys.argv", argv), \
         patch("eufy_sync.credentials.use_keychain_store") as mock_use, \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    mock_use.assert_called_once()
    out = capsys.readouterr().out
    assert out.strip() != ""


def test_cli_use_keychain_exits_one_with_message_when_no_keychain(tmp_path, capsys):
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--use-keychain"]

    with patch("sys.argv", argv), \
         patch("eufy_sync.credentials.use_keychain_store", side_effect=RuntimeError("no keychain backend available")), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "no keychain backend available" in out


def test_cli_use_file_store_exits_one_when_keychain_unreadable(tmp_path, capsys):
    """An unreadable keychain makes use_file_store abort with RuntimeError;
    the CLI must surface the message and exit 1, not dump a traceback."""
    from eufy_sync.cli.app import main

    config_path = tmp_path / "config.yaml"
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--use-file-store"]

    with patch("sys.argv", argv), \
         patch("eufy_sync.credentials.use_file_store", side_effect=RuntimeError("keychain could not be read")), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "keychain could not be read" in out


# --- 10. explicit opt-in: backend selection ----------------------------------
#
# A credentials file only overrides a working keychain when it carries the
# "explicit" marker that use_file_store() writes. An unmarked file is either
# the headless auto-fallback (no keychain: keep using it) or a stray leftover
# (keychain works: ignore it, never delete it).


def test_unmarked_file_with_keyring_is_ignored(fake_keyring, cred_file):
    from eufy_sync.credentials import _active_backend, get_password, store_password

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({"passwords": {"default:eufy": "stray-pw"}, "tokens": {}}))

    assert _active_backend() == "keychain"

    store_password("default:eufy", "keychain-pw")
    assert get_password("default:eufy") == "keychain-pw"
    # The stray file is ignored but never deleted or rewritten.
    on_disk = json.loads(cred_file.read_text())
    assert on_disk["passwords"]["default:eufy"] == "stray-pw"


def test_unmarked_file_without_keyring_stays_file(no_keyring, cred_file):
    """Headless auto-fallback: the file it creates has no marker, and it must
    keep being the active backend on every later run."""
    from eufy_sync.credentials import _active_backend, get_token, store_token

    store_token("eufy", {"access_token": "t"})

    on_disk = json.loads(cred_file.read_text())
    assert "explicit" not in on_disk
    assert _active_backend() == "file"
    assert get_token("eufy") == {"access_token": "t"}


def test_marked_file_with_keyring_stays_file(fake_keyring, cred_file):
    from eufy_sync.credentials import _active_backend, get_password

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({
        "passwords": {"default:eufy": "file-pw"}, "tokens": {}, "explicit": True,
    }))

    assert _active_backend() == "file"
    assert get_password("default:eufy") == "file-pw"


@pytest.mark.parametrize("content", [b"{not valid json::", b"\x80\x81\xfe\xff", b"[1, 2]"])
def test_damaged_file_raises_instead_of_counting_as_unmarked(fake_keyring, cred_file, content):
    """A damaged file may be an explicit file store whose marker can no
    longer be read. Picking the keychain would hide every secret in it, so
    backend selection raises the corruption error instead. (Non-UTF-8 bytes
    make read_text() raise UnicodeDecodeError, a ValueError.)"""
    from eufy_sync.credentials import VaultCorruptError, _active_backend, get_token

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_bytes(content)

    with pytest.raises(VaultCorruptError, match="damaged"):
        _active_backend()
    with pytest.raises(VaultCorruptError):
        get_token("garmin")
    with pytest.raises(VaultCorruptError):
        store_token("garmin", {"a": 1})
    assert cred_file.read_bytes() == content


def test_unreadable_file_raises_instead_of_counting_as_unmarked(fake_keyring, cred_file, monkeypatch):
    from eufy_sync.credentials import _active_backend

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({"explicit": True, "passwords": {}, "tokens": {}}))

    def denied(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(type(cred_file), "read_text", denied)
    with pytest.raises(RuntimeError, match="could not be read"):
        _active_backend()


def test_parsed_unmarked_file_is_still_ignored(fake_keyring, cred_file):
    from eufy_sync.credentials import _active_backend

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({"passwords": {"a": "b"}, "tokens": {}}))
    assert _active_backend() == "keychain"


@pytest.mark.parametrize("section", ["passwords", "tokens"])
@pytest.mark.parametrize("bad", [[], "x", None, 3])
def test_wrong_type_section_in_file_raises_and_is_not_overwritten(no_keyring, cred_file, section, bad):
    from eufy_sync.credentials import VaultCorruptError, store_password

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({"explicit": True, "passwords": {}, "tokens": {}, section: bad})
    cred_file.write_text(original)

    with pytest.raises(VaultCorruptError, match=section):
        store_password("default:eufy", "pw")
    assert cred_file.read_text() == original


@pytest.mark.parametrize("section", ["passwords", "tokens"])
def test_wrong_type_section_in_keychain_raises_and_is_not_overwritten(fake_keyring, section):
    from eufy_sync.credentials import VaultCorruptError, store_password

    original = json.dumps({"passwords": {}, "tokens": {}, section: ["recoverable"]})
    fake_keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, original)

    with pytest.raises(VaultCorruptError, match=section):
        store_password("default:eufy", "pw")
    assert fake_keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT) == original


def test_absent_sections_still_read_as_empty(fake_keyring):
    from eufy_sync.credentials import get_password, store_password

    fake_keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps({"tokens": {"a": {"b": 1}}}))
    assert get_password("default:eufy") is None
    store_password("default:eufy", "pw")
    assert get_password("default:eufy") == "pw"
    assert get_token("a") == {"b": 1}


# --- 11. explicit opt-in: use_file_store merge + marker ----------------------


def test_use_file_store_sets_marker_and_activates_file(fake_keyring, cred_file):
    from eufy_sync.credentials import _active_backend, get_password, store_password, use_file_store

    store_password("default:eufy", "pw1")

    use_file_store()

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert _active_backend() == "file"
    assert get_password("default:eufy") == "pw1"


def test_use_file_store_merges_keychain_and_stray_file(fake_keyring, cred_file):
    """Opting in must not lose secrets from either side: union of both vaults,
    with the currently active store (the keychain here) winning conflicts."""
    from eufy_sync.credentials import store_token, use_file_store

    store_token("garmin", {"di_token": "keychain-A"})
    store_token("shared", {"v": "keychain"})

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({
        "passwords": {},
        "tokens": {"strava": {"access_token": "file-B"}, "shared": {"v": "file"}},
    }))

    use_file_store()

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert on_disk["tokens"]["garmin"] == {"di_token": "keychain-A"}
    assert on_disk["tokens"]["strava"] == {"access_token": "file-B"}
    assert on_disk["tokens"]["shared"] == {"v": "keychain"}


def test_use_file_store_is_idempotent_on_marked_file(fake_keyring, cred_file):
    from eufy_sync.credentials import store_password, use_file_store

    store_password("default:eufy", "pw1")
    use_file_store()
    before = json.loads(cred_file.read_text())

    use_file_store()

    assert json.loads(cred_file.read_text()) == before


def test_use_file_store_aborts_when_keychain_unreadable(fake_keyring, cred_file, monkeypatch):
    """A keychain that exists but cannot be read must abort the opt-in and
    change nothing. Writing the marker over a file that lacks the unread
    keychain secrets would orphan them permanently. Even with existing file
    contents present, the safe move is to stop and let the user retry."""
    from eufy_sync.credentials import _active_backend, use_file_store

    def boom(service, account):
        raise OSError("keychain locked")

    monkeypatch.setattr("keyring.get_password", boom)
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({"passwords": {"default:eufy": "file-pw"}, "tokens": {}}))
    before = cred_file.read_text()

    with pytest.raises(RuntimeError) as exc:
        use_file_store()

    # Actionable message and no state change: the file is byte-for-byte the
    # same (no marker written) and the backend has not flipped to file.
    assert "keychain" in str(exc.value).lower()
    assert cred_file.read_text() == before
    assert _active_backend() == "keychain"


def test_use_file_store_keeps_keychain_vault_when_read_fails(fake_keyring, cred_file, monkeypatch):
    """After a failed keychain read the vault item must be left alone.
    Deleting it needs no read access, so removing it would destroy the only
    copy of every secret that never made it into the file. The abort must
    happen before any write or delete."""
    from eufy_sync.credentials import SERVICE_NAME, VAULT_ACCOUNT, use_file_store

    fake_keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps({
        "passwords": {"default:eufy": "real-pw"}, "tokens": {},
    }))

    def boom(service, account):
        raise OSError("access denied")

    monkeypatch.setattr("keyring.get_password", boom)

    with pytest.raises(RuntimeError):
        use_file_store()

    # Keychain vault item still present; no file written.
    assert (SERVICE_NAME, VAULT_ACCOUNT) in fake_keyring.data
    assert not cred_file.exists()


def test_interrupted_file_save_keeps_previous_vault(fake_keyring, cred_file):
    """The vault file is replaced atomically: a write that dies partway
    through must leave the previous contents (and the opt-in marker) intact
    instead of truncating the file in place."""
    from eufy_sync.credentials import _active_backend, store_token, use_file_store

    use_file_store()
    store_token("garmin", {"di_token": "abc"})
    before = cred_file.read_bytes()

    with pytest.raises(TypeError):
        store_token("bad", {"obj": object()})  # json.dump raises mid-write

    assert cred_file.read_bytes() == before
    # No partial temp file left behind (the temp name carries the writer pid).
    leftovers = list(cred_file.parent.glob(cred_file.name + ".*.tmp"))
    assert leftovers == []
    assert _active_backend() == "file"


def test_marker_survives_store_token_round_trip(fake_keyring, cred_file):
    """_normalize_vault must preserve the marker, or the first write after
    opting in would silently flip the backend to the keychain again."""
    from eufy_sync.credentials import _active_backend, store_token, use_file_store

    use_file_store()
    store_token("garmin", {"di_token": "abc"})

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert on_disk["tokens"]["garmin"] == {"di_token": "abc"}
    assert _active_backend() == "file"


# --- 12. explicit opt-in: use_keychain_store strips the marker ---------------


def test_use_keychain_store_strips_marker_and_unlinks_file(fake_keyring, cred_file):
    import keyring

    from eufy_sync.credentials import (
        SERVICE_NAME,
        get_password,
        store_password,
        use_file_store,
        use_keychain_store,
    )

    store_password("default:eufy", "pw1")
    use_file_store()

    use_keychain_store()

    assert not cred_file.exists()
    stored = json.loads(keyring.get_password(SERVICE_NAME, "vault"))
    assert "explicit" not in stored
    assert stored["passwords"]["default:eufy"] == "pw1"
    assert get_password("default:eufy") == "pw1"


def test_use_keychain_store_stray_file_does_not_overwrite_keychain(fake_keyring, cred_file):
    """A stray unmarked file was never the active store, so moving 'back' to
    the keychain must not let its leftover values clobber real keychain
    secrets. Union is still kept: file-only keys survive the move."""
    import keyring

    from eufy_sync.credentials import SERVICE_NAME, store_password, use_keychain_store

    store_password("default:eufy", "real-pw")  # active backend: keychain
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({
        "passwords": {"default:eufy": "stale-pw", "default:garmin": "file-only"},
        "tokens": {},
    }))

    use_keychain_store()

    stored = json.loads(keyring.get_password(SERVICE_NAME, "vault"))
    assert stored["passwords"]["default:eufy"] == "real-pw"
    assert stored["passwords"]["default:garmin"] == "file-only"
    assert not cred_file.exists()


# --- 13. locked keychain: reads raise, never overwrite -----------------------


def test_keychain_read_failure_raises_actionable_error_and_writes_nothing(fake_keyring, cred_file, monkeypatch):
    """If the keychain cannot be read, returning an empty vault would let the
    next save overwrite the real vault with a near-empty one. The read must
    raise with an actionable message instead, and nothing may be written."""
    from eufy_sync.credentials import get_password, get_token

    def boom(service, account):
        raise OSError("keychain locked")

    monkeypatch.setattr("keyring.get_password", boom)

    with patch("keyring.set_password") as mock_set:
        with pytest.raises(RuntimeError, match="could not be read") as exc:
            get_token("garmin")
        with pytest.raises(RuntimeError, match="--use-file-store"):
            get_password("default:eufy")

    assert exc.value.__cause__ is not None
    mock_set.assert_not_called()
    assert not cred_file.exists()


# --- 14. doctor reflects active store ---------------------------------------


def test_doctor_keychain_line_reflects_active_store(monkeypatch, capsys):
    from eufy_sync.cli import doctor

    monkeypatch.setattr(doctor, "active_store_label", lambda: "system keychain")
    lines: list[str] = []

    def report(status, label, detail, fix=None):
        lines.append((status, label, detail))

    doctor._check_keychain(report)
    assert lines[0][0] == "PASS"
    assert lines[0][2] == "system keychain"


def test_doctor_keychain_line_reflects_file_store(monkeypatch):
    from eufy_sync.cli import doctor

    monkeypatch.setattr(doctor, "active_store_label", lambda: "file (~/.garmin-sync/credentials.json)")
    lines: list[str] = []

    def report(status, label, detail, fix=None):
        lines.append((status, label, detail))

    doctor._check_keychain(report)
    assert lines[0][0] == "PASS"
    assert "file" in lines[0][2]


# --- Vault chunking ----------------------------------------------------------
#
# Windows Credential Manager caps one entry at ~2,560 bytes (UTF-16). A vault
# holding Garmin's two OAuth tokens plus Strava's can exceed that, so the
# keychain backend splits an oversized vault across numbered entries. A vault
# that fits keeps today's single-entry shape, so existing installs never see
# a migration.

def _big_token(size: int) -> dict:
    return {"access_token": "x" * size}


def test_small_vault_keeps_single_entry_shape(fake_keyring):
    store_token("garmin", {"a": 1})
    raw = fake_keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT)
    data = json.loads(raw)
    assert "__chunks__" not in data
    assert data["tokens"]["garmin"] == {"a": 1}
    assert fake_keyring.get_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:1") is None


def _vault_accounts(store: _FakeKeyringStore) -> set[str]:
    """Every vault-related entry currently stored (header and chunks)."""
    return {
        account for account in store.accounts_written()
        if account == VAULT_ACCOUNT or account.startswith(f"{VAULT_ACCOUNT}:")
    }


def _header(store: _FakeKeyringStore) -> dict:
    return json.loads(store.get_password(SERVICE_NAME, VAULT_ACCOUNT))["__vault__"]


def _chunk_accounts(tag: str, count: int) -> set[str]:
    return {f"{VAULT_ACCOUNT}:{tag}:{i}" for i in range(1, count + 1)}


def test_oversized_vault_chunks_and_round_trips(fake_keyring):
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    header = _header(fake_keyring)
    n = header["chunks"]
    assert n >= 3
    for account in _chunk_accounts(header["tag"], n):
        chunk = fake_keyring.get_password(SERVICE_NAME, account)
        assert chunk is not None
        assert len(chunk) <= CHUNK_LIMIT
        # Windows caps one entry at 2,560 bytes of UTF-16.
        assert len(chunk.encode("utf-16-le")) <= 2560
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)
    assert _vault_accounts(fake_keyring) == {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], n)


def test_non_ascii_vault_respects_the_windows_entry_cap(fake_keyring):
    """json.dumps escapes non-ASCII, so a vault full of multi-byte characters
    still splits into entries under the per-entry byte cap."""
    store_token("garmin", {"access_token": "é\U0001f600" * CHUNK_LIMIT})
    header = _header(fake_keyring)
    for account in _chunk_accounts(header["tag"], header["chunks"]):
        assert len(fake_keyring.get_password(SERVICE_NAME, account).encode("utf-16-le")) <= 2560
    assert get_token("garmin") == {"access_token": "é\U0001f600" * CHUNK_LIMIT}


def test_each_save_uses_a_new_generation_and_deletes_the_old_one(fake_keyring):
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    first = _header(fake_keyring)
    store_token("garmin", _big_token(4 * CHUNK_LIMIT))
    second = _header(fake_keyring)

    assert second["gen"] == first["gen"] + 1
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(second["tag"], second["chunks"])
    )
    assert get_token("garmin") == _big_token(4 * CHUNK_LIMIT)


def test_shrinking_vault_deletes_stale_chunks(fake_keyring):
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    store_token("garmin", {"a": 1})  # replaces the big token; vault fits again
    assert get_token("garmin") == {"a": 1}
    raw = json.loads(fake_keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT))
    assert "__vault__" not in raw
    assert raw["tokens"]["garmin"] == {"a": 1}
    assert _vault_accounts(fake_keyring) == {VAULT_ACCOUNT}


def test_missing_chunk_raises_and_is_never_overwritten(fake_keyring, cred_file):
    """A header whose chunk is gone is a damaged vault. Reading it as empty
    would let the next store_token save a vault holding only that token over
    every stored password."""
    from eufy_sync.credentials import VaultCorruptError, store_password

    store_password("default:eufy", "pw")
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    tag = _header(fake_keyring)["tag"]
    fake_keyring.delete_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{tag}:2")
    before = dict(fake_keyring.data)

    with pytest.raises(VaultCorruptError, match="chunk 2 of"):
        get_token("garmin")
    with pytest.raises(VaultCorruptError):
        store_token("strava", {"access_token": "t"})

    assert fake_keyring.data == before


def test_chunks_that_do_not_match_the_header_raise(fake_keyring):
    from eufy_sync.credentials import VaultCorruptError

    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    tag = _header(fake_keyring)["tag"]
    account = f"{VAULT_ACCOUNT}:{tag}:1"
    chunk = fake_keyring.get_password(SERVICE_NAME, account)
    fake_keyring.set_password(SERVICE_NAME, account, chunk.replace("x", "y"))

    with pytest.raises(VaultCorruptError, match="do not match"):
        get_token("garmin")


@pytest.mark.parametrize("header", [
    {"__chunks__": 0},
    {"__chunks__": True},
    {"__chunks__": 10_000},
    {"__vault__": {"gen": 1, "chunks": 2}},
    {"__vault__": "nope"},
])
def test_malformed_chunk_header_raises(fake_keyring, header):
    from eufy_sync.credentials import VaultCorruptError

    fake_keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps(header))
    with pytest.raises(VaultCorruptError):
        get_token("garmin")


def test_absent_vault_reads_as_empty(fake_keyring):
    assert get_token("garmin") is None
    assert _vault_accounts(fake_keyring) == set()


# --- Crash safety ------------------------------------------------------------
#
# A save is a sequence of keyring writes and deletes. Killing the process
# between any two of them must leave a vault that reads back as either the old
# contents or the new ones, and the next completed save must clear whatever
# the killed one left behind.


class _Killed(BaseException):
    """Simulates the process dying: BaseException, so no `except Exception`
    cleanup path inside the module can swallow it."""


def _kill_after(monkeypatch, store: _FakeKeyringStore, steps: int) -> None:
    """Let `steps` backend mutations through, then kill on the next one."""
    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        if calls["n"] > steps:
            raise _Killed()

    def set_password(service, account, password):
        tick()
        store.set_password(service, account, password)

    def delete_password(service, account):
        tick()
        store.delete_password(service, account)

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)


def _restore(monkeypatch, store: _FakeKeyringStore) -> None:
    monkeypatch.setattr("keyring.set_password", store.set_password)
    monkeypatch.setattr("keyring.delete_password", store.delete_password)


def _count_mutations(monkeypatch, store, action) -> int:
    snapshot = dict(store.data)
    counted = {"n": 0}

    def set_password(service, account, password):
        counted["n"] += 1
        store.set_password(service, account, password)

    def delete_password(service, account):
        counted["n"] += 1
        store.delete_password(service, account)

    monkeypatch.setattr("keyring.set_password", set_password)
    monkeypatch.setattr("keyring.delete_password", delete_password)
    action()
    _restore(monkeypatch, store)
    store.data = snapshot
    return counted["n"]


_OLD_PW = {"default:eufy": "pw1", "default:garmin": "pw2"}

# (description, token before, token after): big -> big, small -> big,
# big -> small, and big -> big starting from a vault already on a generation.
_TRANSITIONS = [
    ("big_to_big", _big_token(3 * CHUNK_LIMIT), _big_token(5 * CHUNK_LIMIT)),
    ("big_to_smaller_big", _big_token(5 * CHUNK_LIMIT), _big_token(2 * CHUNK_LIMIT)),
    ("small_to_big", {"a": 1}, _big_token(3 * CHUNK_LIMIT)),
    ("big_to_small", _big_token(3 * CHUNK_LIMIT), {"a": 1}),
]


@pytest.mark.parametrize("name,old,new", _TRANSITIONS, ids=[t[0] for t in _TRANSITIONS])
@pytest.mark.parametrize("presaves", [1, 2, "legacy"])
def test_kill_at_every_step_of_a_save_leaves_a_readable_vault(
    fake_keyring, cred_file, monkeypatch, name, old, new, presaves
):
    """presaves "legacy" starts from the released "vault:i" chunk layout, so
    the killed save is the one migrating it."""
    from eufy_sync.credentials import _load_vault, store_password

    def setup():
        fake_keyring.data.clear()
        if presaves == "legacy":
            _write_legacy_chunked(
                fake_keyring, {"passwords": dict(_OLD_PW), "tokens": {"garmin": old}}
            )
            return
        for account, pw in _OLD_PW.items():
            store_password(account, pw)
        for _ in range(presaves):
            store_token("garmin", old)

    setup()
    total = _count_mutations(monkeypatch, fake_keyring, lambda: store_token("garmin", new))
    assert total >= 2

    for steps in range(total):
        setup()
        _kill_after(monkeypatch, fake_keyring, steps)
        with pytest.raises(_Killed):
            store_token("garmin", new)
        _restore(monkeypatch, fake_keyring)

        vault = _load_vault()
        assert vault["passwords"] == _OLD_PW, f"killed after {steps} of {total}"
        assert vault["tokens"]["garmin"] in (old, new), f"killed after {steps} of {total}"

        # The next completed save leaves only the entries its header uses.
        # Saves are serialized by the vault lock, so the killed save's
        # chunks cannot belong to a save still running and go at once.
        store_token("strava", {"access_token": "t"})
        vault = _load_vault()
        assert vault["passwords"] == _OLD_PW
        assert vault["tokens"]["strava"] == {"access_token": "t"}
        raw = json.loads(fake_keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT))
        expected = {VAULT_ACCOUNT}
        if "__vault__" in raw:
            expected |= _chunk_accounts(raw["__vault__"]["tag"], raw["__vault__"]["chunks"])
        assert _vault_accounts(fake_keyring) == expected, f"killed after {steps} of {total}"


def test_reader_retries_once_when_a_save_switches_the_header_mid_read(fake_keyring, monkeypatch):
    """A save in another process can switch the header and delete the old
    chunks while this process is reading them. The reader sees a missing
    chunk, notices the header moved on, and reads the new vault."""
    from eufy_sync import credentials

    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    old_tag = _header(fake_keyring)["tag"]
    real_get = fake_keyring.get_password
    state = {"saved": False}

    def racing_get(service, account):
        if account == f"{VAULT_ACCOUNT}:{old_tag}:2" and not state["saved"]:
            state["saved"] = True
            monkeypatch.setattr("keyring.get_password", real_get)
            credentials._save_vault_to_keychain(
                {"passwords": {}, "tokens": {"garmin": _big_token(4 * CHUNK_LIMIT)}}
            )
            monkeypatch.setattr("keyring.get_password", racing_get)
        return real_get(service, account)

    monkeypatch.setattr("keyring.get_password", racing_get)
    assert get_token("garmin") == _big_token(4 * CHUNK_LIMIT)


# --- Upgrade from the released chunk layout ----------------------------------


def _write_legacy_chunked(store: _FakeKeyringStore, vault: dict) -> int:
    """Store `vault` exactly as released versions did: "vault:1".."vault:N"
    under a {"__chunks__": N} header."""
    payload = json.dumps(vault)
    chunks = [payload[i:i + CHUNK_LIMIT] for i in range(0, len(payload), CHUNK_LIMIT)]
    for i, chunk in enumerate(chunks, start=1):
        store.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{i}", chunk)
    store.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps({"__chunks__": len(chunks)}))
    return len(chunks)


def test_legacy_chunked_vault_reads_and_migrates_on_next_save(fake_keyring, cred_file):
    from eufy_sync.credentials import get_password

    legacy = {
        "passwords": {"default:eufy": "pw"},
        "tokens": {"garmin": _big_token(3 * CHUNK_LIMIT)},
    }
    n = _write_legacy_chunked(fake_keyring, legacy)
    assert n >= 3

    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)
    assert get_password("default:eufy") == "pw"

    store_token("strava", {"access_token": "t"})

    header = _header(fake_keyring)
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], header["chunks"])
    )
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)
    assert get_token("strava") == {"access_token": "t"}
    assert get_password("default:eufy") == "pw"


def test_legacy_chunked_vault_with_missing_chunk_raises(fake_keyring):
    from eufy_sync.credentials import VaultCorruptError

    _write_legacy_chunked(fake_keyring, {"passwords": {}, "tokens": {"garmin": _big_token(3 * CHUNK_LIMIT)}})
    fake_keyring.delete_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:2")
    with pytest.raises(VaultCorruptError):
        get_token("garmin")


def test_legacy_single_entry_vault_reads_and_stays_single(fake_keyring, cred_file):
    from eufy_sync.credentials import get_password

    fake_keyring.set_password(
        SERVICE_NAME, VAULT_ACCOUNT,
        json.dumps({"passwords": {"default:eufy": "pw"}, "tokens": {"garmin": {"a": 1}}}),
    )
    assert get_password("default:eufy") == "pw"
    store_token("strava", {"b": 2})
    assert get_token("garmin") == {"a": 1}
    assert get_token("strava") == {"b": 2}
    assert _vault_accounts(fake_keyring) == {VAULT_ACCOUNT}


def test_prerelease_generation_leftovers_are_cleaned_up(fake_keyring):
    """Pre-release builds named chunks by generation alone ("vault:<gen>:<i>")
    and left crash leftovers at the neighbouring generations. A vault still
    on that layout reads, and the first save deletes its chunks and both
    neighbours."""
    vault = {"passwords": {"default:eufy": "pw"}, "tokens": {"garmin": _big_token(3 * CHUNK_LIMIT)}}
    payload = json.dumps(vault)
    chunks = [payload[i:i + CHUNK_LIMIT] for i in range(0, len(payload), CHUNK_LIMIT)]
    for i, chunk in enumerate(chunks, start=1):
        fake_keyring.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:5:{i}", chunk)
    fake_keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps({"__vault__": {
        "gen": 5, "chunks": len(chunks),
        "sha256": hashlib.sha256(payload.encode()).hexdigest(),
    }}))
    for stale in (4, 6):
        for i in (1, 2, 3):
            fake_keyring.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{stale}:{i}", "junk")

    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)
    store_token("garmin", _big_token(2 * CHUNK_LIMIT))

    header = _header(fake_keyring)
    assert header["gen"] == 6 and header["tag"].startswith("6.")
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], header["chunks"])
    )
    assert get_token("garmin") == _big_token(2 * CHUNK_LIMIT)


def _kill_on_header_write(monkeypatch, store: _FakeKeyringStore) -> None:
    """Let a save write its chunks, then kill it as it switches the header."""
    def set_password(service, account, password):
        if account == VAULT_ACCOUNT:
            raise _Killed()
        store.set_password(service, account, password)

    monkeypatch.setattr("keyring.set_password", set_password)


def test_consecutive_interrupted_saves_leave_no_orphaned_chunks(fake_keyring, monkeypatch):
    """Each killed save leaves a full set of chunks (plaintext slices of the
    vault) under a tag no header names. However many pile up, the next
    completed save deletes all of them, along with released-version
    "vault:i" chunks."""
    credentials.store_password("default:eufy", "pw")
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    for i in (1, 2):
        fake_keyring.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{i}", "legacy-junk")

    for n in range(4):
        _kill_on_header_write(monkeypatch, fake_keyring)
        with pytest.raises(_Killed):
            store_token("garmin", _big_token((3 + n) * CHUNK_LIMIT))
        _restore(monkeypatch, fake_keyring)
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)
    orphans = _vault_accounts(fake_keyring) - {VAULT_ACCOUNT, credentials.JOURNAL_ACCOUNT}
    assert len(orphans) > 4 * 3

    store_token("strava", {"access_token": "t"})

    header = _header(fake_keyring)
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], header["chunks"])
    )
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)


def test_crash_leftovers_are_deleted_by_the_very_next_save(fake_keyring, monkeypatch):
    """Under the vault lock no other save can be running, so a journal tag
    the header does not name is a crash leftover with nothing to wait for."""
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    _kill_on_header_write(monkeypatch, fake_keyring)
    with pytest.raises(_Killed):
        store_token("garmin", _big_token(4 * CHUNK_LIMIT))
    _restore(monkeypatch, fake_keyring)
    pending = set(json.loads(fake_keyring.get_password(SERVICE_NAME, credentials.JOURNAL_ACCOUNT)))
    live = _header(fake_keyring)["tag"]
    other = (pending - {live}).pop()
    assert fake_keyring.get_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{other}:1") is not None

    store_token("strava", {"access_token": "t"})

    assert fake_keyring.get_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:{other}:1") is None
    assert fake_keyring.get_password(SERVICE_NAME, credentials.JOURNAL_ACCOUNT) is None


def test_prerelease_timestamped_journal_is_still_swept(fake_keyring):
    """Pre-release builds stored the journal as {tag: timestamp}."""
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    for i in (1, 2):
        fake_keyring.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:9.0000abcd:{i}", "junk")
    fake_keyring.set_password(
        SERVICE_NAME, credentials.JOURNAL_ACCOUNT, json.dumps({"9.0000abcd": time.time()})
    )

    store_token("strava", {"access_token": "t"})

    header = _header(fake_keyring)
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], header["chunks"])
    )


def test_switching_to_the_file_store_deletes_every_known_chunk(fake_keyring, cred_file, monkeypatch):
    """--use-file-store must not leave plaintext slices of the vault in the
    keychain the user just opted out of: the live chunks, journal-tracked
    leftovers of any age, and released-version "vault:i" chunks all go."""
    from eufy_sync.credentials import _load_vault, use_file_store

    credentials.store_password("default:eufy", "pw")
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    fake_keyring.set_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:1", "legacy-junk")
    for n in range(2):
        _kill_on_header_write(monkeypatch, fake_keyring)
        with pytest.raises(_Killed):
            store_token("garmin", _big_token((4 + n) * CHUNK_LIMIT))
        _restore(monkeypatch, fake_keyring)

    use_file_store()

    assert _vault_accounts(fake_keyring) == set()
    assert _load_vault()["tokens"]["garmin"] == _big_token(3 * CHUNK_LIMIT)


def test_a_save_inside_another_save_waits_for_the_lock(fake_keyring, monkeypatch):
    """The review's race, replayed: writer B has loaded the vault, and writer
    A tries to save completely before B's header write lands. With the vault
    lock A cannot start until B finishes, so A works on B's result and both
    updates survive. A runs in a thread, standing in for another process."""
    import threading

    from eufy_sync.credentials import _load_vault_from_keychain, store_password

    store_token("garmin", {"blob": "o" * 3000})
    store_token("garmin", {"blob": "o" * 3000})
    real_set = fake_keyring.set_password
    started = threading.Event()
    other = {}

    def writer_a():
        started.set()
        store_token("garmin", {"blob": "a" * 2000})

    def racing_set(service, account, password):
        if account == VAULT_ACCOUNT and "thread" not in other:
            other["thread"] = threading.Thread(target=writer_a)
            other["thread"].start()
            started.wait()
            # Give A every chance to run if it were not blocked.
            other["thread"].join(timeout=0.3)
            assert other["thread"].is_alive(), "writer A ran while B held the vault lock"
        real_set(service, account, password)

    monkeypatch.setattr("keyring.set_password", racing_set)
    store_password("u:garmin", "x")
    other["thread"].join(timeout=10)
    assert not other["thread"].is_alive()

    after = _load_vault_from_keychain()
    assert after["passwords"]["u:garmin"] == "x"
    assert after["tokens"]["garmin"] == {"blob": "a" * 2000}
    header = _header(fake_keyring)
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], header["chunks"])
    )


def test_concurrent_threads_storing_different_tokens_all_persist(fake_keyring):
    import threading

    names = [f"service{i}" for i in range(8)]
    errors = []

    def store(name):
        try:
            for round_ in range(5):
                store_token(name, {"blob": name * 400, "round": round_})
        except Exception as e:  # pragma: no cover - reported below
            errors.append(e)

    threads = [threading.Thread(target=store, args=(name,)) for name in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    for name in names:
        assert get_token(name) == {"blob": name * 400, "round": 4}
    header = _header(fake_keyring)
    assert _vault_accounts(fake_keyring) == (
        {VAULT_ACCOUNT} | _chunk_accounts(header["tag"], header["chunks"])
    )


def test_shared_service_name_and_legacy_token_item_are_untouched(fake_keyring, cred_file):
    """Another tool reads the legacy "token:garmin" item under the same
    service name. Vault saves, chunked or not, must never touch it."""
    from eufy_sync.credentials import SERVICE_NAME as service

    assert service == "eufy-garmin-sync"
    fake_keyring.set_password(SERVICE_NAME, "token:garmin", json.dumps({"x": 1}))
    store_token("strava", _big_token(3 * CHUNK_LIMIT))
    store_token("strava", {"a": 1})
    assert fake_keyring.get_password(SERVICE_NAME, "token:garmin") == json.dumps({"x": 1})


def test_chunk_read_failure_raises_friendly_error(fake_keyring, cred_file, monkeypatch):
    """A keyring exception while reassembling chunks (locked or access denied
    partway through) is the same unreadable-keychain condition as a failed
    initial read - it must surface the actionable RuntimeError, not a raw
    backend exception, so a partial read can never be saved back over the real
    vault. A genuinely missing chunk keeps its malformed-vault handling
    (covered above)."""
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))

    real_get = fake_keyring.get_password

    def flaky(service, account):
        # The header read (account "vault") succeeds; the first chunk read fails.
        if account.startswith(f"{VAULT_ACCOUNT}:"):
            raise OSError("keychain locked")
        return real_get(service, account)

    monkeypatch.setattr("keyring.get_password", flaky)

    with pytest.raises(RuntimeError, match="could not be read"):
        get_token("garmin")


def test_use_file_store_deletes_chunk_entries_not_just_the_header(fake_keyring, cred_file):
    """Leaving the keychain must take the numbered chunk entries with it.
    Deleting only the header makes the leftovers invisible to every reader
    while each one still holds a slice of the plaintext vault in the keychain
    the user just opted out of."""
    from eufy_sync.credentials import _active_backend, get_token, use_file_store

    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    store_token("garmin", _big_token(3 * CHUNK_LIMIT))  # move past generation 1
    assert _header(fake_keyring)["chunks"] >= 3

    use_file_store()

    assert _vault_accounts(fake_keyring) == set()

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert on_disk["tokens"]["garmin"] == _big_token(3 * CHUNK_LIMIT)
    assert _active_backend() == "file"
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)


def test_use_file_store_survives_a_failing_chunk_delete(fake_keyring, cred_file, monkeypatch):
    """The file already holds the merged vault by the time the chunks are
    cleaned up, so a keychain that refuses the delete must not fail the
    switch and leave the user on neither store."""
    from eufy_sync.credentials import _active_backend, get_token, use_file_store

    store_token("garmin", _big_token(3 * CHUNK_LIMIT))

    def boom(service, account):
        raise OSError("keychain locked")

    monkeypatch.setattr("keyring.delete_password", boom)

    use_file_store()  # must not raise

    assert _active_backend() == "file"
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)


def test_vault_exactly_at_chunk_limit_stays_single_entry(fake_keyring):
    """The chunking boundary is inclusive: a serialized vault whose length is
    exactly CHUNK_LIMIT still fits in one entry (the split only triggers above
    the limit). Compute the token size that lands the full payload exactly on
    CHUNK_LIMIT and confirm no chunk entries are written."""
    def payload_len(n: int) -> int:
        vault = {"passwords": {}, "tokens": {"garmin": {"access_token": "x" * n}}}
        return len(json.dumps(vault))

    # Each extra character in the token string adds exactly one byte to the
    # serialized JSON, so solve for the size directly.
    n = 1 + (CHUNK_LIMIT - payload_len(1))
    assert payload_len(n) == CHUNK_LIMIT

    store_token("garmin", {"access_token": "x" * n})

    raw = fake_keyring.get_password(SERVICE_NAME, VAULT_ACCOUNT)
    assert len(raw) == CHUNK_LIMIT
    data = json.loads(raw)
    assert "__chunks__" not in data
    assert fake_keyring.get_password(SERVICE_NAME, f"{VAULT_ACCOUNT}:1") is None
    assert get_token("garmin") == {"access_token": "x" * n}


def test_doctor_fails_the_keychain_line_for_a_damaged_credentials_file(fake_keyring, cred_file):
    from eufy_sync.cli import doctor

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("{not json")
    lines: list[tuple] = []
    doctor._check_keychain(lambda status, label, detail, fix=None: lines.append((status, detail)))
    assert lines[0][0] == "FAIL"
    assert "damaged" in lines[0][1]


# --- Vault lock ----------------------------------------------------------------

_FAKE_KEYRING_PRELUDE = """
import sys, types
# No real keychain in the child: a stub module that is never used.
sys.modules["keyring"] = types.ModuleType("keyring")
from pathlib import Path
from eufy_sync import credentials
credentials.CRED_FILE = Path(sys.argv[1])
credentials._keyring_available = lambda: False
"""


def _run_child(script: str, *args: str, timeout: float = 60):
    import subprocess
    import sys
    return subprocess.Popen(
        [sys.executable, "-c", _FAKE_KEYRING_PRELUDE + script, *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_concurrent_processes_storing_different_tokens_all_persist(no_keyring, cred_file):
    """Separate processes share only the OS lock. Without it, two
    read-modify-writes of the same file interleave and one token is lost."""
    script = """
name = sys.argv[2]
for round_ in range(15):
    credentials.store_token(name, {"blob": name * 200, "round": round_})
"""
    names = [f"proc{i}" for i in range(4)]
    children = [_run_child(script, str(cred_file), name) for name in names]
    for child in children:
        _, err = child.communicate(timeout=60)
        assert child.returncode == 0, err
    for name in names:
        assert get_token(name) == {"blob": name * 200, "round": 14}


def test_another_process_cannot_write_while_the_lock_is_held(no_keyring, cred_file):
    from eufy_sync.credentials import vault_lock

    script = """
credentials.VAULT_LOCK_TIMEOUT = 0.3
try:
    credentials.store_token("other", {"a": 1})
except credentials.VaultLockError as e:
    print("refused:", e)
"""
    with vault_lock():
        child = _run_child(script, str(cred_file))
        out, err = child.communicate(timeout=60)
    assert child.returncode == 0, err
    assert "refused:" in out and "Retry" in out
    assert not cred_file.exists()


def test_vault_lock_is_reentrant_and_released(no_keyring, cred_file):
    from eufy_sync import file_lock
    from eufy_sync.credentials import store_password, vault_lock, vault_lock_path

    with vault_lock():
        with vault_lock():
            store_password("default:eufy", "pw")
        # Still held by this process after the inner block.
        fd = file_lock.acquire(vault_lock_path())
        assert fd is None
    fd = file_lock.acquire(vault_lock_path())
    assert fd is not None
    file_lock.release(fd)
    assert vault_lock_path().exists()


def test_store_refuses_when_the_lock_file_cannot_open(fake_keyring, cred_file):
    from eufy_sync.credentials import VaultLockError, store_password

    with patch("eufy_sync.file_lock.os.open", side_effect=OSError(30, "Read-only file system")):
        with pytest.raises(VaultLockError, match="could not be created"):
            store_password("default:eufy", "pw")
    assert _vault_accounts(fake_keyring) == set()


def test_migration_runs_under_the_lock(fake_keyring, cred_file):
    from eufy_sync import credentials as creds

    fake_keyring.set_password(SERVICE_NAME, "default:eufy", "legacy-pw")
    seen = []
    real_save = creds._save_vault_to_keychain.__wrapped__

    def spy(vault):
        seen.append(creds._lock_depth)
        real_save(vault)

    with patch.object(creds, "_save_vault_to_keychain", spy):
        assert creds.get_password("default:eufy") == "legacy-pw"
    assert seen and all(depth >= 1 for depth in seen)
    assert fake_keyring.get_password(SERVICE_NAME, "default:eufy") is None


def _before_first_lock(monkeypatch, action):
    """Run action once, just before the first vault_lock() entry: the window
    after an unlocked read where another process can finish a write."""
    import contextlib

    from eufy_sync import credentials as creds

    real = creds.vault_lock
    pending = [action]

    @contextlib.contextmanager
    def hooked():
        if pending:
            pending.pop()()
        with real():
            yield

    monkeypatch.setattr(creds, "vault_lock", hooked)


def test_password_migration_does_not_undo_a_concurrent_delete(fake_keyring, cred_file, monkeypatch):
    """get_password reads the legacy item before locking. If a delete_password
    finishes in that window, the migration must not save the cached value."""
    from eufy_sync.credentials import get_password

    fake_keyring.set_password(SERVICE_NAME, "default:eufy", "legacy-pw")
    _before_first_lock(monkeypatch, lambda: fake_keyring.delete_password(SERVICE_NAME, "default:eufy"))

    assert get_password("default:eufy") is None
    assert credentials._load_vault()["passwords"] == {}
    assert fake_keyring.get_password(SERVICE_NAME, "default:eufy") is None


def test_token_migration_does_not_undo_a_concurrent_delete(fake_keyring, cred_file, monkeypatch):
    fake_keyring.set_password(SERVICE_NAME, "token:strava", json.dumps({"t": 1}))
    _before_first_lock(monkeypatch, lambda: fake_keyring.delete_password(SERVICE_NAME, "token:strava"))

    assert get_token("strava") is None
    assert credentials._load_vault()["tokens"] == {}
    assert fake_keyring.get_password(SERVICE_NAME, "token:strava") is None


def test_migration_saves_the_legacy_value_read_under_the_lock(fake_keyring, cred_file, monkeypatch):
    """A legacy item rewritten in the window is migrated with its new value."""
    from eufy_sync.credentials import get_password

    fake_keyring.set_password(SERVICE_NAME, "default:eufy", "old")
    _before_first_lock(monkeypatch, lambda: fake_keyring.set_password(SERVICE_NAME, "default:eufy", "new"))

    assert get_password("default:eufy") == "new"
    assert credentials._load_vault()["passwords"] == {"default:eufy": "new"}
    assert fake_keyring.get_password(SERVICE_NAME, "default:eufy") is None


def _switch_during_first_read(monkeypatch, backend, switch):
    """Run switch (a store change "in another process") after the read has
    chosen backend but before it loads it."""
    from eufy_sync import credentials as creds

    real = creds._load_from
    pending = [switch]

    def hooked(chosen):
        if pending and chosen == backend:
            pending.pop()()
        return real(chosen)

    monkeypatch.setattr(creds, "_load_from", hooked)


def test_read_that_chose_the_file_survives_a_switch_to_the_keychain(fake_keyring, cred_file, monkeypatch):
    """use_keychain_store unlinks the file the read picked; the credentials
    are in the keychain by then and must not read as missing."""
    from eufy_sync.credentials import get_password, use_keychain_store

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({"explicit": True, "passwords": {"default:eufy": "pw"}, "tokens": {}}))
    _switch_during_first_read(monkeypatch, "file", use_keychain_store)

    assert get_password("default:eufy", migrate=False) == "pw"
    assert not cred_file.exists()


def test_read_that_chose_the_keychain_survives_a_switch_to_the_file(fake_keyring, cred_file, monkeypatch):
    from eufy_sync.credentials import store_token, use_file_store

    store_token("garmin", {"t": 1})
    _switch_during_first_read(monkeypatch, "keychain", use_file_store)

    assert get_token("garmin") == {"t": 1}
    assert _vault_accounts(fake_keyring) == set()


def test_empty_vault_with_no_switch_reads_the_backend_once(fake_keyring, cred_file, monkeypatch):
    from eufy_sync import credentials as creds

    calls = []
    real = creds._load_from
    monkeypatch.setattr(creds, "_load_from", lambda b: calls.append(b) or real(b))
    assert creds._load_vault() == {"passwords": {}, "tokens": {}}
    assert calls == ["keychain"]


# --- Chunk cap -----------------------------------------------------------------


def test_oversized_vault_is_refused_before_any_write(fake_keyring):
    from eufy_sync.credentials import MAX_CHUNKS, VaultTooLargeError

    store_token("garmin", _big_token(3 * CHUNK_LIMIT))
    before = dict(fake_keyring.data)
    with pytest.raises(VaultTooLargeError, match="--use-file-store"):
        store_token("huge", _big_token(MAX_CHUNKS * CHUNK_LIMIT))
    assert fake_keyring.data == before
    assert get_token("garmin") == _big_token(3 * CHUNK_LIMIT)


def test_largest_allowed_vault_round_trips(fake_keyring):
    from eufy_sync.credentials import MAX_CHUNKS

    overhead = len(json.dumps({"passwords": {}, "tokens": {"garmin": _big_token(0)}}))
    token = _big_token(MAX_CHUNKS * CHUNK_LIMIT - overhead)
    store_token("garmin", token)
    assert _header(fake_keyring)["chunks"] == MAX_CHUNKS
    assert get_token("garmin") == token


def test_use_keychain_store_keeps_the_file_when_the_vault_is_too_large(fake_keyring, cred_file):
    from eufy_sync.credentials import MAX_CHUNKS, VaultTooLargeError, use_keychain_store

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    original = json.dumps({
        "explicit": True, "passwords": {"default:eufy": "pw"},
        "tokens": {"huge": _big_token(MAX_CHUNKS * CHUNK_LIMIT)},
    })
    cred_file.write_text(original)

    with pytest.raises(VaultTooLargeError):
        use_keychain_store()
    assert cred_file.read_text() == original
    assert _vault_accounts(fake_keyring) == set()


def test_legacy_vault_with_more_chunks_than_the_cap_still_reads_and_migrates(fake_keyring, cred_file):
    """Released versions wrote any number of "vault:i" chunks."""
    from eufy_sync.credentials import MAX_CHUNKS

    legacy = {"passwords": {"default:eufy": "pw"}, "tokens": {"garmin": _big_token((MAX_CHUNKS + 5) * CHUNK_LIMIT)}}
    n = _write_legacy_chunked(fake_keyring, legacy)
    assert n > MAX_CHUNKS

    assert get_token("garmin") == legacy["tokens"]["garmin"]
    # Too big for the new layout, so the save is refused and the legacy
    # vault is left as it was rather than half-migrated.
    with pytest.raises(credentials.VaultTooLargeError):
        store_token("strava", {"a": 1})
    assert get_token("garmin") == legacy["tokens"]["garmin"]
    # Shrinking it migrates, and every legacy chunk is deleted.
    store_token("garmin", {"a": 1})
    assert _vault_accounts(fake_keyring) == {VAULT_ACCOUNT}
