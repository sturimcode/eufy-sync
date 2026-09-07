import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.config import EufyConfig, GarminConfig, StravaConfig, UserConfig
from eufy_sync.eufy_client import EufyMeasurement
from eufy_sync.state import SyncState
from eufy_sync.sync import sync_user


def test_state_init_and_roundtrip(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")

    assert not state.is_synced("user1", "m1", "garmin")

    state.record_sync(
        user_name="user1",
        measurement_id="m1",
        measurement_timestamp="2024-04-01T12:00:00+00:00",
        weight_kg=86.2,
        synced_at="2024-04-01T12:01:00+00:00",
        target="garmin",
        response='{"ok": true}',
    )

    assert state.is_synced("user1", "m1", "garmin")
    assert not state.is_synced("user1", "m2", "garmin")
    assert not state.is_synced("roommate", "m1", "garmin")

    state.close()


def test_state_creates_parent_directory(tmp_path: Path):
    """SyncState must create its parent directory if it doesn't exist yet,
    the same way the token/config writers do - otherwise a not-yet-created
    data dir crashes with a raw sqlite OperationalError."""
    db_path = tmp_path / "sub" / "dir" / "state.db"
    assert not db_path.parent.exists()

    state = SyncState(db_path)
    try:
        assert db_path.parent.exists()
        assert db_path.exists()
        # POSIX modes only; Windows does not honor them.
        if os.name != "nt":
            mode = oct(db_path.parent.stat().st_mode)[-3:]
            assert mode == "700"
    finally:
        state.close()


def test_latest_sync_timestamp(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")

    assert state.get_latest_sync_timestamp("user1") is None

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", target="garmin")
    state.record_sync("user1", "m2", "2024-04-02T08:00:00+00:00", 85.9, "2024-04-02T08:01:00+00:00", target="garmin")

    ts = state.get_latest_sync_timestamp("user1")
    assert ts is not None
    assert ts > 0

    state.close()


def test_duplicate_insert_rejected(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", target="garmin")

    try:
        state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:02:00+00:00", target="garmin")
        raise AssertionError("Should have raised")
    except sqlite3.IntegrityError:
        pass

    state.close()


def test_multi_target_tracking(tmp_path: Path):
    """Same measurement can be synced to different targets independently."""
    state = SyncState(tmp_path / "test.db")

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", target="garmin")

    assert state.is_synced("user1", "m1", "garmin")
    assert not state.is_synced("user1", "m1", "strava")

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:30+00:00", target="strava")

    assert state.is_synced("user1", "m1", "garmin")
    assert state.is_synced("user1", "m1", "strava")

    state.close()


def test_v1_to_v2_migration(tmp_path: Path):
    """Existing v1 databases (garmin-only) migrate automatically."""
    db_path = tmp_path / "test.db"

    # Create a v1 schema database manually
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT NOT NULL,
            eufy_measurement_id TEXT NOT NULL,
            measurement_timestamp TEXT NOT NULL,
            weight_kg REAL,
            synced_to_garmin_at TEXT NOT NULL,
            garmin_response TEXT,
            UNIQUE(user_name, eufy_measurement_id)
        );
    """)
    conn.execute(
        "INSERT INTO sync_log (user_name, eufy_measurement_id, measurement_timestamp, weight_kg, synced_to_garmin_at, garmin_response) VALUES (?, ?, ?, ?, ?, ?)",
        ("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", '{"ok": true}'),
    )
    conn.commit()
    conn.close()

    # Opening with SyncState should auto-migrate
    state = SyncState(db_path)

    # Old data should be accessible with target="garmin"
    assert state.is_synced("user1", "m1", "garmin")
    assert not state.is_synced("user1", "m1", "strava")

    # Should be able to add strava sync for the same measurement
    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:02:00+00:00", target="strava")
    assert state.is_synced("user1", "m1", "strava")

    state.close()


def test_get_history(tmp_path: Path):
    """get_history returns recent measurements with per-target status."""
    state = SyncState(tmp_path / "test.db")

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", target="garmin")
    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:30+00:00", target="strava")
    state.record_sync("user1", "m2", "2024-04-02T08:00:00+00:00", 85.9, "2024-04-02T08:01:00+00:00", target="garmin")

    history = state.get_history("user1", limit=10)
    assert len(history) == 2

    # Most recent first
    assert history[0]["weight_kg"] == 85.9
    assert history[0]["targets"] == {"garmin"}

    assert history[1]["weight_kg"] == 86.2
    assert history[1]["targets"] == {"garmin", "strava"}

    state.close()


def test_get_history_empty(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    assert state.get_history("user1") == []
    state.close()


def test_has_synced_on_date(tmp_path: Path):
    """has_synced_on_date is true only for the local calendar date of a
    recorded garmin sync, and only for the matching target."""
    state = SyncState(tmp_path / "test.db")

    recorded_dt = datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    local_date = recorded_dt.astimezone().date()
    other_date = (recorded_dt + timedelta(days=5)).astimezone().date()

    assert state.has_synced_on_date("user1", "garmin", local_date) is False

    state.record_sync(
        "user1", "m1", recorded_dt.isoformat(), 86.2,
        "2024-04-01T12:01:00+00:00", target="garmin",
    )

    assert state.has_synced_on_date("user1", "garmin", local_date) is True
    assert state.has_synced_on_date("user1", "garmin", other_date) is False
    # Different target, same date: no garmin record exists for it.
    assert state.has_synced_on_date("user1", "strava", local_date) is False

    state.close()


def _measurement(weight_kg: float, dt: datetime) -> EufyMeasurement:
    return EufyMeasurement(
        measurement_id=f"cust_{int(dt.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=dt,
        weight_kg=weight_kg,
    )


def test_strava_receives_only_the_newest_valid_measurement(tmp_path: Path):
    """Skip history and invalid later readings when choosing Strava's current weight."""
    state = SyncState(tmp_path / "test.db")

    older = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    middle = _measurement(85.5, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc))
    newest = _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc))

    # Eufy API typically returns newest-first
    invalid = _measurement(0.0, newest.timestamp + timedelta(days=1))
    fetched = [invalid, newest, middle, older]

    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        strava=StravaConfig(client_id="cid", client_secret="csec"),
    )

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = fetched
    fake_eufy.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.return_value = None
    fake_strava.update_weight.return_value = {"weight": None}
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert errors == {}

    weights_uploaded = [call.args[0] for call in fake_strava.update_weight.call_args_list]
    assert weights_uploaded == [86.0]
    assert counts == {"strava": 1}
    assert state.is_synced(user.name, newest.measurement_id, "strava")
    assert not state.is_synced(user.name, older.measurement_id, "strava")
    assert state.get_latest_sync_timestamp(user.name, "strava") == int(newest.timestamp.timestamp())

    state.close()


