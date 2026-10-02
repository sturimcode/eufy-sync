"""Intervals.icu as a per-date history target in sync_user: one wellness
record per local date, the newest weigh-in of a date wins, and failures go
through the same retry queue as Garmin."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.config import EufyConfig, GarminConfig, IntervalsConfig, UserConfig
from eufy_sync.eufy_client import EufyMeasurement
from eufy_sync.state import SyncState
from eufy_sync.sync import PermanentSyncError, RetryNextRunError, sync_user

# Local noon keeps every same-day offset below on one local date.
NOON = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)


def _m(weight_kg: float, days_ago: int, minutes: int = 0, *, body_fat: float | None = 20.0,
       weight_only: bool = False) -> EufyMeasurement:
    dt = NOON - timedelta(days=days_ago) + timedelta(minutes=minutes)
    return EufyMeasurement(
        measurement_id=f"cust_{int(dt.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=dt,
        weight_kg=weight_kg,
        body_fat_pct=None if weight_only else body_fat,
        weight_only=weight_only,
    )


def _user(garmin: bool = False) -> UserConfig:
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw") if garmin else None,
        intervals=IntervalsConfig(athlete_id="i12345", api_key="test-key"),
    )


def _run(user, state, history, *, fail_weights=(), error=None, fetches=None, garmin=None, **kwargs):
    """One run against a fake Eufy that honors the fetch cursor."""
    def fetch(after_timestamp=None):
        if fetches is not None:
            fetches.append(after_timestamp)
        return [m for m in history if after_timestamp is None or m.timestamp.timestamp() >= after_timestamp]

    eufy = MagicMock()
    eufy.fetch_measurements.side_effect = fetch

    def update(day, weight_kg, body_fat_pct=None):
        if round(weight_kg, 2) in fail_weights:
            raise error or RuntimeError("Temporary Intervals.icu failure during wellness update (HTTP 503)")
        sent = {"date": day.isoformat(), "weight": round(weight_kg, 2)}
        if body_fat_pct is not None:
            sent["bodyFat"] = round(body_fat_pct, 1)
        return sent

    intervals = MagicMock()
    intervals.update_wellness.side_effect = update
    if garmin is None:
        garmin = MagicMock()
        garmin.has_weight_on_date.return_value = False
        garmin.upload_body_composition.return_value = {"ok": True}

    with patch("eufy_sync.sync.EufyClient", return_value=eufy), \
         patch("eufy_sync.intervals_client.IntervalsClient", return_value=intervals) as cls, \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, headless=True, **kwargs)
    return counts, errors, intervals, cls


def _sent(intervals) -> list[tuple[str, float, float | None]]:
    return [
        (c.args[0].isoformat(), round(c.args[1], 2), c.args[2])
        for c in intervals.update_wellness.call_args_list
    ]


def _day(m: EufyMeasurement) -> str:
    return m.timestamp.astimezone().date().isoformat()


def _rows(state):
    return {(r["target"], r["measurement_id"]): r for r in state.get_upload_retries("default")}


def test_each_date_gets_its_newest_weigh_in_with_body_fat(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    morning, evening, next_day = _m(80.0, 3, 0, body_fat=21.0), _m(80.6, 3, 120, body_fat=20.5), _m(79.9, 2)

    counts, errors, intervals, _ = _run(user, state, [next_day, evening, morning], backfill_days=7)

    assert errors == {}
    assert counts == {"intervals": 2}
    assert _sent(intervals) == [(_day(evening), 80.6, 20.5), (_day(next_day), 79.9, 20.0)]
    assert state.is_synced("default", evening.measurement_id, "intervals")
    assert not state.is_synced("default", morning.measurement_id, "intervals")
    intervals.authenticate.assert_called_once_with()
    intervals.close.assert_called_once_with()
    state.close()


def test_a_synced_date_is_not_sent_again(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m = _m(80.0, 2)
    _run(user, state, [m])

    counts, errors, intervals, _ = _run(user, state, [m], backfill_days=7)

    assert errors == {} and counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    state.close()


def test_a_later_weigh_in_on_a_synced_date_replaces_it(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    first = _m(80.0, 2)
    _run(user, state, [first])
    later = _m(80.4, 2, 90)

    counts, _, intervals, _ = _run(user, state, [first, later])

    assert counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(later), 80.4, 20.0)]
    state.close()


def test_backfill_never_overwrites_a_date_with_an_older_weigh_in(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    later = _m(80.4, 2, 90)
    _run(user, state, [later])
    # An earlier reading of the same day turns up only now.
    earlier = _m(81.0, 2)

    counts, errors, intervals, _ = _run(user, state, [earlier], backfill_days=7)

    assert errors == {} and counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    state.close()


def test_invalid_weights_are_never_sent(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    good, bad = _m(80.0, 2), _m(5.0, 1)

    counts, errors, intervals, _ = _run(user, state, [good, bad])

    assert errors == {} and counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(good), 80.0, 20.0)]
    state.close()


def test_processed_record_replaces_a_raw_weight_only_one(tmp_path: Path):
    """Issue #48 for Intervals.icu: the raw weight lands first, the processed
    record (a different id, seconds apart) later adds body fat."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    raw = _m(80.0, 2, 1, weight_only=True)
    _run(user, state, [raw])
    full = _m(80.05, 2, 0, body_fat=19.5)

    counts, errors, intervals, _ = _run(user, state, [raw, full])

    assert errors == {} and counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(full), 80.05, 19.5)]
    assert state.weight_only_syncs_on_date("default", "intervals", full.timestamp.astimezone().date()) == []

    _, _, intervals, _ = _run(user, state, [raw, full])
    intervals.update_wellness.assert_not_called()
    state.close()


def test_processed_record_with_the_same_id_resends_once(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    raw = _m(80.0, 2, weight_only=True)
    _run(user, state, [raw])
    full = _m(80.0, 2, body_fat=19.5)
    assert full.measurement_id == raw.measurement_id

    counts, _, intervals, _ = _run(user, state, [full])
    assert counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(full), 80.0, 19.5)]

    counts, _, intervals, _ = _run(user, state, [full])
    assert counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    state.close()


def test_a_batch_prefers_the_processed_record_over_its_raw_twin(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    full = _m(80.0, 2, 0, body_fat=19.5)
    raw = _m(80.0, 2, 1, weight_only=True)

    counts, _, intervals, _ = _run(user, state, [full, raw])

    assert counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(full), 80.0, 19.5)]
    state.close()


def test_retryable_failure_is_queued_and_replayed_next_run(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m1, m2, m3 = _m(80.0, 3), _m(81.0, 2), _m(82.0, 1)

    counts, errors, intervals, _ = _run(user, state, [m1, m2, m3], fail_weights={81.0})
    assert counts == {"intervals": 1} and "503" in errors["intervals"]
    assert [w for _, w, _ in _sent(intervals)] == [80.0, 81.0, 81.0, 81.0]  # backoff, then stop
    assert _rows(state)[("intervals", m2.measurement_id)]["attempts"] == 1

    counts, errors, intervals, _ = _run(user, state, [m1, m2, m3])
    assert errors == {}
    assert [w for _, w, _ in _sent(intervals)] == [81.0, 82.0]
    assert _rows(state) == {}
    state.close()


def test_rate_limit_stops_the_target_for_this_run_and_queues(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=True)
    m1, m2 = _m(80.0, 2), _m(81.0, 1)
    error = RetryNextRunError("Intervals.icu rate limit reached during wellness update (HTTP 429)")

    counts, errors, intervals, _ = _run(user, state, [m1, m2], fail_weights={80.0}, error=error)

    assert [w for _, w, _ in _sent(intervals)] == [80.0]  # no in-run retries
    assert "429" in errors["intervals"]
    assert counts == {"garmin": 2, "intervals": 0}  # Garmin keeps going
    assert ("intervals", m1.measurement_id) in _rows(state)
    state.close()


def test_rejected_key_is_reported_and_not_queued(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m1 = _m(80.0, 1)
    error = PermanentSyncError("Intervals.icu rejected the API key (HTTP 401). Run: eufy-sync --setup-intervals")

    counts, errors, intervals, _ = _run(user, state, [m1], fail_weights={80.0}, error=error)

    assert "--setup-intervals" in errors["intervals"]
    assert intervals.update_wellness.call_count == 1
    assert _rows(state) == {}
    state.close()


def test_queued_entry_is_dropped_once_a_newer_weigh_in_reaches_its_date(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    earlier, later = _m(80.0, 2), _m(80.5, 2, 60)
    _run(user, state, [earlier], fail_weights={80.0})
    assert ("intervals", earlier.measurement_id) in _rows(state)

    counts, errors, intervals, _ = _run(user, state, [earlier, later])

    assert errors == {} and counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(later), 80.5, 20.0)]
    assert _rows(state) == {}
    state.close()


def test_triage_drops_a_queued_weigh_in_its_date_has_moved_past(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    earlier, later = _m(80.0, 2), _m(80.5, 2, 60)
    _run(user, state, [later])
    state.record_upload_failure("default", "intervals", earlier.measurement_id,
                                earlier.timestamp.isoformat(), 80.0, NOON.isoformat())

    counts, errors, intervals, _ = _run(user, state, [])

    assert errors == {} and counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    assert _rows(state) == {}
    state.close()


def test_cursor_reaches_back_for_a_queued_failure(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    old, newer = _m(80.0, 10), _m(81.0, 1)
    _run(user, state, [newer])
    state.record_upload_failure("default", "intervals", old.measurement_id,
                                old.timestamp.isoformat(), 80.0, NOON.isoformat())
    fetches: list = []

    counts, errors, intervals, _ = _run(user, state, [old, newer], fetches=fetches)

    assert fetches[0] <= old.timestamp.timestamp()
    assert errors == {} and counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(old), 80.0, 20.0)]
    assert _rows(state) == {}
    state.close()


def test_cursor_reaches_back_for_a_raw_weigh_in_awaiting_its_processed_record(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    raw, newer = _m(80.0, 5, weight_only=True), _m(81.0, 1)
    _run(user, state, [raw, newer])
    fetches: list = []

    _run(user, state, [raw, newer], fetches=fetches)

    assert fetches[0] <= raw.timestamp.timestamp() - 60
    state.close()


def test_target_intervals_builds_only_that_client(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=True)
    garmin = MagicMock()

    with patch("eufy_sync.garmin_client.GarminClient", side_effect=AssertionError("not selected")):
        counts, errors, intervals, _ = _run(user, state, [_m(80.0, 1)], target="intervals", garmin=garmin)

    assert counts == {"intervals": 1} and errors == {}
    state.close()


def test_target_intervals_requires_it_configured(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = UserConfig(name="default", eufy=EufyConfig(email="e@example.com", password="pw"),
                      garmin=GarminConfig(email="g@example.com", password="pw"))
    with pytest.raises(ValueError, match="not configured"):
        sync_user(user, state, target="intervals")
    state.close()


def test_dry_run_sends_nothing(tmp_path: Path, capsys):
    state = SyncState(tmp_path / "s.db")
    user = _user()

    counts, errors, intervals, _ = _run(user, state, [_m(80.0, 2), _m(80.5, 2, 60)], dry_run=True)

    assert counts == {"intervals": 1}
    intervals.update_wellness.assert_not_called()
    assert "Would sync to intervals: 80.5 kg" in capsys.readouterr().out
    assert state.get_latest_sync_timestamp("default", "intervals") is None
    state.close()


def test_repair_resends_a_synced_date(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m = _m(80.0, 2)
    _run(user, state, [m])

    counts, _, intervals, _ = _run(user, state, [m], repair_days=7)

    assert counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(m), 80.0, 20.0)]
    state.close()


def test_authentication_failure_leaves_garmin_running(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=True)
    intervals = MagicMock()
    intervals.authenticate.side_effect = PermanentSyncError(
        "Intervals.icu is missing its API key or athlete id. Run: eufy-sync --setup-intervals"
    )
    garmin = MagicMock()
    garmin.has_weight_on_date.return_value = False
    garmin.upload_body_composition.return_value = {"ok": True}
    eufy = MagicMock()
    eufy.fetch_measurements.return_value = [_m(80.0, 1)]

    with patch("eufy_sync.sync.EufyClient", return_value=eufy), \
         patch("eufy_sync.intervals_client.IntervalsClient", return_value=intervals), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=garmin), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, headless=True)

    assert counts == {"garmin": 1}
    assert "--setup-intervals" in errors["intervals"]
    state.close()


# --- order independence (the per-date desired state) ---------------------------


def test_raw_arriving_after_its_processed_record_changes_nothing(tmp_path: Path):
    """Codex finding 1: a fetch that returns only the raw twin of a processed
    record already sent must not overwrite it with the raw weight."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    processed = _m(80.0, 2, 0, body_fat=19.5)
    raw = EufyMeasurement("raw-id", "cust", "dev", processed.timestamp + timedelta(seconds=30), 80.08,
                          weight_only=True)
    _run(user, state, [processed])

    counts, errors, intervals, _ = _run(user, state, [raw])
    assert errors == {} and counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    # Nothing is left waiting for a processed record that already landed.
    assert state.get_oldest_weight_only_timestamp("default", "intervals") is None

    counts, _, intervals, _ = _run(user, state, [processed, raw])
    assert counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    state.close()


def test_same_id_upgrade_across_midnight_lands_on_the_raw_readings_date(tmp_path: Path):
    """Codex finding 2. Policy: a weigh-in belongs to the date of its
    earliest reading."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    midnight = datetime.combine((NOON - timedelta(days=1)).date(), time()).astimezone()
    raw = EufyMeasurement("cust_x", "cust", "dev", midnight - timedelta(seconds=15), 80.0, weight_only=True)
    full = EufyMeasurement("cust_x", "cust", "dev", midnight + timedelta(seconds=15), 80.0, body_fat_pct=19.5)
    raw_day = _day(raw)
    assert raw_day != _day(full)
    _run(user, state, [raw])

    counts, errors, intervals, _ = _run(user, state, [full])
    assert errors == {} and counts == {"intervals": 1}
    assert _sent(intervals) == [(raw_day, 80.0, 19.5)]

    counts, _, intervals, _ = _run(user, state, [raw, full])
    assert counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    state.close()


