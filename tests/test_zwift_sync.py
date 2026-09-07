from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.config import EufyConfig, GarminConfig, StravaConfig, UserConfig, ZwiftConfig
from eufy_sync.eufy_client import EufyMeasurement
from eufy_sync.state import SyncState
from eufy_sync.sync import PermanentSyncError, sync_user


def _measurement(weight: float, timestamp: datetime, *, weight_only: bool = False) -> EufyMeasurement:
    return EufyMeasurement(
        measurement_id=f"cust_{int(timestamp.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=timestamp,
        weight_kg=weight,
        weight_only=weight_only,
    )


def _user(*, garmin: bool = False, strava: bool = False) -> UserConfig:
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw") if garmin else None,
        strava=StravaConfig(client_id="cid", client_secret="secret") if strava else None,
        zwift=ZwiftConfig(email="z@example.com", password="pw"),
    )


def _source(measurements):
    source = MagicMock()
    source.fetch_measurements.return_value = measurements
    return source


def test_zwift_receives_only_latest_valid_fresh_weight(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user()
    older = _measurement(85.0, datetime(2026, 5, 10, tzinfo=timezone.utc))
    latest = _measurement(84.5, older.timestamp + timedelta(days=1), weight_only=True)
    invalid = _measurement(0.0, latest.timestamp + timedelta(days=1))
    source = _source([invalid, latest, older])
    zwift = MagicMock()
    zwift.update_weight.return_value = {"verified": True, "changed": True, "weight_grams": 84500}

    with patch("eufy_sync.sync.EufyClient", return_value=source), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert errors == {}
    assert counts == {"zwift": 1}
    zwift.update_weight.assert_called_once_with(84.5)
    assert state.is_synced(user.name, latest.measurement_id, "zwift")
    assert not state.is_synced(user.name, older.measurement_id, "zwift")
    zwift.close.assert_called_once()
    state.close()


def test_zwift_backfill_never_rolls_back_current_weight(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user()
    older = _measurement(85.0, datetime(2026, 5, 10, tzinfo=timezone.utc))
    current = _measurement(80.0, older.timestamp + timedelta(days=10))
    state.record_sync(user.name, current.measurement_id, current.timestamp.isoformat(), 80.0,
                      current.timestamp.isoformat(), target="zwift")
    zwift = MagicMock()

    with patch("eufy_sync.sync.EufyClient", return_value=_source([older])), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=30)

    assert errors == {}
    assert counts == {"zwift": 0}
    zwift.update_weight.assert_not_called()
    assert state.get_latest_sync_timestamp(user.name, "zwift") == int(current.timestamp.timestamp())
    state.close()


def test_pending_garmin_recovery_is_never_sent_to_zwift(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user(garmin=True)
    pending = _measurement(85.0, datetime(2026, 5, 10, tzinfo=timezone.utc))
    payload = asdict(pending)
    payload["timestamp"] = pending.timestamp.isoformat()
    state.save_pending_upgrade(user.name, "raw-id", payload)
    garmin = MagicMock()
    garmin.has_weight_on_date.return_value = False
    garmin.upload_body_composition.return_value = {"ok": True}
    zwift = MagicMock()

    with patch("eufy_sync.sync.EufyClient", return_value=_source([])), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state)

    assert errors == {}
    assert counts == {"garmin": 1, "zwift": 0}
    zwift.update_weight.assert_not_called()
    assert not state.is_synced(user.name, pending.measurement_id, "zwift")
    state.close()


def test_same_id_pending_collision_uses_fresh_reading_for_current_weight_targets(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user(garmin=True, strava=True)
    timestamp = datetime(2026, 5, 10, tzinfo=timezone.utc)
    saved_full = EufyMeasurement(
        measurement_id=f"cust_{int(timestamp.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=timestamp,
        weight_kg=85.0,
        body_fat_pct=18.5,
        muscle_mass_kg=45.0,
    )
    fresh_raw = _measurement(83.0, timestamp, weight_only=True)
    assert fresh_raw.measurement_id == saved_full.measurement_id

    state.record_sync(
        user.name, "raw-id", timestamp.isoformat(), saved_full.weight_kg,
        timestamp.isoformat(), target="garmin", weight_only=True,
    )
    payload = asdict(saved_full)
    payload["timestamp"] = saved_full.timestamp.isoformat()
    state.save_pending_upgrade(user.name, "raw-id", payload)

    garmin = MagicMock()
    garmin.upload_body_composition.return_value = {"ok": True}
    strava = MagicMock()
    strava.update_weight.return_value = {"ok": True}
    zwift = MagicMock()
    zwift.update_weight.return_value = {"verified": True, "changed": True, "weight_grams": 83000}

    with patch("eufy_sync.sync.EufyClient", return_value=_source([fresh_raw])), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=strava), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state)

    assert errors == {}
    assert counts == {"garmin": 1, "strava": 1, "zwift": 1}
    uploaded = garmin.upload_body_composition.call_args.args[0]
    assert uploaded.weight == 85.0
    assert uploaded.percent_fat == 18.5
    strava.update_weight.assert_called_once_with(83.0)
    zwift.update_weight.assert_called_once_with(83.0)

    rows = state._conn.execute(
        "SELECT target, weight_kg, measurement_timestamp, weight_only "
        "FROM sync_log WHERE eufy_measurement_id = ? AND target IN (?, ?) ORDER BY target",
        (fresh_raw.measurement_id, "strava", "zwift"),
    ).fetchall()
    assert rows == [
        ("strava", 83.0, fresh_raw.timestamp.isoformat(), 1),
        ("zwift", 83.0, fresh_raw.timestamp.isoformat(), 1),
    ]
    state.close()


