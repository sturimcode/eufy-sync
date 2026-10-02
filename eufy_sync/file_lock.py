"""Cross-platform exclusive locks on a file (fcntl.flock or msvcrt.locking).

Shared by the sync lock (eufy_sync.cli.lock) and the credential vault lock
(eufy_sync.credentials). The OS drops a lock when its handle closes or the
process dies, so a killed process never leaves a stale lock behind.

On POSIX a lock belongs to the file, not the path. If a holder unlinks the
file (only --uninstall does), a process that opened the old file before the
unlink could lock it after the release while a third process creates and
locks a new file at the same path. acquire() rejects a lock on a file that is
no longer linked at its path and retries on the new one, so at most one
process ever holds the lock for a path.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

POLL_INTERVAL = 0.05
MAX_ORPHAN_RETRIES = 5


def _open(path: Path) -> int:
    """Open (creating if needed) the lock file. Raises OSError if it cannot."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)


def try_lock(fd: int) -> bool:
    """Take the exclusive lock without blocking. False when someone holds it."""
    try:
        if sys.platform == "win32":
            # msvcrt locks a byte range from the current position; one byte
            # past EOF is fine and keeps the file empty.
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def unlock(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Closing the handle releases it anyway.
        pass


def _still_linked(fd: int, path: Path) -> bool:
    if sys.platform == "win32":
        # Windows cannot delete a file another process has open.
        return True
    try:
        on_disk = os.stat(path)
    except OSError:
        return False
    held = os.fstat(fd)
    return (on_disk.st_dev, on_disk.st_ino) == (held.st_dev, held.st_ino)


def acquire(path: Path, timeout: float = 0.0) -> int | None:
    """Open `path` and take its exclusive lock, waiting up to `timeout`
    seconds for another holder to let go.

    Returns the open handle holding the lock (pass it to release()), or None
    if someone else still held it when the wait ran out. Raises OSError when
    the lock file cannot be created or opened."""
    deadline = time.monotonic() + timeout
    orphans = 0
    while True:
        fd = _open(path)
        if try_lock(fd):
            if _still_linked(fd, path):
                return fd
            # The previous holder unlinked the file while holding it; this
            # lock is on an orphan. Start over on whatever is at the path now.
            release(fd)
            orphans += 1
            if orphans <= MAX_ORPHAN_RETRIES:
                continue
        else:
            os.close(fd)
        if time.monotonic() >= deadline:
            return None
        time.sleep(POLL_INTERVAL)


def release(fd: int) -> None:
    unlock(fd)
    os.close(fd)
