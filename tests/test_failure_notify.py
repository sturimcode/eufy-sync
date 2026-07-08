from __future__ import annotations

import json

import pytest

from eufy_sync.cli import failure_notify as fn


# --- classification ----------------------------------------------------------


@pytest.mark.parametrize("msg", [
    "default: [Errno 8] nodename nor servname provided, or not known",
    "_ssl.c:1063: The handshake operation timed out",
    "[Errno -2] Name or service not known",
    "Temporary failure in name resolution",
    "Connection reset by peer",
    "Read timed out.",
    "The read operation timed out",          # httpx ReadTimeout
    "timed out",                             # bare socket/httpx ConnectTimeout
    "All connection attempts failed",        # httpx ConnectError, common on wake
    "Connection aborted.",
    "HTTPSConnectionPool: Max retries exceeded with url",
    "[Errno 51] Network is unreachable",
    "Connection refused",
])
def test_recognizes_transient_network_errors(msg):
    assert fn.is_transient_network_error(msg) is True


@pytest.mark.parametrize("msg", [
    "Re-authenticate with: eufy-sync --reauth garmin",
    "You changed your Eufy password. Run: eufy-sync --update-password",
    "multiple Eufy profiles; run eufy-sync --select-profile",
    "API Error 401 - ",
    "Invalid credentials",
    "",
    # Persistent TLS problems carry _ssl.c too, but they are user-actionable
    # (clock, CA bundle, proxy) and must NOT be silenced as a passing blip.
    "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)",
    "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1006)",
])
def test_real_failures_are_not_transient(msg):
    assert fn.is_transient_network_error(msg) is False


# --- streak counter ----------------------------------------------------------


def test_record_increments_and_persists():
    # The autouse hermetic fixture points shared.DATA_DIR at tmp, which is
    # where the streak file lands.
    count, _ = fn.record_network_failure(now=1000.0)
    assert count == 1
    count, _ = fn.record_network_failure(now=1000.0 + 4 * 3600)
    assert count == 2


def test_record_returns_hours_since_first_failure():
    _, hours = fn.record_network_failure(now=1000.0)
    assert hours == 0.0
    _, hours = fn.record_network_failure(now=1000.0 + 8 * 3600)
    # Two failures 8h apart: the streak started at the first, so ~8h down.
    assert round(hours) == 8


def test_should_escalate_only_at_threshold():
    assert fn.THRESHOLD == 3
    assert fn.should_escalate(1) is False
    assert fn.should_escalate(2) is False
    assert fn.should_escalate(3) is True
    # Past the threshold it must not re-fire every run.
    assert fn.should_escalate(4) is False
    assert fn.should_escalate(6) is False


def test_clear_resets_streak():
    fn.record_network_failure(now=1000.0)
    fn.record_network_failure(now=2000.0)
    fn.clear_network_failures()
    count, _ = fn.record_network_failure(now=3000.0)
    assert count == 1  # started over after clear


def test_clear_is_safe_when_no_streak_file():
    # Must not raise when there is nothing to clear.
    fn.clear_network_failures()
    fn.clear_network_failures()


def test_streak_survives_across_reads():
    """Each scheduled run is a fresh process, so the count must come off disk,
    not memory."""
    from eufy_sync.cli import shared
    fn.record_network_failure(now=1000.0)
    fn.record_network_failure(now=2000.0)
    on_disk = json.loads((shared.DATA_DIR / "network_fail_streak.json").read_text())
    assert on_disk["count"] == 2


@pytest.mark.parametrize("contents", ["{garbage", "[1, 2, 3]", "null", '"x"', "12"])
def test_corrupt_streak_file_counts_as_zero_and_never_raises(contents):
    """Invalid JSON or valid-but-non-object JSON must reset the counter to
    zero, not raise (which would crash a headless sync with no outer except)."""
    from eufy_sync.cli import shared
    shared.DATA_DIR.mkdir(parents=True, exist_ok=True)
    (shared.DATA_DIR / "network_fail_streak.json").write_text(contents)
    count, _ = fn.record_network_failure(now=1000.0)
    assert count == 1
