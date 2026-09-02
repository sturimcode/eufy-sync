from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.config import GarminConfig
from eufy_sync.garmin_client import GarminClient
from eufy_sync.transform import GarminBodyComposition


def _client_with_fake_garmin(fake_garmin):
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    client._garmin = fake_garmin
    return client


def test_upload_maps_fields_to_add_body_composition():
    fake = MagicMock()
    fake.add_body_composition.return_value = {"ok": True}
    client = _client_with_fake_garmin(fake)
    bc = GarminBodyComposition(
        timestamp="2026-06-10T08:00:00+00:00",
        weight=86.2,
        percent_fat=18.5,
        percent_hydration=55.3,
        visceral_fat_rating=8.0,
        bone_mass=3.2,
        muscle_mass=45.2,
        basal_met=1650,
        metabolic_age=28,
        bmi=None,
    )
    client.upload_body_composition(bc)
    kwargs = fake.add_body_composition.call_args.kwargs
    assert kwargs["weight"] == 86.2
    assert kwargs["timestamp"] == "2026-06-10T08:00:00+00:00"
    assert kwargs["percent_fat"] == 18.5
    assert kwargs["visceral_fat_rating"] == 8.0
    assert kwargs["basal_met"] == 1650
    assert kwargs["bmi"] is None


def test_has_weight_on_date_true_with_daily_summaries_key():
    fake = MagicMock()
    fake.get_body_composition.return_value = {"dailyWeightSummaries": [{"weight": 86000}]}
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True


def test_has_weight_on_date_true_when_entry_exists():
    fake = MagicMock()
    fake.get_body_composition.return_value = {"dateWeightList": [{"weight": 86000}]}
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True


def test_has_weight_on_date_false_when_empty():
    fake = MagicMock()
    fake.get_body_composition.return_value = {"dateWeightList": []}
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False


def test_has_weight_on_date_false_on_read_error():
    fake = MagicMock()
    fake.get_body_composition.side_effect = RuntimeError("boom")
    client = _client_with_fake_garmin(fake)
    assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False


def test_has_weight_on_date_queries_local_calendar_date():
    """The duplicate check must query by LOCAL date, since that is the
    calendar date Garmin files the (now-corrected) upload under. midnight_utc
    is a UTC instant chosen right at 00:00 UTC: in any timezone west of UTC
    (negative offset) the local calendar date is one day earlier, so this
    reliably diverges from the raw-UTC date string on most machines while the
    assertion itself stays generic (no hardcoded offset)."""
    fake = MagicMock()
    fake.get_body_composition.return_value = {"dateWeightList": []}
    client = _client_with_fake_garmin(fake)

    midnight_utc = datetime(2026, 6, 10, 0, 0, 0, tzinfo=timezone.utc)
    client.has_weight_on_date(midnight_utc)

    expected_date_str = midnight_utc.astimezone().strftime("%Y-%m-%d")
    args, kwargs = fake.get_body_composition.call_args
    queried = args[0] if args else kwargs.get("startdate")
    assert queried == expected_date_str


def test_authenticate_uses_auth_login():
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    fake_garmin = MagicMock()
    with patch.object(client._auth, "login", return_value=fake_garmin) as login:
        client.authenticate(allow_interactive=False)
    login.assert_called_once_with(interactive=False)
    assert client._garmin is fake_garmin


def test_upload_reauths_and_retries_on_auth_error_when_interactive():
    from garminconnect import GarminConnectAuthenticationError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    fresh = MagicMock()
    fresh.add_body_composition.return_value = {"ok": True}
    client._garmin = dead
    client._allow_interactive = True
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth", return_value=fresh) as reauth:
        client.upload_body_composition(bc)
    reauth.assert_called_once()
    fresh.add_body_composition.assert_called_once()   # retried on the fresh client
    assert client._garmin is fresh


def test_upload_relogs_in_silently_and_retries_when_headless():
    # A scheduled run has nobody to prompt, but the stored password normally
    # logs straight back in, so the run heals itself instead of nagging.
    from garminconnect import GarminConnectAuthenticationError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    fresh = MagicMock()
    fresh.add_body_composition.return_value = {"ok": True}
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        with patch.object(client._auth, "force_reauth") as interactive_reauth:
            result = client.upload_body_composition(bc)
    reauth.assert_called_once()
    interactive_reauth.assert_not_called()   # never the prompting path
    fresh.add_body_composition.assert_called_once()
    assert client._garmin is fresh
    assert result == {"ok": True}


def test_upload_propagates_a_silent_relogin_that_needs_mfa():
    from garminconnect import GarminConnectAuthenticationError

    from eufy_sync.sync import PermanentSyncError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(
        client._auth, "silent_reauth",
        side_effect=PermanentSyncError("Garmin wants an MFA code... Run: eufy-sync --reauth garmin"),
    ):
        with pytest.raises(PermanentSyncError) as exc:
            client.upload_body_composition(bc)
    # The hint reaches app.py's notification classifier unchanged.
    assert "--reauth garmin" in str(exc.value)


def test_upload_reauths_on_401_connection_error_when_interactive():
    # Garmin surfaces a dead session as a 401 GarminConnectConnectionError, not
    # a GarminConnectAuthenticationError. It must still trigger a re-login.
    from garminconnect import GarminConnectConnectionError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.add_body_composition.return_value = {"ok": True}
    client._garmin = dead
    client._allow_interactive = True
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth", return_value=fresh) as reauth:
        client.upload_body_composition(bc)
    reauth.assert_called_once()
    fresh.add_body_composition.assert_called_once()
    assert client._garmin is fresh


def test_upload_relogs_in_silently_on_401_when_headless():
    # The 401 flavor of a dead session takes the same silent recovery.
    from garminconnect import GarminConnectConnectionError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.add_body_composition.return_value = {"ok": True}
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        client.upload_body_composition(bc)
    reauth.assert_called_once()
    fresh.add_body_composition.assert_called_once()


def test_upload_propagates_a_failed_retry_after_a_silent_relogin():
    # The re-login worked, the retried upload did not. That error is the run's
    # real problem and must not be masked by anything here.
    from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    fresh = MagicMock()
    fresh.add_body_composition.side_effect = GarminConnectConnectionError("API Error 500 - ")
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", return_value=fresh):
        with pytest.raises(GarminConnectConnectionError):
            client.upload_body_composition(bc)
    fresh.add_body_composition.assert_called_once()   # retried once, not looped


def test_upload_propagates_non_auth_connection_error():
    # A non-401 connection error (e.g. 500) is transient, not an auth failure:
    # it must propagate to _retry, not trigger a re-login.
    from garminconnect import GarminConnectConnectionError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    fake = MagicMock()
    fake.add_body_composition.side_effect = GarminConnectConnectionError("API Error 500 - ")
    client._garmin = fake
    client._allow_interactive = True
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth") as reauth:
        with pytest.raises(GarminConnectConnectionError):
            client.upload_body_composition(bc)
    reauth.assert_not_called()


def test_upload_propagates_rate_limit_without_reauth():
    # A 429 mid-sync is not an auth error, so upload does not re-auth; it
    # propagates and sync._is_permanent stops it from being retried.
    from garminconnect import GarminConnectTooManyRequestsError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    fake = MagicMock()
    fake.add_body_composition.side_effect = GarminConnectTooManyRequestsError("429")
    client._garmin = fake
    client._allow_interactive = True
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth") as reauth:
        with pytest.raises(GarminConnectTooManyRequestsError):
            client.upload_body_composition(bc)
    reauth.assert_not_called()  # a 429 must not trigger a re-login


# ---------------------------------------------------------------------------
# The duplicate check heals a dead session the same way upload does
# ---------------------------------------------------------------------------


def test_duplicate_check_relogs_in_silently_and_retries_when_headless():
    # The duplicate check is the run's first Garmin call, so a token that
    # expired between runs used to surface here as a warning on every
    # scheduled sync. It must heal and answer instead.
    from garminconnect import GarminConnectConnectionError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.get_body_composition.return_value = {"dateWeightList": [{"weight": 86000}]}
    client._garmin = dead
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True
    reauth.assert_called_once()
    assert client._garmin is fresh   # later calls ride the healed session