def test_dry_run_prints_preview_and_records_nothing(tmp_path: Path, capsys):
    """--dry-run must be honest: it previews each measurement via print (so
    it is visible at default log level) and never writes to state, since a
    dry run is not a real sync."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()

    first = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    second = _measurement(84.7, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc))

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = [first, second]
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin):
        counts, errors = sync_user(user, state, backfill_days=7, dry_run=True)

    assert errors == {}
    assert counts == {"garmin": 2}

    out = capsys.readouterr().out
    assert out.count("[DRY RUN] Would sync") == 2

    fake_garmin.upload_body_composition.assert_not_called()
    assert not state.is_synced(user.name, first.measurement_id, "garmin")
    assert not state.is_synced(user.name, second.measurement_id, "garmin")

    state.close()


def test_latest_timestamp_is_target_agnostic(tmp_path: Path):
    """get_latest_sync_timestamp returns the most recent across all targets."""
    state = SyncState(tmp_path / "test.db")

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", target="garmin")
    state.record_sync("user1", "m2", "2024-04-02T08:00:00+00:00", 85.9, "2024-04-02T08:01:00+00:00", target="strava")

    ts = state.get_latest_sync_timestamp("user1")
    # Should return the later timestamp (m2) regardless of target
    assert ts is not None
    assert ts > 0

    state.close()


def test_ambiguous_profile_error_is_permanent():
    from eufy_sync.eufy_client import AmbiguousProfileError
    from eufy_sync.sync import _is_permanent
    assert _is_permanent(AmbiguousProfileError([])) is True


def test_rate_limit_error_is_permanent():
    from garminconnect import GarminConnectTooManyRequestsError

    from eufy_sync.sync import _is_permanent
    assert _is_permanent(GarminConnectTooManyRequestsError("429")) is True


def _garmin_user() -> UserConfig:
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw"),
    )


def _run_garmin_sync(user, state, measurements, has_weight_on_date_return, repair_days=None, dry_run=False):
    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = measurements
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.has_weight_on_date.return_value = has_weight_on_date_return
    fake_garmin.upload_body_composition.return_value = {"ok": True}
    fake_garmin.close.return_value = None

    kwargs = {"repair_days": repair_days} if repair_days else {"backfill_days": 7}
    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.sync.time.sleep"):
        sync_user(user, state, dry_run=dry_run, **kwargs)  # returns (counts, errors); not needed here

    return fake_garmin


def test_corrected_same_day_reweigh_still_uploads_to_garmin(tmp_path: Path):
    """A re-weigh later the same local day must still upload to Garmin even
    though Garmin (per has_weight_on_date) already has an entry for that
    date, as long as WE were the one who put it there (our sync_log has a
    garmin record for that local date). This is the corrected-re-weigh case:
    without the fix, the guard cannot tell our own earlier upload from
    another source's and permanently skips the correction."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()

    morning = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    corrected = _measurement(84.7, datetime(2026, 5, 10, 8, 30, tzinfo=timezone.utc))

    # Run 1: Garmin has nothing yet for the date, measurement A syncs normally.
    fake_garmin_1 = _run_garmin_sync(user, state, [morning], has_weight_on_date_return=False)
    fake_garmin_1.upload_body_composition.assert_called_once()
    assert state.is_synced(user.name, morning.measurement_id, "garmin")

    # Run 2: Garmin now reports an entry for the date (our own upload from
    # run 1), but our sync_log also has a garmin record for that local date,
    # so measurement B (the correction) must still be uploaded.
    fake_garmin_2 = _run_garmin_sync(user, state, [corrected], has_weight_on_date_return=True)
    fake_garmin_2.upload_body_composition.assert_called_once()

    assert state.is_synced(user.name, corrected.measurement_id, "garmin")
    cursor = state._conn.execute(
        "SELECT response FROM sync_log WHERE eufy_measurement_id = ?",
        (corrected.measurement_id,),
    )
    response = cursor.fetchone()[0]
    assert response != '{"skipped": "already_in_garmin"}'

    state.close()


def _garmin_and_strava_user() -> UserConfig:
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw"),
        strava=StravaConfig(client_id="cid", client_secret="csec"),
    )


