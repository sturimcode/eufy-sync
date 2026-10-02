"""Failed uploads: the fetch cursor brings them back, the retry queue counts
the attempts and gives up on a measurement that would otherwise hold back
every newer one."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from eufy_sync.cli.status import _retry_queue_line
from eufy_sync.config import EufyConfig, GarminConfig, StravaConfig, UserConfig, ZwiftConfig
from eufy_sync.eufy_client import EufyMeasurement
from eufy_sync.state import SyncState
from eufy_sync.sync import (
    MAX_RETRY_ATTEMPTS,
    RETRY_MAX_AGE_DAYS,
    PermanentSyncError,
    UnsupportedMeasurementError,
    sync_user,
)

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _m(weight_kg: float, days_ago: float) -> EufyMeasurement:
    dt = NOW - timedelta(days=days_ago)
    return EufyMeasurement(
        measurement_id=f"cust_{int(dt.timestamp())}",
        customer_id="cust",
        device_id="dev",
        timestamp=dt,
        weight_kg=weight_kg,
    )


def _user(garmin: bool = True, strava: bool = False) -> UserConfig:
    return UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        garmin=GarminConfig(email="g@example.com", password="pw") if garmin else None,
        strava=StravaConfig(client_id="cid", client_secret="csec") if strava else None,
    )


def _run(user, state, history, fail_weights=(), garmin_error=None, strava_error=None, **kwargs):
    """One sync run against a fake Eufy that honors the fetch cursor the way
    the real one does (timestamp >= after). Uploads of a weight listed in
    fail_weights raise the given error (a Garmin 503 by default)."""
    fake_eufy = MagicMock()
    fake_eufy.fetch_measurements.side_effect = lambda after_timestamp=None: [
        m for m in history if after_timestamp is None or m.timestamp.timestamp() >= after_timestamp
    ]

    def garmin_upload(body_comp):
        if round(body_comp.weight, 2) in fail_weights:
            raise garmin_error or RuntimeError("Garmin returned 503")
        return {"ok": True}

    fake_garmin = MagicMock()
    fake_garmin.has_weight_on_date.return_value = False
    fake_garmin.upload_body_composition.side_effect = garmin_upload

    def strava_update(weight_kg):
        if round(weight_kg, 2) in fail_weights:
            raise strava_error or RuntimeError("Strava returned 503")
        return {"weight": weight_kg}

    fake_strava = MagicMock()
    fake_strava.update_weight.side_effect = strava_update

    with patch("eufy_sync.sync.EufyClient", return_value=fake_eufy), \
         patch("eufy_sync.garmin_client.GarminClient", return_value=fake_garmin), \
         patch("eufy_sync.strava_client.StravaClient", return_value=fake_strava), \
         patch("eufy_sync.sync.time.sleep"):
        counts, errors = sync_user(user, state, headless=True, **kwargs)
    return counts, errors, fake_garmin, fake_strava


def _garmin_weights(fake_garmin) -> list[float]:
    return [round(c.args[0].weight, 2) for c in fake_garmin.upload_body_composition.call_args_list]


def _rows(state, user_name="default") -> dict[tuple[str, str], dict]:
    return {(r["target"], r["measurement_id"]): r for r in state.get_upload_retries(user_name)}


def test_garmin_failure_is_queued_and_retried_once_next_run(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m1, m2, m3 = _m(80.0, 3), _m(81.0, 2), _m(82.0, 1)

    counts, errors, garmin, _ = _run(user, state, [m1, m2, m3], fail_weights={81.0})
    assert counts["garmin"] == 1 and "503" in errors["garmin"]
    assert _rows(state)[("garmin", m2.measurement_id)]["attempts"] == 1
    assert state.waiting_upload_retries("default") == {"garmin": 1}

    counts, errors, garmin, _ = _run(user, state, [m1, m2, m3])
    assert errors == {}
    # The failed one is replayed once; the one that landed is not resent.
    assert _garmin_weights(garmin) == [81.0, 82.0]
    assert _rows(state) == {}
    state.close()


def test_attempts_accumulate_across_runs(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m1 = _m(80.0, 1)
    for _ in range(3):
        _run(user, state, [m1], fail_weights={80.0})
    assert _rows(state)[("garmin", m1.measurement_id)]["attempts"] == 3
    state.close()


def test_permanent_unsupported_and_auth_failures_are_not_queued(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(strava=True)
    m1 = _m(80.0, 1)
    unauthorized = httpx.HTTPStatusError(
        "401", request=httpx.Request("POST", "https://example.invalid"),
        response=httpx.Response(401),
    )

    _run(user, state, [m1], fail_weights={80.0},
         garmin_error=PermanentSyncError("Run: eufy-sync --reauth garmin"),
         strava_error=UnsupportedMeasurementError("out of range"))
    _run(user, state, [m1], fail_weights={80.0}, garmin_error=unauthorized,
         strava_error=PermanentSyncError("Strava rejected the session"))
    assert _rows(state) == {}
    state.close()


def _cap(state, measurement, *, attempts=MAX_RETRY_ATTEMPTS):
    state.record_upload_failure(
        "default", "garmin", measurement.measurement_id, measurement.timestamp.isoformat(),
        measurement.weight_kg, NOW.isoformat(),
    )
    state._conn.execute(
        "UPDATE upload_retries SET attempts = ? WHERE measurement_id = ?",
        (attempts, measurement.measurement_id),
    )
    state._conn.commit()


def test_poison_measurement_is_given_up_once_a_newer_one_uploads(tmp_path: Path, caplog):
    """(b) The target is healthy and one measurement keeps failing: the first
    run where it is past its cap and a newer one lands gives it up."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    poison, m2, m3 = _m(80.0, 3), _m(81.0, 2), _m(82.0, 1)
    _cap(state, poison, attempts=MAX_RETRY_ATTEMPTS - 1)

    # One short of the cap: it still stops Garmin, as today.
    _, errors, garmin, _ = _run(user, state, [poison, m2, m3], fail_weights={80.0})
    assert _garmin_weights(garmin) == [80.0] * 3  # in-run retries only
    assert "503" in errors["garmin"]
    assert _rows(state)[("garmin", poison.measurement_id)]["gave_up"] is False

    with caplog.at_level(logging.WARNING, logger="eufy_sync"):
        _, errors, garmin, _ = _run(user, state, [poison, m2, m3], fail_weights={80.0})

    assert errors == {}
    assert _garmin_weights(garmin) == [80.0] * 3 + [81.0, 82.0]
    assert "Giving up on the Garmin upload of 80.00 kg" in caplog.text
    assert _rows(state)[("garmin", poison.measurement_id)]["gave_up"] is True
    assert state.waiting_upload_retries("default") == {}

    # Given up for good: a later run that reaches back to it skips it.
    _, _, garmin, _ = _run(user, state, [poison, m2, m3], backfill_days=7)
    assert _garmin_weights(garmin) == []
    state.close()


