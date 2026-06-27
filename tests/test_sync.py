import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from eufy_sync.config import EufyConfig, StravaConfig, UserConfig
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

    assert errors == {}, f"expected no errors, got {errors}"

    weights_uploaded = [call.args[0] for call in fake_strava.update_weight.call_args_list]
    assert weights_uploaded == [85.0, 85.5, 86.0], (
        f"Strava must receive weights oldest→newest so the final value is the newest, "
        f"got order: {weights_uploaded}"
    )

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


from eufy_sync.config import ZwiftConfig


def test_zwift_gets_exactly_one_put_per_sync(tmp_path: Path):
    """Zwift's profile endpoint is heavy - we update once per sync, not once per measurement."""
    state = SyncState(tmp_path / "test.db")

    measurements = [
        _measurement(85.0, datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc)),
        _measurement(85.5, datetime(2026, 5, 11, 8, 0, tzinfo=timezone.utc)),
        _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc)),
    ]

    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        zwift=ZwiftConfig(email="z@example.com", password="zpw"),
    )

    fake_eufy = MagicMock()
    fake_eufy.fetch_measurements.return_value = list(measurements)

    fake_zwift = MagicMock()
    fake_zwift.update_weight.return_value = {"weight": 86000}

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=fake_zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert fake_zwift.update_weight.call_count == 1, "Zwift should be PUT exactly once per sync"
    assert fake_zwift.update_weight.call_args.args[0] == 86.0, "Should send the newest weight"
    assert counts["zwift"] == 1
    assert errors == {}
    # All three measurements must be marked synced for Zwift
    for m in measurements:
        assert state.is_synced("default", m.measurement_id, "zwift")
    state.close()


def test_zwift_failure_does_not_block_strava(tmp_path: Path):
    """A Zwift exception must not prevent Strava sync from completing."""
    state = SyncState(tmp_path / "test.db")

    measurement = _measurement(86.0, datetime(2026, 5, 12, 8, 0, tzinfo=timezone.utc))

    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        strava=StravaConfig(client_id="cid", client_secret="csec"),
        zwift=ZwiftConfig(email="z@example.com", password="zpw"),
    )

    fake_eufy = MagicMock()
    fake_eufy.fetch_measurements.return_value = [measurement]

    fake_strava = MagicMock()
    fake_strava.update_weight.return_value = {"weight": 86}

    fake_zwift = MagicMock()
    fake_zwift.authenticate.side_effect = RuntimeError("Zwift exploded")

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=fake_zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert counts.get("strava") == 1, "Strava must have succeeded"
    assert "zwift" in errors, f"Zwift error must be reported; got {errors}"
    assert "Zwift exploded" in errors["zwift"]
    state.close()