def test_one_target_auth_failure_does_not_block_the_other(tmp_path: Path):
    """A dead Strava token must not prevent Garmin from syncing. sync_user
    returns (counts, errors); the surviving target still uploads."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    measurement = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = [measurement]
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.has_weight_on_date.return_value = False
    fake_garmin.upload_body_composition.return_value = {"ok": True}
    fake_garmin.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.side_effect = RuntimeError("Strava token revoked")
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert counts["garmin"] == 1
    assert "strava" not in counts
    assert "Strava token revoked" in errors["strava"]
    fake_garmin.upload_body_composition.assert_called_once()
    fake_strava.update_weight.assert_not_called()

    state.close()


def test_all_targets_auth_failure_raises(tmp_path: Path):
    """If every target fails to authenticate, sync_user raises (whole-user
    failure) rather than silently doing nothing."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.side_effect = RuntimeError("Garmin dead token")
    fake_garmin.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.side_effect = RuntimeError("Strava dead token")
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        try:
            sync_user(user, state, backfill_days=7)
            raise AssertionError("Should have raised")
        except RuntimeError as e:
            assert "Garmin dead token" in str(e)

    state.close()


def _run_dual_sync(user, state, measurements, garmin_upload=None, strava_upload=None):
    """Run sync_user with both targets faked. garmin_upload/strava_upload are
    passed straight to the upload mocks as side_effect, so a test can make one
    or both give up. Returns (counts, errors, fake_garmin, fake_strava)."""
    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = measurements
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.has_weight_on_date.return_value = False
    fake_garmin.upload_body_composition.side_effect = garmin_upload
    if garmin_upload is None:
        fake_garmin.upload_body_composition.return_value = {"ok": True}
    fake_garmin.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.return_value = None
    fake_strava.update_weight.side_effect = strava_upload
    if strava_upload is None:
        fake_strava.update_weight.return_value = {"weight": None}
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7, headless=True)

    return counts, errors, fake_garmin, fake_strava


def test_garmin_upload_failure_does_not_stop_strava(tmp_path: Path):
    """A session that dies mid-run must drop Garmin alone. Before the fix the
    PermanentSyncError escaped sync_user and Strava - which was healthy - lost
    the rest of the run too. The reauth hint must come back in errors, intact
    enough for the CLI to key its notification off."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    first = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    second = _measurement(84.7, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc))

    from eufy_sync.sync import PermanentSyncError
    dead_session = PermanentSyncError(
        "Garmin wants an MFA code and no one is here to type it. Run: eufy-sync --reauth garmin"
    )

    counts, errors, fake_garmin, fake_strava = _run_dual_sync(
        user, state, [first, second], garmin_upload=dead_session,
    )

    assert "--reauth" in errors["garmin"]
    assert "strava" not in errors
    assert counts["strava"] == 1
    assert counts["garmin"] == 0

    # Garmin is dropped after the first failure, not retried per measurement.
    assert fake_garmin.upload_body_composition.call_count == 1
    weights = [call.args[0] for call in fake_strava.update_weight.call_args_list]
    assert weights == [84.7]

    # Nothing is recorded for the upload that failed.
    assert not state.is_synced(user.name, first.measurement_id, "garmin")
    assert not state.is_synced(user.name, first.measurement_id, "strava")
    assert state.is_synced(user.name, second.measurement_id, "strava")

    state.close()


def test_all_upload_failures_return_errors_without_raising(tmp_path: Path):
    """With every target dropped there is nothing left to sync, but sync_user
    still returns: the caller turns a non-empty errors dict into failures and
    exit code 1."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    first = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    second = _measurement(84.7, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc))

    from eufy_sync.sync import PermanentSyncError

    counts, errors, fake_garmin, fake_strava = _run_dual_sync(
        user, state, [first, second],
        garmin_upload=PermanentSyncError("Garmin wants an MFA code and no one is here to type it. Run: eufy-sync --reauth garmin"),
        strava_upload=PermanentSyncError("Strava token rejected"),
    )

    assert set(errors) == {"garmin", "strava"}
    assert counts == {"garmin": 0, "strava": 0}
    # Each target is dropped after its one attempted upload. Strava only
    # attempts the newest reading, even after Garmin failed on the oldest.
    assert fake_garmin.upload_body_composition.call_count == 1
    assert fake_strava.update_weight.call_count == 1

    state.close()


def test_exhausted_retries_drop_only_the_failing_target(tmp_path: Path):
    """The containment is not limited to permanent errors: a transient failure
    that outlives MAX_RETRIES drops that target too, and the other keeps
    syncing."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    measurement = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    counts, errors, fake_garmin, fake_strava = _run_dual_sync(
        user, state, [measurement],
        strava_upload=RuntimeError("all connection attempts failed"),
    )

    assert "all connection attempts failed" in errors["strava"]
    assert "garmin" not in errors
    assert counts["garmin"] == 1
    from eufy_sync.sync import MAX_RETRIES
    assert fake_strava.update_weight.call_count == MAX_RETRIES

    state.close()


def test_other_source_same_day_entry_is_still_skipped(tmp_path: Path):
    """When Garmin already has an entry for the date but WE never synced
    anything for that local date ourselves (e.g. another source/device
    uploaded it), the guard still applies and the measurement is recorded as
    skipped."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()

    measurement = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    fake_garmin = _run_garmin_sync(user, state, [measurement], has_weight_on_date_return=True)
    fake_garmin.upload_body_composition.assert_not_called()

    assert state.is_synced(user.name, measurement.measurement_id, "garmin")
    cursor = state._conn.execute(
        "SELECT response FROM sync_log WHERE eufy_measurement_id = ?",
        (measurement.measurement_id,),
    )
    response = cursor.fetchone()[0]
    assert response == '{"skipped": "already_in_garmin"}'

    state.close()