def test_duplicate_check_reauths_on_auth_error_when_interactive():
    from garminconnect import GarminConnectAuthenticationError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    fresh = MagicMock()
    fresh.get_body_composition.return_value = {"dateWeightList": []}
    client._garmin = dead
    client._allow_interactive = True
    with patch.object(client._auth, "force_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
    reauth.assert_called_once()


def test_duplicate_check_fails_open_when_the_relogin_fails():
    # A relogin that wants MFA must not end the run inside the duplicate
    # check: fail open, and let the upload raise the error that carries the
    # fix-it hint.
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import PermanentSyncError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    client._garmin = dead
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth",
                      side_effect=PermanentSyncError("Garmin wants an MFA code")):
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False


# ---------------------------------------------------------------------------
# close() persists whatever the library rotated during the run
# ---------------------------------------------------------------------------


def test_close_saves_a_token_rotated_during_the_run():
    fake = MagicMock()
    client = _client_with_fake_garmin(fake)
    with patch.object(client._auth, "save_if_changed") as save:
        client.close()
    save.assert_called_once_with(fake)
    assert client._garmin is None


def test_close_without_a_session_saves_nothing():
    # sync_user closes every client it built, including ones whose
    # authenticate() failed and never set _garmin.
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    with patch.object(client._auth, "save_if_changed") as save:
        client.close()
    save.assert_not_called()


def test_close_survives_a_failing_save():
    client = _client_with_fake_garmin(MagicMock())
    with patch.object(client._auth, "save_if_changed", side_effect=RuntimeError("keychain locked")):
        client.close()   # must not raise
    assert client._garmin is None


# ---------------------------------------------------------------------------
# Issue #48: deleting our weight-only entry so the full record replaces it
# ---------------------------------------------------------------------------

UPLOADED_AT = datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc)
DATE_STR = UPLOADED_AT.astimezone().strftime("%Y-%m-%d")


def _millis(dt: datetime) -> int:
    """Garmin reports weigh-in timestamps as epoch milliseconds."""
    return int(dt.timestamp() * 1000)


def _weigh_ins(*entries):
    fake = MagicMock()
    fake.get_daily_weigh_ins.return_value = {"dateWeightList": list(entries)}
    return fake


def test_delete_weight_entry_matches_by_weight_and_deletes():
    """Garmin returning no timestamps at all leaves weight as the only
    evidence; a single match still stands on its own."""
    fake = _weigh_ins(
        {"samplePk": 111, "weight": 92500.0},   # someone else's 92.5 kg entry
        {"samplePk": 222, "weight": 85000.0},   # our 85.0 kg weight-only upload
    )
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is True

    fake.get_daily_weigh_ins.assert_called_once_with(DATE_STR)
    fake.delete_weigh_in.assert_called_once_with(222, DATE_STR)


def test_delete_weight_entry_no_match_deletes_nothing():
    fake = _weigh_ins({"samplePk": 111, "weight": 92500.0})
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
    fake.delete_weigh_in.assert_not_called()


def test_delete_weight_entry_matches_on_weight_and_timestamp():
    fake = _weigh_ins(
        {"samplePk": 111, "weight": 92500.0, "timestampGMT": _millis(UPLOADED_AT)},
        {"samplePk": 222, "weight": 85000.0, "timestampGMT": _millis(UPLOADED_AT)},
    )
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is True
    fake.delete_weigh_in.assert_called_once_with(222, DATE_STR)


def test_delete_weight_entry_picks_the_entry_stamped_at_our_upload():
    """Two entries within the weight window on the same day: only the one
    stamped at the instant we uploaded is ours. Matching on weight alone used
    to delete whichever came first in the list, which could be a manual
    weigh-in at a similar weight."""
    manual = UPLOADED_AT + timedelta(hours=5)
    fake = _weigh_ins(
        {"samplePk": 111, "weight": 85030.0, "timestampGMT": _millis(manual)},
        {"samplePk": 222, "weight": 85000.0, "timestampGMT": _millis(UPLOADED_AT)},
    )
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is True
    fake.delete_weigh_in.assert_called_once_with(222, DATE_STR)


