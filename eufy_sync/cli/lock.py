"""Single-instance lock for the sync path.

A manual run and the 4-hourly scheduled run can overlap. Two syncs at once can
upload the same measurement twice, and both can refresh the Strava token - the
loser then saves a token the rotation already killed. An exclusive OS lock on
one file in the data dir keeps the second run out of the sync path; the
live diagnostics share this lock, since they can refresh tokens too. The
read-only commands (--status, --history) stay unlocked. Credential-changing
commands require the lock instead of continuing if the lock file cannot open.

The lock is a courtesy, not a guarantee. If the file cannot be created the run
proceeds unlocked rather than failing: an overlap is a rare annoyance, while a
sync that refuses to start is a real one (same trade-off failure_notify makes
with its counter file). The credential vault does not depend on this: every
vault write separately takes the vault lock in eufy_sync.credentials, which
refuses to write rather than run unlocked.

There is no PID or staleness handling on purpose. The OS drops the lock when
the handle closes or the process dies, so a killed run leaves nothing behind.
"""
from __future__ import annotations

import contextlib
import sys
from pathlib import Path
from typing import Iterator

from eufy_sync import file_lock
from eufy_sync.cli import shared

LOCK_NAME = "sync.lock"


def lock_path() -> Path:
    # Read at call time: the data dir is redirected in tests.
    return shared.DATA_DIR / LOCK_NAME


@contextlib.contextmanager
def single_instance(require_lock: bool = False) -> Iterator[bool]:
    """Hold the sync lock for the block.

    Yields True when this run owns the lock. By default, failure to create the
    lock file also yields True so ordinary sync remains available. With
    require_lock=True, that failure yields False so credential and token
    mutations cannot proceed without serialization.
    """
    try:
        fd = file_lock.acquire(lock_path())
    except OSError:
        yield not require_lock
        return
    if fd is None:
        yield False
        return
    try:
        yield True
    finally:
        file_lock.release(fd)


def unlink_while_held() -> None:
    """Delete the lock file while this process still holds it (call inside
    single_instance). Deleting it after release would let another process
    lock the old file while a third creates and locks a new one at the same
    path. POSIX only: Windows refuses to delete an open file, so there the
    caller deletes it after release, and a process that has it open blocks
    that delete."""
    if sys.platform == "win32":
        return
    try:
        lock_path().unlink(missing_ok=True)
    except OSError:
        pass