# ---------------------------------------------------------------------------
# Issue #48: state tracking for weight-only (raw Wi-Fi) syncs
# ---------------------------------------------------------------------------

def test_weight_only_sync_is_tracked_and_found_by_date(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    ts = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)

    state.record_sync(
        "user1", "cust_100", ts.isoformat(), 85.0,
        "2026-07-09T07:01:00+00:00", target="garmin", weight_only=True,
    )

    rows = state.weight_only_syncs_on_date("user1", "garmin", ts.astimezone().date())
    assert len(rows) == 1
    assert rows[0]["measurement_id"] == "cust_100"
    assert rows[0]["weight_kg"] == 85.0
    assert datetime.fromisoformat(rows[0]["measurement_timestamp"]) == ts

    # Other dates, targets, and users see nothing
    assert state.weight_only_syncs_on_date("user1", "garmin", ts.date() + timedelta(days=1)) == []
    assert state.weight_only_syncs_on_date("user1", "strava", ts.astimezone().date()) == []
    assert state.weight_only_syncs_on_date("roommate", "garmin", ts.astimezone().date()) == []

    state.close()


def test_full_sync_is_not_upgradable(tmp_path: Path):
    """Default record_sync (a processed record) must never be offered for upgrade."""
    state = SyncState(tmp_path / "test.db")
    ts = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
    state.record_sync("user1", "cust_100", ts.isoformat(), 85.0,
                      "2026-07-09T07:01:00+00:00", target="garmin")

    assert state.weight_only_syncs_on_date("user1", "garmin", ts.astimezone().date()) == []
    state.close()


def test_mark_upgraded_clears_weight_only(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    ts = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
    state.record_sync("user1", "cust_100", ts.isoformat(), 85.0,
                      "2026-07-09T07:01:00+00:00", target="garmin", weight_only=True)

    state.mark_upgraded("user1", "cust_100", "garmin")

    assert state.weight_only_syncs_on_date("user1", "garmin", ts.astimezone().date()) == []
    # The sync itself is still recorded (no re-upload of the raw record)
    assert state.is_synced("user1", "cust_100", "garmin")
    state.close()


def test_weight_only_column_migration(tmp_path: Path):
    """A database created before the weight_only column must open cleanly,
    with existing rows treated as full records (they predate the raw
    fallback, so none of them can be weight-only)."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE sync_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_name TEXT NOT NULL,
            eufy_measurement_id TEXT NOT NULL,
            measurement_timestamp TEXT NOT NULL,
            weight_kg REAL,
            target TEXT NOT NULL DEFAULT 'garmin',
            synced_at TEXT NOT NULL,
            response TEXT,
            UNIQUE(user_name, eufy_measurement_id, target)
        );
        INSERT INTO sync_log (user_name, eufy_measurement_id, measurement_timestamp,
                              weight_kg, target, synced_at)
        VALUES ('user1', 'cust_100', '2026-07-09T07:00:00+00:00', 85.0,
                'garmin', '2026-07-09T07:01:00+00:00');
    """)
    conn.commit()
    conn.close()

    state = SyncState(db_path)
    assert state.is_synced("user1", "cust_100", "garmin")
    old_date = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc).astimezone().date()
    assert state.weight_only_syncs_on_date("user1", "garmin", old_date) == []
    # New writes with the flag work after migration
    state.record_sync("user1", "cust_200", "2026-07-10T07:00:00+00:00", 84.8,
                      "2026-07-10T07:01:00+00:00", target="garmin", weight_only=True)
    new_date = datetime(2026, 7, 10, 7, 0, tzinfo=timezone.utc).astimezone().date()
    assert len(state.weight_only_syncs_on_date("user1", "garmin", new_date)) == 1
    state.close()


def _raw_measurement(weight_kg: float, dt: datetime) -> EufyMeasurement:
    return EufyMeasurement(
        measurement_id=f"cust_{int(dt.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=dt,
        weight_kg=weight_kg,
        weight_only=True,
    )


def _full_measurement(weight_kg: float, dt: datetime, measurement_id: str | None = None) -> EufyMeasurement:
    return EufyMeasurement(
        measurement_id=measurement_id or f"cust_{int(dt.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=dt,
        weight_kg=weight_kg,
        body_fat_pct=18.5,
        muscle_mass_kg=45.0,
    )