def test_zwift_failure_is_isolated_and_not_recorded(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user(garmin=True, strava=True)
    measurement = _measurement(84.5, datetime(2026, 5, 10, tzinfo=timezone.utc))
    garmin = MagicMock()
    garmin.has_weight_on_date.return_value = False
    garmin.upload_body_composition.return_value = {"ok": True}
    strava = MagicMock()
    strava.update_weight.return_value = {"ok": True}
    zwift = MagicMock()
    zwift.update_weight.side_effect = PermanentSyncError("Zwift read-back verification failed")

    with patch("eufy_sync.sync.EufyClient", return_value=_source([measurement])), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=strava), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7)

    assert counts == {"garmin": 1, "strava": 1, "zwift": 0}
    assert errors == {"zwift": "Zwift read-back verification failed"}
    assert state.is_synced(user.name, measurement.measurement_id, "garmin")
    assert state.is_synced(user.name, measurement.measurement_id, "strava")
    assert not state.is_synced(user.name, measurement.measurement_id, "zwift")
    state.close()


def test_zwift_dry_run_writes_neither_remote_nor_state(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user()
    measurement = _measurement(84.5, datetime(2026, 5, 10, tzinfo=timezone.utc))
    zwift = MagicMock()

    with patch("eufy_sync.sync.EufyClient", return_value=_source([measurement])), \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift):
        counts, errors = sync_user(user, state, backfill_days=7, dry_run=True)

    assert errors == {}
    assert counts == {"zwift": 1}
    zwift.update_weight.assert_not_called()
    assert not state.is_synced(user.name, measurement.measurement_id, "zwift")
    state.close()


def test_target_zwift_never_constructs_or_authenticates_other_services(tmp_path: Path):
    state = SyncState(tmp_path / "state.db")
    user = _user(garmin=True, strava=True)
    measurement = _measurement(84.5, datetime(2026, 5, 10, tzinfo=timezone.utc))
    zwift = MagicMock()
    zwift.update_weight.return_value = {"verified": True, "changed": True, "weight_grams": 84500}

    with patch("eufy_sync.sync.EufyClient", return_value=_source([measurement])), \
         patch("eufy_sync.garmin_client.GarminClient") as garmin_class, \
         patch("eufy_sync.strava_client.StravaClient") as strava_class, \
         patch("eufy_sync.zwift_client.ZwiftClient", return_value=zwift), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, backfill_days=7, target="zwift")

    assert errors == {}
    assert counts == {"zwift": 1}
    garmin_class.assert_not_called()
    strava_class.assert_not_called()
    zwift.authenticate.assert_called_once_with()
    zwift.update_weight.assert_called_once_with(84.5)
    assert not state.is_synced(user.name, measurement.measurement_id, "garmin")
    assert not state.is_synced(user.name, measurement.measurement_id, "strava")
    state.close()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ("fitbit", "Unknown sync target 'fitbit'. Choose garmin, strava, or zwift."),
        ("garmin", "Sync target 'garmin' is not configured for user 'default'."),
    ],
)
def test_invalid_target_fails_before_any_client_is_constructed(tmp_path: Path, target: str, message: str):
    state = SyncState(tmp_path / "state.db")
    user = _user()

    with patch("eufy_sync.sync.EufyClient") as eufy_class, \
         pytest.raises(ValueError, match=message.replace(".", r"\.")):
        sync_user(user, state, target=target)

    eufy_class.assert_not_called()
    state.close()