def test_delete_weight_entry_spares_a_manual_weigh_in_at_another_time():
    """The single entry near our weight is stamped hours away, so it is not
    the one we uploaded. Nothing is deleted; the caller uploads anyway."""
    fake = _weigh_ins(
        {"samplePk": 111, "weight": 85000.0,
         "timestampGMT": _millis(UPLOADED_AT + timedelta(hours=6))},
    )
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
    fake.delete_weigh_in.assert_not_called()


def test_delete_weight_entry_leaves_two_close_timestamps_alone():
    """Both entries fall inside the weight and time windows, so neither can be
    identified as ours. Fail open to the duplicate rather than guess."""
    fake = _weigh_ins(
        {"samplePk": 111, "weight": 85000.0,
         "timestampGMT": _millis(UPLOADED_AT + timedelta(seconds=30))},
        {"samplePk": 222, "weight": 85020.0, "timestampGMT": _millis(UPLOADED_AT)},
    )
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
    fake.delete_weigh_in.assert_not_called()


def test_delete_weight_entry_leaves_untimed_duplicates_alone():
    """No timestamps and two entries in the weight window: still ambiguous."""
    fake = _weigh_ins(
        {"samplePk": 111, "weight": 85030.0},
        {"samplePk": 222, "weight": 85000.0},
    )
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
    fake.delete_weigh_in.assert_not_called()


def test_delete_weight_entry_accepts_the_date_field_as_the_instant():
    """Garmin's timestampGMT and date differ by the device's UTC offset and
    are not consistent about which carries the true instant, so a match on
    either counts."""
    fake = _weigh_ins({"samplePk": 222, "weight": 85000.0, "date": _millis(UPLOADED_AT)})
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is True
    fake.delete_weigh_in.assert_called_once_with(222, DATE_STR)


def test_delete_weight_entry_ignores_unusable_timestamp_fields():
    """A null or non-numeric timestamp is no timestamp; the entry falls back
    to the weight-only path instead of failing to parse."""
    fake = _weigh_ins({"samplePk": 222, "weight": 85000.0,
                       "timestampGMT": None, "date": "2026-07-09"})
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(UPLOADED_AT, 85.0) is True
    fake.delete_weigh_in.assert_called_once_with(222, DATE_STR)


def test_delete_weight_entry_fails_open_on_api_error():
    """A failed lookup or delete must not break the sync run; the caller
    uploads anyway and the worst case is the pre-existing duplicate."""
    fake = MagicMock()
    fake.get_daily_weigh_ins.side_effect = RuntimeError("Garmin 500")
    client = _client_with_fake_garmin(fake)

    assert client.delete_weight_entry(datetime(2026, 7, 9, 7, 0, tzinfo=timezone.utc), 85.0) is False


def test_delete_weight_entry_relogs_in_and_retries_on_401():
    """A dead session at delete time used to fail open and hand back the very
    duplicate this method exists to prevent (issue #48). Heal and retry."""
    from garminconnect import GarminConnectConnectionError
    dead = MagicMock()
    dead.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = _weigh_ins({"samplePk": 222, "weight": 85000.0})
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False

    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.delete_weight_entry(UPLOADED_AT, 85.0) is True
    reauth.assert_called_once()
    fresh.delete_weigh_in.assert_called_once_with(222, DATE_STR)


def test_delete_weight_entry_fails_open_when_the_relogin_fails():
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import PermanentSyncError
    dead = MagicMock()
    dead.get_daily_weigh_ins.side_effect = GarminConnectConnectionError("API Error 401 - ")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False

    with patch.object(client._auth, "silent_reauth",
                      side_effect=PermanentSyncError("Garmin wants an MFA code")):
        assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