def test_full_record_with_same_id_upgrades_weight_only_sync(tmp_path: Path):
    """Issue #48, blocked case: when the raw and processed record share a
    measurement id, the processed record used to be skipped as already
    synced, stranding the user on weight-only forever. It must instead
    replace the weight-only entry in Garmin."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    dt = datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)

    raw = _raw_measurement(85.0, dt)
    fake_garmin_1 = _run_garmin_sync(user, state, [raw], has_weight_on_date_return=False)
    fake_garmin_1.upload_body_composition.assert_called_once()

    full = _full_measurement(85.0, dt)  # same id: same customer + timestamp
    assert full.measurement_id == raw.measurement_id
    fake_garmin_2 = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True)

    fake_garmin_2.delete_weight_entry.assert_called_once()
    del_dt, del_weight = fake_garmin_2.delete_weight_entry.call_args.args
    assert del_dt == dt
    assert del_weight == 85.0
    fake_garmin_2.upload_body_composition.assert_called_once()

    # The weigh-in is no longer upgradable; a third run does nothing.
    assert state.weight_only_syncs_on_date(user.name, "garmin", dt.astimezone().date()) == []
    fake_garmin_3 = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True)
    fake_garmin_3.upload_body_composition.assert_not_called()
    fake_garmin_3.delete_weight_entry.assert_not_called()

    state.close()


def test_full_record_with_different_id_replaces_instead_of_duplicating(tmp_path: Path):
    """Issue #48, duplicate case: when the processed record carries a
    different timestamp (hence id), it used to upload alongside the raw one,
    giving Garmin a same-day duplicate. It must delete the weight-only entry
    first."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    weigh_in = datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)
    # Both timestamps describe the weigh-in, with a small recording delay.
    # A time hours later no longer counts as evidence of the same reading.
    processed = weigh_in + timedelta(seconds=30)

    raw = _raw_measurement(85.0, weigh_in)
    _run_garmin_sync(user, state, [raw], has_weight_on_date_return=False)

    full = _full_measurement(85.0, processed)
    assert full.measurement_id != raw.measurement_id
    fake_garmin_2 = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True)

    fake_garmin_2.delete_weight_entry.assert_called_once()
    del_dt, del_weight = fake_garmin_2.delete_weight_entry.call_args.args
    assert del_dt == weigh_in  # deletes the raw upload, matched by its own timestamp
    assert del_weight == 85.0
    fake_garmin_2.upload_body_composition.assert_called_once()

    # Both ids are recorded; nothing left to upgrade.
    assert state.is_synced(user.name, raw.measurement_id, "garmin")
    assert state.is_synced(user.name, full.measurement_id, "garmin")
    assert state.weight_only_syncs_on_date(user.name, "garmin", weigh_in.astimezone().date()) == []

    state.close()


@pytest.mark.parametrize("seconds,weight", [(12 * 3600, 85.0), (30, 88.0)])
def test_distinct_full_reading_preserves_same_day_raw_reading(tmp_path: Path, seconds, weight):
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    morning = datetime(2026, 5, 10, 8).astimezone()
    raw = _raw_measurement(85.0, morning)
    _run_garmin_sync(user, state, [raw], has_weight_on_date_return=False)

    full = _full_measurement(weight, morning + timedelta(seconds=seconds))
    garmin = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True)

    garmin.delete_weight_entry.assert_not_called()
    garmin.upload_body_composition.assert_called_once()
    assert state.is_synced(user.name, full.measurement_id, "garmin")
    remaining = state.weight_only_syncs_on_date(user.name, "garmin", morning.date())
    assert [r["measurement_id"] for r in remaining] == [raw.measurement_id]
    state.close()


def test_ambiguous_raw_matches_are_preserved(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    dt = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    raws = [_raw_measurement(85.0, dt + timedelta(seconds=s)) for s in (0, 60)]
    _run_garmin_sync(user, state, raws, has_weight_on_date_return=False)

    full = _full_measurement(85.0, dt + timedelta(seconds=30))
    garmin = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True)

    garmin.delete_weight_entry.assert_not_called()
    garmin.upload_body_composition.assert_called_once()
    assert len(state.weight_only_syncs_on_date(user.name, "garmin", dt.astimezone().date())) == 2
    state.close()


def test_failed_upgrade_survives_restart_and_eufy_outage(tmp_path: Path):
    from eufy_sync.sync import PermanentSyncError

    db_path = tmp_path / "test.db"
    state = SyncState(db_path)
    user = _garmin_user()
    dt = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    raw = _raw_measurement(85.0, dt)
    full = _full_measurement(85.0, dt)
    _run_garmin_sync(user, state, [raw], has_weight_on_date_return=False)
    source = MagicMock()
    source.fetch_measurements.return_value = [full]
    garmin = MagicMock()
    garmin.upload_body_composition.side_effect = PermanentSyncError("upload rejected")

    def delete_only_after_committing_recovery(*args):
        with sqlite3.connect(db_path) as observer:
            assert observer.execute("SELECT COUNT(*) FROM pending_upgrades").fetchone()[0] == 1
        return True

    garmin.delete_weight_entry.side_effect = delete_only_after_committing_recovery
    with patch("eufy_sync.sync.EufyClient", return_value=source), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.sync.time.sleep"):
        _, errors = sync_user(user, state)
    assert errors == {"garmin": "upload rejected"}
    garmin.delete_weight_entry.assert_called_once()
    state.close()

    state = SyncState(db_path)
    assert len(state.get_pending_upgrades(user.name)) == 1
    source.authenticate.side_effect = RuntimeError("network is unreachable")
    garmin.upload_body_composition.side_effect = None
    garmin.upload_body_composition.return_value = {"ok": True}
    garmin.reset_mock()
    user.strava = StravaConfig(client_id="fake", client_secret="fake")
    strava = MagicMock()
    with patch("eufy_sync.sync.EufyClient", return_value=source), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state)
    assert counts == {"garmin": 1, "strava": 0}
    strava.update_weight.assert_not_called()
    assert errors == {"eufy": "network is unreachable"}
    assert garmin.upload_body_composition.call_args.args[0].percent_fat == 18.5
    assert state.get_pending_upgrades(user.name) == []
    assert state.weight_only_syncs_on_date(user.name, "garmin", dt.astimezone().date()) == []
    state.close()


def test_uploaded_replacement_finishes_bookkeeping_after_a_crash(tmp_path: Path):
    from dataclasses import asdict

    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    dt = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    raw = _raw_measurement(85.0, dt)
    full = _full_measurement(85.0, dt + timedelta(seconds=30))
    state.record_sync(user.name, raw.measurement_id, raw.timestamp.isoformat(),
                      raw.weight_kg, dt.isoformat(), target="garmin", weight_only=True)
    payload = asdict(full)
    payload["timestamp"] = full.timestamp.isoformat()
    state.save_pending_upgrade(user.name, raw.measurement_id, payload)
    state.record_sync(user.name, full.measurement_id, full.timestamp.isoformat(),
                      full.weight_kg, dt.isoformat(), target="garmin")

    garmin = _run_garmin_sync(user, state, [], has_weight_on_date_return=True)

    garmin.delete_weight_entry.assert_not_called()
    garmin.upload_body_composition.assert_not_called()
    assert state.get_pending_upgrades(user.name) == []
    assert state.get_oldest_weight_only_timestamp(user.name, "garmin") is None
    state.close()


def test_second_full_record_same_day_does_not_delete(tmp_path: Path):
    """Regression: a corrected re-weigh (two FULL records the same day) must
    keep today's behavior - upload the second one, delete nothing."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()

    first = _full_measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    second = _full_measurement(84.7, datetime(2026, 5, 10, 8, 30, tzinfo=timezone.utc))

    _run_garmin_sync(user, state, [first], has_weight_on_date_return=False)
    fake_garmin_2 = _run_garmin_sync(user, state, [second], has_weight_on_date_return=True)

    fake_garmin_2.upload_body_composition.assert_called_once()
    fake_garmin_2.delete_weight_entry.assert_not_called()

    state.close()


def test_automatic_sync_revisits_older_weight_only_readings(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    old_dt = datetime(2026, 5, 10, 12, tzinfo=timezone.utc)
    old_raw = _raw_measurement(85.0, old_dt)
    newest = _full_measurement(84.0, old_dt + timedelta(days=3))
    _run_garmin_sync(user, state, [old_raw, newest], has_weight_on_date_return=False)
    old_full = _full_measurement(85.0, old_dt)
    source = MagicMock()
    source.fetch_measurements.side_effect = lambda after_timestamp: [
        m for m in (old_full, newest) if m.timestamp.timestamp() >= after_timestamp
    ]
    garmin = MagicMock()
    garmin.upload_body_composition.return_value = {"ok": True}
    with patch("eufy_sync.sync.EufyClient", return_value=source), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state)
    assert errors == {}
    assert counts == {"garmin": 1}
    assert state.get_oldest_weight_only_timestamp(user.name, "garmin") is None
    assert state.get_latest_sync_timestamp(user.name, "garmin") == int(newest.timestamp.timestamp())
    state.close()


@pytest.mark.parametrize("mode,include_current", [
    ({"backfill_days": 30}, True),
    ({"backfill_days": 30}, False),
    ({"repair_days": 30}, False),
])
def test_strava_never_rolls_back_to_missing_history(tmp_path: Path, mode, include_current):
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()
    older = _measurement(85.0, datetime(2026, 5, 10, 12, tzinfo=timezone.utc))
    current = _measurement(80.0, datetime(2026, 5, 20, 12, tzinfo=timezone.utc))
    state.record_sync(user.name, current.measurement_id, current.timestamp.isoformat(),
                      current.weight_kg, current.timestamp.isoformat(), target="strava")
    source = MagicMock()
    source.fetch_measurements.return_value = [older, current] if include_current else [older]
    garmin = MagicMock()
    garmin.has_weight_on_date.return_value = False
    garmin.upload_body_composition.return_value = {"ok": True}
    strava = MagicMock()
    strava.update_weight.return_value = {"ok": True}

    with patch("eufy_sync.sync.EufyClient", return_value=source), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, **mode)

    assert errors == {}
    strava.update_weight.assert_not_called()
    assert not state.is_synced(user.name, older.measurement_id, "strava")
    assert state.is_synced(user.name, older.measurement_id, "garmin")
    assert counts["garmin"] == len(source.fetch_measurements.return_value)
    state.close()


def test_strava_advances_after_skipping_older_history(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()
    older = _measurement(85.0, datetime(2026, 5, 10, 12, tzinfo=timezone.utc))
    current = _measurement(80.0, datetime(2026, 5, 20, 12, tzinfo=timezone.utc))
    newest = _measurement(79.0, datetime(2026, 5, 21, 12, tzinfo=timezone.utc))
    state.record_sync(user.name, current.measurement_id, current.timestamp.isoformat(),
                      current.weight_kg, current.timestamp.isoformat(), target="strava")

    counts, errors, _, strava = _run_dual_sync(user, state, [newest, older, current])

    assert errors == {}
    strava.update_weight.assert_called_once_with(79.0)
    assert counts["strava"] == 1
    assert state.is_synced(user.name, newest.measurement_id, "strava")
    state.close()


def test_skipped_raw_record_is_not_marked_upgradable(tmp_path: Path):
    """A raw record skipped by the other-source guard was never uploaded by
    us, so a later full record must not delete the other source's entry."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    dt = datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)

    # Garmin already has data from another source; the raw record is skipped.
    raw = _raw_measurement(85.0, dt)
    fake_garmin_1 = _run_garmin_sync(user, state, [raw], has_weight_on_date_return=True)
    fake_garmin_1.upload_body_composition.assert_not_called()

    assert state.weight_only_syncs_on_date(user.name, "garmin", dt.astimezone().date()) == []

    full = _full_measurement(85.0, datetime(2026, 5, 10, 9, 30, tzinfo=timezone.utc))
    fake_garmin_2 = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True)
    fake_garmin_2.delete_weight_entry.assert_not_called()

    state.close()


# ---------------------------------------------------------------------------
# Per-target fetch cursor: one target's progress must not hide measurements
# another target missed while its auth was down
# ---------------------------------------------------------------------------


def test_recovered_target_refetches_outage_window(tmp_path: Path):
    """Garmin was down (auth failing) while Strava kept syncing and advancing
    its cursor. Once Garmin recovers, the fetch must reach back to GARMIN's
    own cursor, so the outage-window measurement still lands in Garmin, while
    Strava skips it as already synced."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    # Before the outage both targets synced m1. During the outage only
    # Strava synced m2.
    state.record_sync("default", "m1", "2026-05-01T08:00:00+00:00", 85.0, "2026-05-01T08:01:00+00:00", target="garmin")
    state.record_sync("default", "m1", "2026-05-01T08:00:00+00:00", 85.0, "2026-05-01T08:01:00+00:00", target="strava")
    outage = _measurement(84.5, datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc))
    state.record_sync("default", outage.measurement_id, outage.timestamp.isoformat(), 84.5, "2026-05-05T08:01:00+00:00", target="strava")

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = [outage]
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.has_weight_on_date.return_value = False
    fake_garmin.upload_body_composition.return_value = {"ok": True}
    fake_garmin.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.return_value = None
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state)

    # The fetch reached back to Garmin's cursor (m1), not Strava's (m2).
    garmin_cursor = int(datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc).timestamp())
    fake_eufy.fetch_measurements.assert_called_once_with(after_timestamp=garmin_cursor)

    # Garmin received the outage-window measurement; Strava skipped it.
    assert counts["garmin"] == 1
    assert counts["strava"] == 0
    fake_garmin.upload_body_composition.assert_called_once()
    fake_strava.update_weight.assert_not_called()
    assert errors == {}

    state.close()


def test_newly_added_target_still_backfills_seven_days(tmp_path: Path):
    """A target with no syncs yet pulls the fetch window back to 7 days even
    when the other target's cursor is current."""
    import time as time_module

    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    # Garmin synced moments ago; Strava is brand new (no rows).
    now = datetime.now(timezone.utc)
    state.record_sync("default", "m1", now.isoformat(), 85.0, now.isoformat(), target="garmin")

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = []
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.return_value = None
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        sync_user(user, state)

    after = fake_eufy.fetch_measurements.call_args.kwargs["after_timestamp"]
    seven_days_ago = int(time_module.time()) - 7 * 86400
    assert abs(after - seven_days_ago) < 60  # 7-day backfill, not garmin's fresh cursor

    state.close()


