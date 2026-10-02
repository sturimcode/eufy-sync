"""The single-instance lock that keeps a manual sync off the scheduled one."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from eufy_sync.cli import lock, shared
from eufy_sync.cli.shared import _write_config


def test_lock_file_lands_in_the_data_dir_and_is_created_on_demand():
    """The data dir may not exist yet on a first scheduled run, so taking the
    lock has to create it the same way the config and state writers do."""
    assert not shared.DATA_DIR.exists()

    with lock.single_instance() as acquired:
        assert acquired is True
        assert lock.lock_path() == shared.DATA_DIR / "sync.lock"
        assert lock.lock_path().exists()


def test_second_run_is_told_the_lock_is_held():
    """Two live handles on the same file: the second must not get the lock.
    This is the overlap the 4-hourly task creates when a manual run is already
    going."""
    with lock.single_instance() as first:
        assert first is True
        with lock.single_instance() as second:
            assert second is False


def test_lock_is_released_when_the_block_ends():
    with lock.single_instance() as first:
        assert first is True

    with lock.single_instance() as again:
        assert again is True


def test_lock_is_released_when_the_block_raises():
    """A sync that dies (or calls sys.exit) must not leave the next run
    locked out. The OS would drop it on process exit anyway; this covers the
    same-process case."""
    with pytest.raises(RuntimeError):
        with lock.single_instance() as first:
            assert first is True
            raise RuntimeError("sync blew up")

    with lock.single_instance() as again:
        assert again is True


def test_unusable_lock_file_runs_unlocked():
    """The lock is a courtesy. A data dir we cannot write to must not become a
    new way for the sync to refuse to start."""
    with patch("eufy_sync.file_lock.os.open", side_effect=OSError("read-only")):
        with lock.single_instance() as acquired:
            assert acquired is True
            # Nothing is holding anything, so a second run is not blocked.
            with lock.single_instance() as second:
                assert second is True


def test_strict_lock_refuses_to_run_when_lock_file_cannot_open():
    with patch("eufy_sync.file_lock.os.open", side_effect=OSError("read-only")):
        with lock.single_instance(require_lock=True) as acquired:
            assert acquired is False


def _write_synced_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "pw"},
            "garmin": {"email": "g@example.com", "password": "pw"},
        }],
    })
    return config_path


@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_sync_skips_and_exits_zero_when_a_run_is_in_progress(
    _keyring, _migrate, _notice, mock_updates, mock_notify, tmp_path, capsys
):
    """A scheduled run that collides with a manual one prints one line and
    stops. It must exit 0 and notify nothing - an overlap is not a failure,
    and a failure toast every four hours is exactly what this avoids."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]

    def boom(*a, **kw):
        raise AssertionError("sync must not run while another run holds the lock")

    with lock.single_instance() as held:
        assert held is True
        with patch("eufy_sync.sync.sync_user", side_effect=boom), \
             patch("sys.argv", argv):
            main()  # returns instead of raising SystemExit: exit code 0

    out = capsys.readouterr().out
    assert "another eufy-sync run is in progress" in out.lower()
    mock_notify.assert_not_called()
    # The skip happens before any other sync-path work, including the
    # password migration, which writes the credential vault.
    mock_updates.assert_not_called()
    _migrate.assert_not_called()


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.cli.setup._migrate_config_passwords")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_sync_runs_and_releases_the_lock_for_the_next_run(
    _keyring, _migrate, _notice, _updates, _notify, _summary, tmp_path
):
    """An uncontended run takes the lock, syncs, and leaves it free."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    db_path = tmp_path / "state.db"
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(db_path), "--headless"]

    with patch("eufy_sync.sync.sync_user", return_value=({"garmin": 1}, {})), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    with lock.single_instance() as acquired:
        assert acquired is True


class _DictKeyring:
    def __init__(self):
        self.data = {}
        self.writes = []

    def get_password(self, service, account):
        return self.data.get((service, account))

    def set_password(self, service, account, password):
        self.writes.append(account)
        self.data[(service, account)] = password

    def delete_password(self, service, account):
        self.writes.append(account)
        self.data.pop((service, account), None)


@pytest.mark.parametrize("flag", [["--status"], ["--history"]])
def test_status_and_history_read_credentials_without_writing_the_vault(flag, tmp_path, monkeypatch):
    """--status and --history run without the sync lock, so they must not
    write the vault: a sync may be saving it at that moment. Plaintext YAML
    passwords stay in the YAML and legacy per-password keychain items are
    read where they are, both left for the next locked run to migrate."""
    from eufy_sync import credentials
    from eufy_sync.cli.app import main

    store = _DictKeyring()
    monkeypatch.setattr("keyring.get_password", store.get_password)
    monkeypatch.setattr("keyring.set_password", store.set_password)
    monkeypatch.setattr("keyring.delete_password", store.delete_password)
    monkeypatch.setattr(credentials, "_keyring_available", lambda: True)
    monkeypatch.setattr(credentials, "CRED_FILE", tmp_path / "credentials.json")
    store.data[(credentials.SERVICE_NAME, "default:garmin")] = "legacy-pw"

    config_path = tmp_path / "config.yaml"
    _write_config(config_path, {
        "users": [{
            "name": "default",
            "eufy": {"email": "e@example.com", "password": "yaml-pw"},
            "garmin": {"email": "g@example.com"},
        }],
    })
    before = config_path.read_text()
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(tmp_path / "state.db"), *flag]

    with patch("sys.argv", argv), patch("eufy_sync.cli.setup._show_upgrade_notice"):
        main()

    assert store.writes == []
    assert store.data[(credentials.SERVICE_NAME, "default:garmin")] == "legacy-pw"
    assert config_path.read_text() == before


@patch("eufy_sync.cli.status._print_summary")
@patch("eufy_sync.platform_support.notify")
@patch("eufy_sync.cli.updater._check_for_updates")
@patch("eufy_sync.cli.setup._show_upgrade_notice")
@patch("eufy_sync.credentials._keyring_available", return_value=False)
def test_sync_runs_the_password_migration_while_holding_the_lock(
    _keyring, _notice, _updates, _notify, _summary, tmp_path
):
    """The migration writes the vault, so it runs inside the sync lock."""
    from eufy_sync.cli.app import main

    config_path = _write_synced_config(tmp_path)
    argv = ["eufy-sync", "--config", str(config_path), "--db", str(tmp_path / "state.db"), "--headless"]
    held = {}

    def migrate(path):
        with lock.single_instance() as other:
            held["by_us"] = other is False

    with patch("eufy_sync.cli.setup._migrate_config_passwords", side_effect=migrate), \
         patch("eufy_sync.sync.sync_user", return_value=({"garmin": 1}, {})), \
         patch("sys.argv", argv), \
         pytest.raises(SystemExit):
        main()

    assert held == {"by_us": True}


# ---------------------------------------------------------------------------
# Commands that change credentials, tokens, or config wait for a running sync
# ---------------------------------------------------------------------------

# (extra argv, function the command runs, flag named in the retry hint)
_CREDENTIAL_COMMANDS = [
    (["--uninstall"], "eufy_sync.cli.maintenance._uninstall", "--uninstall"),
    (["--use-file-store"], "eufy_sync.credentials.use_file_store", "--use-file-store"),
    (["--use-keychain"], "eufy_sync.credentials.use_keychain_store", "--use-keychain"),
    (["--update"], "eufy_sync.cli.updater._self_update", "--update"),
    (["--setup-strava"], "eufy_sync.cli.setup._setup_strava", "--setup-strava"),
    (["--setup-zwift"], "eufy_sync.cli.setup._setup_zwift", "--setup-zwift"),
    (["--disconnect-zwift"], "eufy_sync.cli.maintenance._disconnect_zwift", "--disconnect-zwift"),
    (["--setup-intervals"], "eufy_sync.cli.setup._setup_intervals", "--setup-intervals"),
    (["--disconnect-intervals"], "eufy_sync.cli.maintenance._disconnect_intervals", "--disconnect-intervals"),
    (["--select-profile"], "eufy_sync.cli.profiles._select_profile", "--select-profile"),
    (["--update-password"], "eufy_sync.cli.maintenance._update_password", "--update-password"),
    (["--reauth"], "eufy_sync.cli.maintenance._reauth", "--reauth"),
    (["--reauth", "garmin"], "eufy_sync.cli.maintenance._reauth", "--reauth"),
    # No config: the first-run wizard, which stores passwords and logs in.
    ([], "eufy_sync.cli.setup._first_run_setup", "eufy-sync"),
]


def _command_argv(tmp_path: Path, extra: list[str]) -> list[str]:
    # The config path does not exist, so a bare run reaches first-run setup.
    return ["eufy-sync", "--config", str(tmp_path / "missing.yaml"), *extra]


@pytest.mark.parametrize("extra, target, flag", _CREDENTIAL_COMMANDS)
def test_credential_command_refuses_while_a_sync_holds_the_lock(tmp_path, capsys, extra, target, flag):
    from eufy_sync.cli.app import main

    with lock.single_instance() as held:
        assert held is True
        with patch(target) as command, \
             patch("sys.argv", _command_argv(tmp_path, extra)), \
             pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 1
    command.assert_not_called()
    assert f"Retry {flag} when it finishes" in capsys.readouterr().out


@pytest.mark.parametrize("extra, target, flag", _CREDENTIAL_COMMANDS)
def test_credential_command_runs_while_holding_the_lock(tmp_path, extra, target, flag):
    from eufy_sync.cli.app import main

    seen = []

    def check_lock(*args, **kwargs):
        with lock.single_instance() as other:
            seen.append(other)

    with patch(target, side_effect=check_lock), \
         patch("sys.argv", _command_argv(tmp_path, extra)):
        try:
            main()
        except SystemExit:
            pass  # several of these commands exit on their own

    assert seen == [False]   # a sync starting mid-command would have been kept out
    with lock.single_instance() as acquired:
        assert acquired is True   # and the lock is free again afterwards


@pytest.mark.parametrize("extra, target, flag", _CREDENTIAL_COMMANDS)
def test_credential_command_refuses_when_the_lock_file_cannot_open(tmp_path, capsys, extra, target, flag):
    from eufy_sync.cli.app import main

    with patch("eufy_sync.file_lock.os.open", side_effect=OSError("read-only")), \
         patch(target) as command, \
         patch("sys.argv", _command_argv(tmp_path, extra)), \
         pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    command.assert_not_called()


@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.platform_support.agent_installed", return_value=False)
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", return_value="y")
def test_uninstall_under_the_lock_still_removes_the_whole_data_dir(
    _input, mock_stdin, _agent, _keyring, tmp_path
):
    """--uninstall holds the lock file open while it sweeps the data dir, and
    Windows cannot delete an open file. The sweep skips it and the command
    removes it after release, so nothing is left behind."""
    from eufy_sync.cli.app import main

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(shared.DATA_DIR)
    with patch("sys.argv", ["eufy-sync", "--uninstall", "--config", str(config_path)]):
        main()

    assert not config_path.exists()
    assert not lock.lock_path().exists()
    assert not shared.DATA_DIR.exists()


@patch("eufy_sync.credentials._keyring_available", return_value=False)
@patch("eufy_sync.platform_support.agent_installed", return_value=False)
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", return_value="y")
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX unlink ordering")
def test_uninstall_deletes_each_lock_file_before_releasing_it(_input, mock_stdin, _agent, _keyring, tmp_path):
    """Deleting a lock file after release lets another process lock the old
    file while a third creates and locks a new one at the same path. On
    POSIX every lock --uninstall releases must already be unlinked."""
    import os

    from eufy_sync import file_lock
    from eufy_sync.cli.app import main

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(shared.DATA_DIR)
    real_release = file_lock.release
    links_at_release = []

    def spy(fd):
        links_at_release.append(os.fstat(fd).st_nlink)
        real_release(fd)

    with patch("eufy_sync.file_lock.release", spy), \
         patch("sys.argv", ["eufy-sync", "--uninstall", "--config", str(config_path)]):
        main()

    # The vault lock (released when the sweep ends) and the sync lock.
    assert len(links_at_release) == 2
    assert links_at_release == [0, 0]
    assert not shared.DATA_DIR.exists()


@patch("eufy_sync.credentials._keyring_available", return_value=True)
@patch("eufy_sync.platform_support.agent_installed", return_value=False)
@patch("eufy_sync.cli.maintenance.sys.stdin")
@patch("builtins.input", side_effect=["y", "n"])
@pytest.mark.skipif(sys.platform == "win32", reason="POSIX unlink ordering")
def test_uninstall_removes_the_vault_lock_last_and_holds_it_throughout(
    _input, mock_stdin, _agent, _keyring, tmp_path
):
    """A vault lock file deleted mid-sweep lets another process create and
    lock a fresh one while uninstall is still removing credentials. Even when
    the directory listing yields vault.lock first, every other removal (data
    dir and keychain) must happen while it is still on disk and held, and it
    must be the last thing deleted under the vault lock."""
    import shutil

    from eufy_sync import credentials
    from eufy_sync.cli.app import main
    from eufy_sync.credentials import store_password, store_token, vault_lock_path

    mock_stdin.isatty.return_value = True
    config_path = _write_synced_config(shared.DATA_DIR)
    store_password("default:eufy", "pw")
    store_token("garmin", {"t": 1})
    (shared.DATA_DIR / "credentials.json").write_text('{"passwords": {}, "tokens": {}}')
    (shared.DATA_DIR / "state.db").write_text("")
    (shared.DATA_DIR / "cache").mkdir()
    (shared.DATA_DIR / "cache" / "x").write_text("")
    vault_lock_path().touch()

    events = []

    def record(what):
        events.append((what, vault_lock_path().exists(), credentials._lock_depth > 0))

    real_unlink = Path.unlink
    real_rmtree = shutil.rmtree
    real_iterdir = Path.iterdir
    real_delete_password = credentials.delete_password
    real_delete_token = credentials.delete_token

    def unlink(self, *args, **kwargs):
        if self.parent == shared.DATA_DIR:
            record(self.name)
        return real_unlink(self, *args, **kwargs)

    def rmtree(path, *args, **kwargs):
        record(Path(path).name)
        return real_rmtree(path, *args, **kwargs)

    def iterdir(self):
        items = list(real_iterdir(self))
        # Worst case for the old loop: the vault lock comes first.
        return iter(sorted(items, key=lambda p: p.name != "vault.lock"))

    def delete_password(account):
        record(f"password:{account}")
        real_delete_password(account)

    def delete_token(name):
        record(f"token:{name}")
        real_delete_token(name)

    with patch.object(Path, "unlink", unlink), \
         patch.object(Path, "iterdir", iterdir), \
         patch("eufy_sync.cli.maintenance.shutil.rmtree", rmtree), \
         patch("eufy_sync.credentials.delete_password", delete_password), \
         patch("eufy_sync.credentials.delete_token", delete_token), \
         patch("sys.argv", ["eufy-sync", "--uninstall", "--config", str(config_path)]):
        main()

    names = [name for name, _, _ in events]
    assert "vault.lock" in names
    vault_index = names.index("vault.lock")
    # Only the sync lock (handled by the caller) may go after the vault lock.
    assert names[vault_index + 1:] == ["sync.lock"]
    for name in ("credentials.json", "config.yaml", "state.db", "cache",
                 "password:default:eufy", "token:garmin"):
        assert name in names[:vault_index]
    for name, lock_on_disk, held in events[:vault_index + 1]:
        assert lock_on_disk and held, name
    assert not shared.DATA_DIR.exists()


def test_windows_uninstall_deletes_lock_files_only_after_release(tmp_path, monkeypatch):
    """Windows cannot delete an open file, so the lock files are skipped
    while held and deleted by _remove_lock_files afterwards."""
    from types import SimpleNamespace

    from eufy_sync.cli import maintenance

    fake_sys = SimpleNamespace(platform="win32")
    monkeypatch.setattr(lock, "sys", fake_sys)
    monkeypatch.setattr(maintenance, "sys", fake_sys)
    shared.DATA_DIR.mkdir(parents=True)
    for name in ("sync.lock", "vault.lock"):
        (shared.DATA_DIR / name).write_text("")

    lock.unlink_while_held()
    assert lock.lock_path().exists()
    maintenance._remove_lock_files(shared.DATA_DIR)
    assert not shared.DATA_DIR.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_posix_remove_lock_files_never_deletes_a_lock_file_after_release(tmp_path):
    """On POSIX a lock file present after release may be a new one another
    process just created and locked; it must be left alone."""
    from eufy_sync.cli import maintenance

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "sync.lock").write_text("")
    maintenance._remove_lock_files(data_dir)
    assert (data_dir / "sync.lock").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_lock_on_an_unlinked_file_is_retried_on_the_new_file(tmp_path):
    """A process that opened the lock file before a holder unlinked it must
    not treat a lock on that orphan as the lock for the path."""
    import os

    from eufy_sync import file_lock

    path = tmp_path / "x.lock"
    stale = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    path.unlink()
    real_open = file_lock._open
    opened = []

    def first_open_is_stale(p):
        if not opened:
            opened.append(stale)
            return stale
        fd = real_open(p)
        opened.append(fd)
        return fd

    with patch("eufy_sync.file_lock._open", first_open_is_stale):
        fd = file_lock.acquire(path)
    # acquire noticed the orphan and opened the path a second time. Compare
    # against the file now at the path, not the stale inode number: once the
    # orphan is closed, filesystems such as ext4 may hand that number out again.
    assert fd is not None and len(opened) == 2
    assert os.fstat(fd).st_ino == os.stat(path).st_ino
    file_lock.release(fd)
