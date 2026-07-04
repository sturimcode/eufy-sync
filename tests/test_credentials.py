from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

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
            raise keyring.errors.PasswordDeleteError("not found")

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
    from eufy_sync.credentials import store_password, get_password, delete_password

    store_password("default:eufy", "hunter2")
    assert get_password("default:eufy") == "hunter2"

    delete_password("default:eufy")
    assert get_password("default:eufy") is None


def test_keychain_backend_round_trip_token(fake_keyring, cred_file):
    from eufy_sync.credentials import store_token, get_token, delete_token

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
    from eufy_sync.credentials import store_password, store_token, get_password, get_token

    store_password("default:eufy", "pw1")
    store_token("garmin", {"di_token": "b"})
    store_password("default:garmin", "pw2")   # later writes must not drop the earlier keys

    assert get_password("default:eufy") == "pw1"
    assert get_password("default:garmin") == "pw2"
    assert get_token("garmin") == {"di_token": "b"}


# --- 2. file backend round-trip; 0o600; keyring never touched ---------------


def test_file_backend_round_trip(no_keyring, cred_file):
    from eufy_sync.credentials import store_password, get_password, delete_password, store_token, get_token

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
    mode = stat.S_IMODE(cred_file.stat().st_mode)
    assert mode == 0o600

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["passwords"]["default:eufy"] == "hunter2"


def test_file_backend_never_touches_keyring(no_keyring, cred_file):
    """When the file backend is active, keyring.set_password/get_password
    must never be called - prevents a hybrid state where secrets leak into
    both places."""
    from eufy_sync.credentials import store_password, get_password, store_token, get_token

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
    from eufy_sync.credentials import (
        store_password, store_token, use_file_store, _active_backend,
        get_password, get_token,
    )
    import keyring
    from eufy_sync.credentials import SERVICE_NAME

    store_password("default:eufy", "pw1")
    store_token("garmin", {"di_token": "abc"})

    use_file_store()

    assert cred_file.exists()
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
        store_password, store_token, use_file_store, use_keychain_store,
        _active_backend, get_password, get_token,
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
    from eufy_sync.credentials import store_token, get_token, _active_backend

    assert not cred_file.exists()
    assert _active_backend() == "file"

    store_token("eufy", {"access_token": "headless-tok"})

    assert cred_file.exists()
    mode = stat.S_IMODE(cred_file.stat().st_mode)
    assert mode == 0o600
    assert get_token("eufy") == {"access_token": "headless-tok"}


# --- 7. malformed vault JSON --------------------------------------------------


def test_malformed_file_vault_json_returns_none_not_crash(no_keyring, cred_file):
    from eufy_sync.credentials import get_password, get_token

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("{not valid json::")

    assert get_password("default:eufy") is None
    assert get_token("eufy") is None


def test_malformed_keychain_vault_json_returns_none_not_crash(fake_keyring, cred_file):
    import keyring
    from eufy_sync.credentials import SERVICE_NAME, get_password, get_token

    keyring.set_password(SERVICE_NAME, "vault", "{not valid json::")

    assert get_password("default:eufy") is None
    assert get_token("eufy") is None


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


# --- 10. explicit opt-in: backend selection ----------------------------------
#
# A credentials file only overrides a working keychain when it carries the
# "explicit" marker that use_file_store() writes. An unmarked file is either
# the headless auto-fallback (no keychain: keep using it) or a stray leftover
# (keychain works: ignore it, never delete it).


def test_unmarked_file_with_keyring_is_ignored(fake_keyring, cred_file):
    from eufy_sync.credentials import _active_backend, store_password, get_password

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
    from eufy_sync.credentials import _active_backend, store_token, get_token

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


def test_malformed_file_counts_as_unmarked(fake_keyring, cred_file):
    from eufy_sync.credentials import _active_backend

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text("{not valid json::")

    assert _active_backend() == "keychain"


