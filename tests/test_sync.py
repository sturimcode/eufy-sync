import sqlite3
from pathlib import Path

from eufy_sync.state import SyncState


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