def test_repair_after_an_upgrade_resends_the_processed_values(tmp_path: Path):
    """Codex finding 3: the replaced raw reading, later than the processed
    one, must not block the processed values on repair."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    processed = _m(80.0, 2, 0, body_fat=19.5)
    raw = EufyMeasurement("raw-id", "cust", "dev", processed.timestamp + timedelta(seconds=30), 80.05,
                          weight_only=True)
    _run(user, state, [raw])
    counts, _, intervals, _ = _run(user, state, [raw, processed])
    assert counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(processed), 80.0, 19.5)]

    counts, _, intervals, _ = _run(user, state, [raw, processed])
    assert counts == {"intervals": 0}

    counts, _, intervals, _ = _run(user, state, [raw, processed], repair_days=7)
    assert counts == {"intervals": 1}
    assert _sent(intervals) == [(_day(processed), 80.0, 19.5)]
    state.close()


@pytest.mark.parametrize("first_run, second_run", [
    (["later"], ["earlier"]),
    (["earlier"], ["later"]),
    (["earlier", "later"], []),
    (["later", "earlier"], []),
])
def test_the_later_weigh_in_wins_whatever_the_fetch_order(tmp_path: Path, first_run, second_run):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    by_name = {"earlier": _m(81.0, 2, 0, body_fat=21.0), "later": _m(80.0, 2, 180, body_fat=20.0)}
    sent = []
    for batch in (first_run, second_run):
        if batch:
            _, _, intervals, _ = _run(user, state, [by_name[n] for n in batch])
            sent.extend(_sent(intervals))

    # The last value sent to the date is what Intervals.icu holds.
    assert sent[-1] == (_day(by_name["later"]), 80.0, 20.0)
    state.close()


def test_no_put_when_a_new_weigh_in_has_the_same_values(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    first, second = _m(80.0, 2, 0), _m(80.0, 2, 240)
    _run(user, state, [first])

    counts, errors, intervals, _ = _run(user, state, [first, second])

    assert errors == {} and counts == {"intervals": 0}
    intervals.update_wellness.assert_not_called()
    # The date's record now names the newer weigh-in.
    day = second.timestamp.astimezone().date()
    readings = state.get_intervals_days("default", [day])[day]["readings"]
    assert [r["id"] for r in readings] == [second.measurement_id]
    state.close()


def test_new_table_is_created_on_an_existing_database(tmp_path: Path):
    import sqlite3

    path = tmp_path / "s.db"
    SyncState(path).close()
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE intervals_days")
    state = SyncState(path)
    assert state.get_intervals_days("default", [NOON.date()]) == {}
    state.close()
