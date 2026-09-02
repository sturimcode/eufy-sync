"""Decide when a failed headless sync should actually notify.

A scheduled sync fails most often because the network was briefly gone (the
Mac was asleep, Wi-Fi was flapping) when the 4-hourly timer fired. Those
failures self-heal on the next run, so firing a scary "eufy-sync failed"
notification for each one trains the user to ignore the notifications that
matter (a bad password, a revoked token).

So transient network failures on headless runs stay silent until several land
in a row, at which point one notification says the network has been down a
while and syncs are waiting. Any successful run clears the streak. Real
failures (auth, password, profile) always notify immediately and are handled
by the caller, never routed through here.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from eufy_sync.cli import shared

# Re-exported: app.py and the tests reach the classifier through this module.
from eufy_sync.network import is_transient_network_error  # noqa: F401

# Consecutive headless network failures before one escalation notification.
# The timer runs every 4h, so 3 in a row means the network has been gone for
# roughly 8-12h - long enough to be worth telling the user about.
THRESHOLD = 3

_STREAK_FILE = "network_fail_streak.json"

def _streak_path() -> Path:
    return shared.DATA_DIR / _STREAK_FILE


def _read() -> tuple[int, float]:
    try:
        data = json.loads(_streak_path().read_text())
    except (OSError, ValueError):
        return 0, 0.0
    # Valid JSON that is not an object (a list, string, number, or null) has no
    # .get, and a non-numeric count/since would not convert. Any of these means
    # a corrupt counter, which must reset to zero, never crash the sync.
    if not isinstance(data, dict):
        return 0, 0.0
    try:
        return int(data.get("count", 0)), float(data.get("since", 0.0))
    except (ValueError, TypeError):
        return 0, 0.0


def _write(count: int, since: float) -> None:
    try:
        shared.DATA_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        _streak_path().write_text(json.dumps({"count": count, "since": since}))
    except OSError:
        # Losing the counter only means one extra (or one skipped) notification;
        # never let it break the run.
        pass


def record_network_failure(now: float | None = None) -> tuple[int, float]:
    """Add one to the consecutive-network-failure streak.

    Returns (count, hours_since_first): the new streak length and how long the
    network has been failing, measured from the first failure in this streak.
    """
    now = time.time() if now is None else now
    count, since = _read()
    if count <= 0:
        since = now
    count += 1
    _write(count, since)
    hours = max(0.0, (now - since) / 3600.0)
    return count, hours


def should_escalate(count: int) -> bool:
    """Notify exactly once, when the streak first reaches the threshold - not
    again on every later run of a long outage (a success clears the streak and
    lets a fresh outage escalate again)."""
    return count == THRESHOLD


def clear_network_failures() -> None:
    """Reset the streak. Called on any successful sync or a non-network
    failure, so an isolated blot never counts toward the escalation."""
    path = _streak_path()
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass
