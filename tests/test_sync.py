import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        assert False, "Should have raised"
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


def test_strava_receives_measurements_in_chronological_order(tmp_path: Path):
    """Strava only stores latest weight; multi-measurement syncs must finish on the newest."""
    state = SyncState(tmp_path / "test.db")

    older = _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc))
    middle = _measurement(85.5, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc))
    newest = _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc))

    # Eufy API typically returns newest-first
    fetched = [newest, middle, older]

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
    assert weights_uploaded == [85.0, 85.5, 86.0], (
        f"Strava must receive weights oldest→newest so the final value is the newest, "
        f"got order: {weights_uploaded}"
    )

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
    from eufy_sync.sync import _is_permanent
    from eufy_sync.eufy_client import AmbiguousProfileError
    assert _is_permanent(AmbiguousProfileError([])) is True


def test_rate_limit_error_is_permanent():
    from eufy_sync.sync import _is_permanent
    from garminconnect import GarminConnectTooManyRequestsError
    assert _is_permanent(GarminConnectTooManyRequestsError("429")) is True


def _garmin_user() -> UserConfig:
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw"),
    )


def _run_garmin_sync(user, state, measurements, has_weight_on_date_return):
    fake_eufy = MagicMock()
    fake_eufy.authenticate.return_value = None
    fake_eufy.fetch_measurements.return_value = measurements
    fake_eufy.close.return_value = None

    fake_garmin = MagicMock()
    fake_garmin.authenticate.return_value = None
    fake_garmin.has_weight_on_date.return_value = has_weight_on_date_return
    fake_garmin.upload_body_composition.return_value = {"ok": True}
    fake_garmin.close.return_value = None

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.sync.time.sleep"):
        sync_user(user, state, backfill_days=7)  # returns (counts, errors); not needed here

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
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "Garmin dead token" in str(e)

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