def test_poison_measurement_past_the_age_cap_is_given_up_the_same_way(tmp_path: Path, caplog):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    old, new = _m(80.0, RETRY_MAX_AGE_DAYS + 1), _m(81.0, 1)
    _cap(state, old, attempts=1)

    with caplog.at_level(logging.WARNING, logger="eufy_sync"):
        _, errors, garmin, _ = _run(user, state, [old, new], fail_weights={80.0}, backfill_days=30)

    assert errors == {}
    assert _garmin_weights(garmin)[-1] == 81.0
    assert "Giving up on the Garmin upload of 80.00 kg" in caplog.text
    assert _rows(state)[("garmin", old.measurement_id)]["gave_up"] is True
    state.close()


def test_capped_entry_stays_pending_when_newer_ones_fail_too(tmp_path: Path):
    """(c) The newer measurements fail as well, so the target is down: the
    capped entry keeps counting attempts and nothing is given up."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    head, m2, m3 = _m(80.0, RETRY_MAX_AGE_DAYS + 1), _m(81.0, 2), _m(82.0, 1)
    _cap(state, head)

    _, errors, garmin, _ = _run(user, state, [head, m2, m3], fail_weights={80.0, 81.0, 82.0}, backfill_days=30)

    assert "503" in errors["garmin"]
    # It moved past the capped head, then stopped at the next failure.
    assert _garmin_weights(garmin) == [80.0] * 3 + [81.0] * 3
    rows = _rows(state)
    assert rows[("garmin", head.measurement_id)]["gave_up"] is False
    assert rows[("garmin", head.measurement_id)]["attempts"] == MAX_RETRY_ATTEMPTS + 1
    assert rows[("garmin", m2.measurement_id)]["attempts"] == 1
    assert ("garmin", m3.measurement_id) not in rows
    state.close()


def test_capped_entry_that_is_the_newest_stays_pending_and_reports(tmp_path: Path):
    """Nothing newer to prove the target healthy: keep it, report the error."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    only = _m(80.0, RETRY_MAX_AGE_DAYS + 1)
    _cap(state, only)

    _, errors, _, _ = _run(user, state, [only], fail_weights={80.0}, backfill_days=30)

    assert "503" in errors["garmin"]
    assert _rows(state)[("garmin", only.measurement_id)]["gave_up"] is False
    state.close()