def test_non_utf8_file_counts_as_unmarked(fake_keyring, cred_file):
    """read_text() raises UnicodeDecodeError (a ValueError) on non-UTF-8
    bytes; that must count as no marker, not crash every backend lookup."""
    from eufy_sync.credentials import _active_backend

    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_bytes(b"\x80\x81\xfe\xff")

    assert _active_backend() == "keychain"


# --- 11. explicit opt-in: use_file_store merge + marker ----------------------


def test_use_file_store_sets_marker_and_activates_file(fake_keyring, cred_file):
    from eufy_sync.credentials import use_file_store, _active_backend, store_password, get_password

    store_password("default:eufy", "pw1")

    use_file_store()

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert _active_backend() == "file"
    assert get_password("default:eufy") == "pw1"


def test_use_file_store_merges_keychain_and_stray_file(fake_keyring, cred_file):
    """Opting in must not lose secrets from either side: union of both vaults,
    with the currently active store (the keychain here) winning conflicts."""
    from eufy_sync.credentials import use_file_store, store_token

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
    from eufy_sync.credentials import use_file_store, store_password

    store_password("default:eufy", "pw1")
    use_file_store()
    before = json.loads(cred_file.read_text())

    use_file_store()

    assert json.loads(cred_file.read_text()) == before


def test_use_file_store_keychain_read_failure_notes_and_continues(fake_keyring, cred_file, monkeypatch, capsys):
    """A locked keychain must not block opting into the file store; the
    existing file contents are adopted and a one-line note explains that the
    keychain secrets could not be copied."""
    from eufy_sync.credentials import use_file_store

    def boom(service, account):
        raise OSError("keychain locked")

    monkeypatch.setattr("keyring.get_password", boom)
    cred_file.parent.mkdir(parents=True, exist_ok=True)
    cred_file.write_text(json.dumps({"passwords": {"default:eufy": "file-pw"}, "tokens": {}}))

    use_file_store()

    out = capsys.readouterr().out
    assert "keychain" in out.lower()
    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert on_disk["passwords"]["default:eufy"] == "file-pw"


def test_use_file_store_keeps_keychain_vault_when_read_fails(fake_keyring, cred_file, monkeypatch, capsys):
    """Deleting the keychain vault item needs no read access, so after a
    failed read it would destroy the only copy of every secret that never
    made it into the file. The item must be left alone."""
    from eufy_sync.credentials import SERVICE_NAME, VAULT_ACCOUNT, use_file_store

    fake_keyring.set_password(SERVICE_NAME, VAULT_ACCOUNT, json.dumps({
        "passwords": {"default:eufy": "real-pw"}, "tokens": {},
    }))

    def boom(service, account):
        raise OSError("access denied")

    monkeypatch.setattr("keyring.get_password", boom)

    use_file_store()

    on_disk = json.loads(cred_file.read_text())
    assert on_disk["explicit"] is True
    assert (SERVICE_NAME, VAULT_ACCOUNT) in fake_keyring.data


def test_interrupted_file_save_keeps_previous_vault(fake_keyring, cred_file):
    """The vault file is replaced atomically: a write that dies partway
    through must leave the previous contents (and the opt-in marker) intact
    instead of truncating the file in place."""
    from eufy_sync.credentials import use_file_store, store_token, _active_backend

    use_file_store()
    store_token("garmin", {"di_token": "abc"})
    before = cred_file.read_bytes()

    with pytest.raises(TypeError):
        store_token("bad", {"obj": object()})  # json.dump raises mid-write

    assert cred_file.read_bytes() == before
    assert not cred_file.with_name(cred_file.name + ".tmp").exists()
    assert _active_backend() == "file"


def test_marker_survives_store_token_round_trip(fake_keyring, cred_file):
    """_normalize_vault must preserve the marker, or the first write after
    opting in would silently flip the backend to the keychain again."""
    from eufy_sync.credentials import use_file_store, store_token, _active_backend

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
        SERVICE_NAME, store_password, use_file_store, use_keychain_store, get_password,
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
    from eufy_sync.credentials import get_token, get_password

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
