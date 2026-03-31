import sqlite3
from pathlib import Path

from src.state import SyncState


def test_state_init_and_roundtrip(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")

    assert not state.is_synced("user1", "m1")

    state.record_sync(
        user_name="user1",
        measurement_id="m1",
        measurement_timestamp="2024-04-01T12:00:00+00:00",
        weight_kg=86.2,
        synced_at="2024-04-01T12:01:00+00:00",
        garmin_response='{"ok": true}',
    )

    assert state.is_synced("user1", "m1")
    assert not state.is_synced("user1", "m2")
    assert not state.is_synced("roommate", "m1")

    state.close()


def test_latest_sync_timestamp(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")

    assert state.get_latest_sync_timestamp("user1") is None

    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00")
    state.record_sync("user1", "m2", "2024-04-02T08:00:00+00:00", 85.9, "2024-04-02T08:01:00+00:00")

    ts = state.get_latest_sync_timestamp("user1")
    assert ts is not None
    assert ts > 0

    state.close()


def test_duplicate_insert_rejected(tmp_path: Path):
    state = SyncState(tmp_path / "test.db")
    state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:01:00+00:00")

    try:
        state.record_sync("user1", "m1", "2024-04-01T12:00:00+00:00", 86.2, "2024-04-01T12:02:00+00:00")
        assert False, "Should have raised"
    except sqlite3.IntegrityError:
        pass

    state.close()