def test_three_week_outage_then_recovery_delivers_everything_in_order(tmp_path: Path):
    """(a) Garmin is down for three weeks while a weigh-in arrives every day.
    Every entry passes both caps, but no newer upload ever lands during the
    outage, so nothing is given up and recovery delivers it all in order."""
    state = SyncState(tmp_path / "s.db")
    user = _user()
    before = _m(79.0, 22)
    state.record_sync("default", before.measurement_id, before.timestamp.isoformat(), 79.0, NOW.isoformat())
    days = [_m(round(80.0 + d / 10, 2), 21 - d) for d in range(21)]
    weights = [m.weight_kg for m in days]

    for d in range(1, 22):
        _, errors, _, _ = _run(user, state, days[:d], fail_weights=set(weights))
        assert "503" in errors["garmin"]
    state._conn.execute("UPDATE upload_retries SET attempts = ?", (MAX_RETRY_ATTEMPTS,))
    state._conn.commit()
    _run(user, state, days, fail_weights=set(weights))
    assert not any(r["gave_up"] for r in _rows(state).values())

    _, errors, garmin, _ = _run(user, state, days)

    assert errors == {}
    assert _garmin_weights(garmin) == weights
    assert _rows(state) == {}
    state.close()


def test_repair_overrides_a_given_up_entry_and_success_clears_it(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    m1 = _m(80.0, 1)
    state.record_upload_failure("default", "garmin", m1.measurement_id, m1.timestamp.isoformat(), 80.0, NOW.isoformat())
    state.give_up_upload_retry("default", "garmin", m1.measurement_id)

    _, _, garmin, _ = _run(user, state, [m1], repair_days=3)
    assert _garmin_weights(garmin) == [80.0]
    assert _rows(state) == {}
    state.close()


def test_strava_replays_a_failed_weight_while_it_is_still_the_newest(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=False, strava=True)
    latest = _m(80.0, 1)

    _run(user, state, [latest], fail_weights={80.0})
    assert ("strava", latest.measurement_id) in _rows(state)

    _, errors, _, strava = _run(user, state, [latest])
    assert errors == {}
    assert [c.args[0] for c in strava.update_weight.call_args_list] == [80.0]
    assert _rows(state) == {}
    state.close()


def test_strava_never_replays_an_older_weight_once_a_newer_one_exists(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=False, strava=True)
    older, newer = _m(80.0, 2), _m(79.5, 1)

    _run(user, state, [older], fail_weights={80.0})
    _, errors, _, strava = _run(user, state, [older, newer])

    assert errors == {}
    assert [c.args[0] for c in strava.update_weight.call_args_list] == [79.5]
    assert _rows(state) == {}
    state.close()


def test_newer_strava_failure_replaces_the_older_entry(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=False, strava=True)
    older, newer = _m(80.0, 2), _m(79.5, 1)

    _run(user, state, [older], fail_weights={80.0})
    _run(user, state, [older, newer], fail_weights={79.5})

    assert set(_rows(state)) == {("strava", newer.measurement_id)}
    state.close()


def test_strava_entry_behind_its_current_weight_is_dropped_without_sending(tmp_path: Path):
    """A newer weight reached Strava some other way (e.g. --target strava
    with a different fetch); the queued older one must not overwrite it."""
    state = SyncState(tmp_path / "s.db")
    user = _user(garmin=False, strava=True)
    older, newer = _m(80.0, 2), _m(79.5, 1)
    state.record_upload_failure("default", "strava", older.measurement_id, older.timestamp.isoformat(), 80.0, NOW.isoformat())
    state.record_sync("default", newer.measurement_id, newer.timestamp.isoformat(), 79.5, NOW.isoformat(), target="strava")

    _, _, _, strava = _run(user, state, [older], backfill_days=7)
    strava.update_weight.assert_not_called()
    assert _rows(state) == {}
    state.close()


def test_dry_run_predicts_without_touching_the_queue(tmp_path: Path, capsys):
    state = SyncState(tmp_path / "s.db")
    user = _user()
    aged, failed = _m(80.0, RETRY_MAX_AGE_DAYS + 1), _m(81.0, 1)
    _cap(state, aged, attempts=1)
    _cap(state, failed, attempts=1)
    state.give_up_upload_retry("default", "garmin", failed.measurement_id)
    state.record_upload_failure("default", "garmin", "synced", NOW.isoformat(), 79.0, NOW.isoformat())
    state.record_sync("default", "synced", NOW.isoformat(), 79.0, NOW.isoformat())
    before = _rows(state)

    _, _, garmin, _ = _run(user, state, [aged, failed], backfill_days=30, dry_run=True)

    garmin.upload_body_composition.assert_not_called()
    assert _rows(state) == before
    out = capsys.readouterr().out
    # The capped one would be tried and given up only if a newer one lands;
    # the given-up one would not be sent at all.
    assert "Would retry garmin: 80.0 kg" in out and "given up if it fails" in out
    assert "81.0 kg" not in out
    state.close()


def test_existing_database_gains_the_retry_table(tmp_path: Path):
    """A state.db from 1.14.0 has sync_log (with weight_only) and
    pending_upgrades but no upload_retries. Opening it adds the table and
    keeps every existing row."""
    db_path = tmp_path / "state.db"
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
            weight_only INTEGER NOT NULL DEFAULT 0,
            UNIQUE(user_name, eufy_measurement_id, target)
        );
        CREATE TABLE pending_upgrades (
            user_name TEXT NOT NULL,
            measurement_id TEXT NOT NULL,
            previous_measurement_id TEXT NOT NULL,
            measurement_json TEXT NOT NULL,
            PRIMARY KEY(user_name, measurement_id)
        );
    """)
    conn.execute(
        "INSERT INTO sync_log (user_name, eufy_measurement_id, measurement_timestamp, weight_kg, target, synced_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("default", "m1", "2026-09-01T08:00:00+00:00", 80.0, "garmin", "2026-09-01T08:01:00+00:00"),
    )
    conn.commit()
    conn.close()

    state = SyncState(db_path)
    assert state.is_synced("default", "m1", "garmin")
    assert state.get_upload_retries("default") == []
    assert state.record_upload_failure("default", "garmin", "m2", "2026-09-02T08:00:00+00:00", 80.1, NOW.isoformat()) == 1
    state.close()

    # Reopening is a no-op for the schema and keeps the queued entry.
    state = SyncState(db_path)
    assert state.waiting_upload_retries("default") == {"garmin": 1}
    state.close()


def test_status_line_counts_waiting_uploads(tmp_path: Path):
    state = SyncState(tmp_path / "s.db")
    user = _user(strava=True)
    assert _retry_queue_line(state, user) is None

    for mid in ("a", "b"):
        state.record_upload_failure("default", "garmin", mid, NOW.isoformat(), 80.0, NOW.isoformat())
    assert _retry_queue_line(state, user) == "2 uploads waiting to retry (Garmin)"

    state.record_upload_failure("default", "strava", "c", NOW.isoformat(), 80.0, NOW.isoformat())
    state.give_up_upload_retry("default", "garmin", "b")
    assert _retry_queue_line(state, user) == "2 uploads waiting to retry (Garmin, Strava)"

    # A target removed from the config will never retry, so it is not shown.
    garmin_only = _user()
    assert _retry_queue_line(state, garmin_only) == "1 upload waiting to retry (Garmin)"
    state.close()


def test_status_shows_the_retry_line(tmp_path: Path, capsys):
    from eufy_sync.cli.status import _show_status

    state = SyncState(tmp_path / "s.db")
    user = UserConfig(
        name="default",
        eufy=EufyConfig(email="e@example.com", password="pw"),
        zwift=ZwiftConfig(email="z@example.com", password="pw"),
    )
    state.record_upload_failure("default", "zwift", "a", NOW.isoformat(), 80.0, NOW.isoformat())

    with patch("eufy_sync.eufy_client.EufyClient") as eufy, \
         patch("eufy_sync.cli.status._zwift_token_status", return_value={"state": "valid"}):
        eufy.return_value.token_status.return_value = {"state": "valid", "days_remaining": 10}
        _show_status(state, [user])

    assert "1 upload waiting to retry (Zwift)" in capsys.readouterr().out
    state.close()
