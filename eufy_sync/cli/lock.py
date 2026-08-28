"""Single-instance lock for the sync path.

A manual run and the 4-hourly scheduled run can overlap. Two syncs at once can
upload the same measurement twice, and both can refresh the Strava token - the
loser then saves a token the rotation already killed. An exclusive OS lock on
one file in the data dir keeps the second run out of the sync path; the
read-only commands (--status, --history, --doctor) and the setup/maintenance
commands stay unlocked.

The lock is a courtesy, not a guarantee. If the file cannot be created the run
proceeds unlocked rather than failing: an overlap is a rare annoyance, while a
sync that refuses to start is a real one (same trade-off failure_notify makes
with its counter file).

There is no PID or staleness handling on purpose. The OS drops the lock when
the handle closes or the process dies, so a killed run leaves nothing behind.
"""
from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path
from typing import Iterator

from eufy_sync.cli import shared

LOCK_NAME = "sync.lock"

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


def lock_path() -> Path:
    # Read at call time: the data dir is redirected in tests.
    return shared.DATA_DIR / LOCK_NAME


def _open() -> int | None:
    """Open (creating if needed) the lock file. None if it cannot be made."""
    try:
        shared.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        return os.open(str(lock_path()), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return None


def _try_acquire(fd: int) -> bool:
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


def _release(fd: int) -> None:
    try:
        if sys.platform == "win32":
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        # Closing the handle below releases it anyway.
        pass


@contextlib.contextmanager
def single_instance() -> Iterator[bool]:
    """Hold the sync lock for the block.

    Yields True when this run owns the lock, or when the lock file could not
    be created at all and the run continues unlocked. Yields False when
    another run holds it, and the caller should skip this run.
    """
    fd = _open()
    if fd is None:
        yield True
        return
    try:
        if not _try_acquire(fd):
            yield False
            return
        try:
            yield True
        finally:
            _release(fd)
    finally:
        os.close(fd)