def test_latest_sync_timestamp_filters_by_target(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00", target="garmin")
    state.record_sync("user1", "m2", "2024-04-02T08:00:00+00:00", 85.9, "2024-04-02T08:01:00+00:00", target="strava")

    garmin_ts = state.get_latest_sync_timestamp("user1", "garmin")
    strava_ts = state.get_latest_sync_timestamp("user1", "strava")
    assert garmin_ts == int(datetime(2024, 4, 1, 12, 0, tzinfo=timezone.utc).timestamp())
    assert strava_ts == int(datetime(2024, 4, 2, 8, 0, tzinfo=timezone.utc).timestamp())
    assert state.get_latest_sync_timestamp("user1") == strava_ts  # no target: global max
    assert state.get_latest_sync_timestamp("user1", "zwift") is None

    state.close()


# ---------------------------------------------------------------------------
# Same-date guard: a skipped-because-already-in-Garmin row must not disable
# the guard for a second measurement on the same day
# ---------------------------------------------------------------------------


def test_has_synced_on_date_ignores_skipped_rows(tmp_path: Path):
    from datetime import date

    from eufy_sync.state import SKIPPED_IN_GARMIN_RESPONSE

    state = SyncState(tmp_path / "test.db")
    d = date(2026, 5, 10)

    state.record_sync("user1", "m1", "2026-05-10T08:00:00+00:00", 85.0, "2026-05-10T08:01:00+00:00", target="garmin", response=SKIPPED_IN_GARMIN_RESPONSE)
    assert not state.has_synced_on_date("user1", "garmin", d)

    state.record_sync("user1", "m2", "2026-05-10T09:00:00+00:00", 84.8, "2026-05-10T09:01:00+00:00", target="garmin", response='{"ok": true}')
    assert state.has_synced_on_date("user1", "garmin", d)

    state.close()


def _garmin_rows(state, measurement_id: str) -> int:
    cursor = state._conn.execute(
        "SELECT COUNT(*) FROM sync_log WHERE eufy_measurement_id = ? AND target = ?",
        (measurement_id, "garmin"),
    )
    return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# Issue #58: --repair-days re-syncs a window regardless of recorded state, for
# when the target lost data our state still calls synced
# ---------------------------------------------------------------------------


def test_repair_reuploads_already_synced_measurement(tmp_path: Path):
    """The point of repair mode: state says synced, the entry is gone from
    Garmin, so it must upload again. The existing state row must not gain a
    duplicate (the UNIQUE constraint would raise)."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    m = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    _run_garmin_sync(user, state, [m], has_weight_on_date_return=False)
    assert state.is_synced(user.name, m.measurement_id, "garmin")

    # The user deleted the entry in Garmin; a normal run would skip it.
    normal = _run_garmin_sync(user, state, [m], has_weight_on_date_return=False)
    normal.upload_body_composition.assert_not_called()

    repair = _run_garmin_sync(user, state, [m], has_weight_on_date_return=False, repair_days=7)
    repair.upload_body_composition.assert_called_once()
    assert _garmin_rows(state, m.measurement_id) == 1

    state.close()


def test_repair_uses_its_own_fetch_window(tmp_path: Path):
    import time as time_module

    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = []
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.sync.time.sleep"):
        sync_user(user, state, repair_days=30)

    after = fake_eufy.fetch_measurements.call_args.kwargs["after_timestamp"]
    assert abs(after - (int(time_module.time()) - 30 * 86400)) < 60

    state.close()


def test_repair_still_upgrades_a_weight_only_row(tmp_path: Path):
    """Repair must not flatten the issue #48 upgrade: a full record arriving
    for a weigh-in we synced weight-only still replaces the Garmin entry
    rather than uploading a second one beside it."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    dt = datetime(2026, 5, 10, 7, 0, tzinfo=timezone.utc)

    raw = _raw_measurement(85.0, dt)
    _run_garmin_sync(user, state, [raw], has_weight_on_date_return=False)

    full = _full_measurement(85.0, dt)  # same id: same customer + timestamp
    assert full.measurement_id == raw.measurement_id
    repair = _run_garmin_sync(user, state, [full], has_weight_on_date_return=True, repair_days=7)

    repair.delete_weight_entry.assert_called_once()
    repair.upload_body_composition.assert_called_once()
    assert state.weight_only_syncs_on_date(user.name, "garmin", dt.astimezone().date()) == []

    state.close()


def test_repair_sends_only_the_newest_measurement_to_strava(tmp_path: Path):
    """Strava holds a single current weight, so repeating history there would
    spend the rate limit to land the same value. Garmin gets the whole
    window."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_and_strava_user()

    older = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    middle = _measurement(85.5, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc))
    newest = _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc))

    for m in (older, middle, newest):
        for target in ("garmin", "strava"):
            state.record_sync("default", m.measurement_id, m.timestamp.isoformat(),
                              m.weight_kg, "2026-05-12T09:00:00+00:00", target=target)

    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = [newest, middle, older]  # newest-first, as Eufy returns
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.has_weight_on_date.return_value = False
    fake_garmin.upload_body_composition.return_value = {"ok": True}
    fake_garmin.close.return_value = None

    fake_strava = MagicMock()
    fake_strava.authenticate.return_value = None
    fake_strava.update_weight.return_value = {"weight": None}
    fake_strava.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, repair_days=30)

    assert errors == {}
    assert counts["garmin"] == 3
    assert counts["strava"] == 1
    assert [c.args[0] for c in fake_strava.update_weight.call_args_list] == [86.0]

    state.close()


def test_repair_still_skips_a_foreign_same_date_entry(tmp_path: Path):
    """The same-date guard protects entries another source put in Garmin. We
    never uploaded this date ourselves, so repair must leave it alone."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    m = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    fake_garmin = _run_garmin_sync(user, state, [m], has_weight_on_date_return=True, repair_days=7)

    fake_garmin.upload_body_composition.assert_not_called()
    cursor = state._conn.execute(
        "SELECT response FROM sync_log WHERE eufy_measurement_id = ?",
        (m.measurement_id,),
    )
    assert cursor.fetchone()[0] == '{"skipped": "already_in_garmin"}'

    state.close()


def test_repair_over_an_already_skipped_date_does_not_double_record(tmp_path: Path):
    """A skipped-because-already-in-Garmin row does not count as "we uploaded
    on this date", so repair reaches the guard again for the same measurement.
    Recording that skip a second time would break the UNIQUE constraint."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    m = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    _run_garmin_sync(user, state, [m], has_weight_on_date_return=True)
    assert state.is_synced(user.name, m.measurement_id, "garmin")

    fake_garmin = _run_garmin_sync(user, state, [m], has_weight_on_date_return=True, repair_days=7)

    fake_garmin.upload_body_composition.assert_not_called()
    assert _garmin_rows(state, m.measurement_id) == 1

    state.close()


def test_repair_dry_run_previews_without_uploading(tmp_path: Path, capsys):
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()
    m = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))

    _run_garmin_sync(user, state, [m], has_weight_on_date_return=False)
    capsys.readouterr()

    fake_garmin = _run_garmin_sync(
        user, state, [m], has_weight_on_date_return=False, repair_days=7, dry_run=True,
    )

    fake_garmin.upload_body_composition.assert_not_called()
    assert capsys.readouterr().out.count("[DRY RUN] Would sync") == 1
    assert _garmin_rows(state, m.measurement_id) == 1

    state.close()


def test_second_same_day_measurement_is_also_skipped(tmp_path: Path):
    """Garmin holds an external entry for the date and one run carries two
    Eufy measurements for that same day. The first records a skip; that skip
    must not flip the guard off and let the second upload a duplicate."""
    state = SyncState(tmp_path / "test.db")
    user = _garmin_user()

    first = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    second = _measurement(84.8, datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc))

    fake_garmin = _run_garmin_sync(user, state, [first, second], has_weight_on_date_return=True)

    fake_garmin.upload_body_composition.assert_not_called()
    assert state.is_synced(user.name, first.measurement_id, "garmin")
    assert state.is_synced(user.name, second.measurement_id, "garmin")

    state.close()
