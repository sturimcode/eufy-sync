from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
from garminconnect import Garmin

from eufy_sync import garmin_client
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


def test_a_successful_relogin_is_not_repeated_when_the_api_keeps_refusing():
    # Cloudflare 403s on the API while SSO logins succeed: the first call
    # relogs in, and every later call must fail with its own error instead
    # of running a full login each time. The upload, having outlived both the
    # relogin and the fingerprint retry, is classified as permanent so _retry
    # does not ask again seconds later.
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import PermanentSyncError

    blocked = GarminConnectConnectionError("API Error 403 - ")
    stale = MagicMock()
    stale.get_body_composition.side_effect = blocked
    fresh = MagicMock()
    fresh.get_body_composition.side_effect = blocked
    fresh.get_daily_weigh_ins.side_effect = blocked
    fresh.add_body_composition.side_effect = blocked
    client = _client_with_fake_garmin(stale)
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=86.2)

    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        with pytest.raises(GarminConnectConnectionError, match="403"):
            client.check_connection()
        with pytest.raises(PermanentSyncError, match="403"):
            client.upload_body_composition(bc)

    reauth.assert_called_once()


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


# ---------------------------------------------------------------------------
# One relogin per run
# ---------------------------------------------------------------------------


def test_failed_relogin_in_duplicate_check_is_not_retried_by_the_upload():
    # The duplicate check fails open after a relogin that wants MFA. The
    # upload that follows must report that same failure, with its fix-it hint,
    # rather than try a second login (another MFA demand, more 429 risk).
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import PermanentSyncError
    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectConnectionError("API Error 403")
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 403")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    failure = PermanentSyncError("Garmin wants an MFA code. Run: eufy-sync --reauth garmin")
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", side_effect=failure) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        with pytest.raises(PermanentSyncError) as exc:
            client.upload_body_composition(bc)
    reauth.assert_called_once()
    assert exc.value is failure
    assert "--reauth garmin" in str(exc.value)


def test_failed_relogin_in_delete_is_not_retried_by_the_upload():
    from garminconnect import GarminConnectAuthenticationError

    from eufy_sync.sync import PermanentSyncError
    dead = MagicMock()
    dead.get_daily_weigh_ins.side_effect = GarminConnectAuthenticationError("dead")
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = True
    failure = PermanentSyncError("Garmin login cancelled")
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "force_reauth", side_effect=failure) as reauth:
        assert client.delete_weight_entry(UPLOADED_AT, 85.0) is False
        with pytest.raises(PermanentSyncError):
            client.upload_body_composition(bc)
    reauth.assert_called_once()


def test_old_session_still_serves_calls_after_a_failed_relogin():
    # A Cloudflare 403 reads like a dead session but may pass. After the
    # relogin fails, the old session is kept, and a later call that goes
    # through is not blocked by the remembered failure.
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import PermanentSyncError
    session = MagicMock()
    session.get_body_composition.side_effect = GarminConnectConnectionError("API Error 403")
    session.add_body_composition.return_value = {"ok": True}
    client = _client_with_fake_garmin(session)
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with patch.object(client._auth, "silent_reauth", side_effect=PermanentSyncError("mfa")):
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        assert client.upload_body_composition(bc) == {"ok": True}
    assert client._garmin is session


# ---------------------------------------------------------------------------
# Cloudflare blocks and the browser-fingerprint fallback (upstream issue #444)
#
# These run the real library against fake transports: a requests adapter for
# the library's own API session and a fake curl_cffi session for the fallback.
# Nothing leaves the machine.
# ---------------------------------------------------------------------------

TOKEN = "token-abc"
CF_PAGE = (
    403,
    {"Content-Type": "text/html; charset=UTF-8", "Server": "cloudflare", "CF-RAY": "8f1-EWR"},
    b"<!DOCTYPE html><html><head><title>Just a moment...</title></head>"
    b"<body><script src='/cdn-cgi/challenge-platform/h/b/orchestrate'></script></body></html>",
)
CF_MITIGATED = (403, {"Content-Type": "text/html", "cf-mitigated": "challenge"}, b"<html></html>")
# Issue #444's block: the same JSON a refused token gets, behind the same
# Cloudflare headers every API answer carries.
JSON_403 = (
    403,
    {"Content-Type": "application/json", "Server": "cloudflare", "CF-RAY": "8f1-EWR"},
    b'{"message":"HTTP 403 Forbidden","error":"ForbiddenException"}',
)


def _ok(body: dict, status: int = 200):
    return (status, {"Content-Type": "application/json"}, json.dumps(body).encode())


class _FakeAdapter(requests.adapters.BaseAdapter):
    """Answers the library's requests session from a script of responses."""

    def __init__(self, responses):
        super().__init__()
        self.responses = list(responses)
        self.sent = []

    def send(self, request, **kwargs):
        self.sent.append(request)
        status, headers, body = self.responses.pop(0)
        resp = requests.Response()
        resp.status_code = status
        resp.headers.update(headers)
        resp._content = body
        resp.url = request.url
        resp.request = request
        return resp

    def close(self):
        pass


class _FakeCffiResponse:
    def __init__(self, status, headers, body):
        self.status_code = status
        self.headers = headers
        self.content = body
        self.text = body.decode()

    def json(self):
        return json.loads(self.content)


class _FakeCffi:
    """Stands in for curl_cffi sessions; records what each fallback sent."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def session(self):
        fake = self

        class _Session:
            def request(self, method, url, headers=None, **kwargs):
                fake.calls.append({"method": method, "url": url, "headers": headers, **kwargs})
                return _FakeCffiResponse(*fake.responses.pop(0))

            def close(self):
                pass

        return _Session()


def _real_garmin(responses):
    garmin = Garmin("g@example.com", "pw", retry_attempts=0)
    garmin.client.di_token = TOKEN
    adapter = _FakeAdapter(responses)
    garmin.client._api_session.mount("https://", adapter)
    return garmin, adapter


def _client_on(garmin, interactive=False):
    client = _client_with_fake_garmin(garmin)
    client._allow_interactive = interactive
    client._watch_responses(garmin)
    return client


BC = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=86.2, percent_fat=18.5)


def test_cloudflare_page_on_upload_retries_through_curl_cffi_without_relogin():
    garmin, adapter = _real_garmin([CF_PAGE])
    original_session = garmin.client._api_session
    client = _client_on(garmin)
    cffi = _FakeCffi([_ok({"detailedImportResult": {"successes": [], "failures": []}}, status=202)])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        result = client.upload_body_composition(BC)

    reauth.assert_not_called()   # the token was never the problem
    assert result == {"detailedImportResult": {"successes": [], "failures": []}}
    assert len(adapter.sent) == 1 and len(cffi.calls) == 1
    call = cffi.calls[0]
    # Same endpoint, same token, and the FIT goes as a curl_cffi multipart
    # body because curl_cffi refuses files=.
    assert call["method"] == "POST"
    assert call["url"] == "https://connectapi.garmin.com/upload-service/upload"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert "files" not in call and call["multipart"] is not None
    assert garmin.client._api_session is original_session   # swapped back


def test_json_403_on_a_read_tries_the_fingerprint_before_any_relogin():
    # Issue #444: the block is a JSON 403. The fingerprint retry is one
    # request; a relogin is a login, a 429 risk, and on that network its own
    # token check fails the same way.
    garmin, _ = _real_garmin([JSON_403])
    client = _client_on(garmin)
    cffi = _FakeCffi([_ok({"dateWeightList": [{"weight": 86000}]})])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True

    reauth.assert_not_called()
    call = cffi.calls[0]
    assert call["method"] == "GET"
    assert call["url"] == "https://connectapi.garmin.com/weight-service/weight/dateRange"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert set(call["params"]) == {"startDate", "endDate"}


def test_once_the_fingerprint_works_later_calls_skip_plain_requests():
    # The block belongs to the network, so after one fingerprint success the
    # rest of the run goes straight through curl_cffi. A new client (the next
    # run) starts on plain requests again.
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    garmin, adapter = _real_garmin([JSON_403])
    client = _client_on(garmin)
    cffi = _FakeCffi([
        _ok({"dateWeightList": []}),
        _ok({"dateWeightList": [held]}),
        _ok({}),
        _ok({"detailedImportResult": {}}, status=202),
    ])
    original_session = garmin.client._api_session

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        assert client.delete_weight_entry(BC_INSTANT, 86.2) is True
        client.upload_body_composition(BC)

    reauth.assert_not_called()
    assert len(adapter.sent) == 1   # only the first, refused, plain request
    assert [c["method"] for c in cffi.calls] == ["GET", "GET", "DELETE", "POST"]
    assert garmin.client._api_session is original_session

    next_run, next_adapter = _real_garmin([_ok({"dateWeightList": []})])
    fresh_client = _client_on(next_run)
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session):
        fresh_client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc))
    assert len(next_adapter.sent) == 1 and len(cffi.calls) == 4


def test_a_failed_fingerprint_retry_does_not_make_it_sticky():
    garmin, adapter = _real_garmin([JSON_403, _ok({"dateWeightList": []})])
    client = _client_on(garmin)
    client._reauth_attempted = True   # isolate the fallback from the relogin
    cffi = _FakeCffi([JSON_403])
    with patch.object(garmin_client, "_new_impersonating_session", cffi.session):
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False
    assert len(adapter.sent) == 2 and len(cffi.calls) == 1


def test_a_cloudflare_block_that_survives_the_fingerprint_never_relogs_in():
    from eufy_sync.sync import PermanentSyncError

    garmin, _ = _real_garmin([CF_PAGE])
    client = _client_on(garmin)
    cffi = _FakeCffi([CF_MITIGATED])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(PermanentSyncError, match="Cloudflare"):
            client.upload_body_composition(BC)

    reauth.assert_not_called()
    assert len(cffi.calls) == 1   # one fallback per call, not a loop


def test_a_json_403_that_survives_the_fingerprint_relogs_in_once():
    # Both transports refuse the token, so it may really be dead: the run's one
    # relogin happens, and the fresh session gets its own fingerprint retry.
    stale, _ = _real_garmin([JSON_403])
    fresh, fresh_adapter = _real_garmin([JSON_403])
    client = _client_on(stale)
    cffi = _FakeCffi([JSON_403, _ok({"dateWeightList": []})])

    with patch.object(garmin_client, "_new_impersonating_session", cffi.session), \
            patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is False

    reauth.assert_called_once()
    assert len(fresh_adapter.sent) == 1 and len(cffi.calls) == 2
    assert client._garmin is fresh


def test_a_401_relogs_in_without_the_fingerprint_retry():
    from garminconnect import GarminConnectConnectionError

    dead = MagicMock()
    dead.get_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.get_body_composition.return_value = {"dateWeightList": []}
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client, "_call_impersonating") as fallback, \
            patch.object(client._auth, "silent_reauth", return_value=fresh):
        client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc))
    fallback.assert_not_called()
    dead.get_body_composition.assert_called_once()


def test_no_fallback_when_the_library_has_no_api_session():
    # A future library without _api_session loses the fallback, not the sync:
    # a 403 goes straight to the relogin as before.
    from garminconnect import GarminConnectConnectionError

    stale = MagicMock()
    stale.client._api_session = None
    stale.get_body_composition.side_effect = GarminConnectConnectionError("API Error 403 - ")
    fresh = MagicMock()
    fresh.get_body_composition.return_value = {"dateWeightList": [{"weight": 1}]}
    client = _client_with_fake_garmin(stale)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        assert client.has_weight_on_date(datetime(2026, 6, 10, tzinfo=timezone.utc)) is True
    reauth.assert_called_once()
    stale.get_body_composition.assert_called_once()


# ---------------------------------------------------------------------------
# Upload outcome classification
# ---------------------------------------------------------------------------


CONFLICT_409 = (409, {"Content-Type": "application/json"}, json.dumps({
    "detailedImportResult": {"failures": [{"messages": [{"content": "Duplicate Activity."}]}]},
}).encode())
BC_INSTANT = datetime.fromisoformat(BC.timestamp)


def test_upload_409_counts_as_uploaded_once_the_lookup_finds_the_weigh_in():
    held = {"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT)}
    garmin, adapter = _real_garmin([CONFLICT_409, _ok({"dateWeightList": [held]})])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        assert client.upload_body_composition(BC) == {"status": "duplicate"}
    reauth.assert_not_called()
    assert len(adapter.sent) == 2
    assert "/weight-service/weight/dayview/" in adapter.sent[1].url


@pytest.mark.parametrize("entries", [
    [],
    # Right weight, but a manual weigh-in hours away is not our upload.
    [{"samplePk": 1, "weight": 86200.0, "timestampGMT": _millis(BC_INSTANT + timedelta(hours=3))}],
    # Right time, wrong weight.
    [{"samplePk": 1, "weight": 90000.0, "timestampGMT": _millis(BC_INSTANT)}],
])
def test_upload_409_without_the_weigh_in_on_garmin_is_permanent(entries):
    from eufy_sync.sync import PermanentSyncError, _is_permanent

    garmin, _ = _real_garmin([CONFLICT_409, _ok({"dateWeightList": entries})])
    client = _client_on(garmin)
    with pytest.raises(PermanentSyncError, match="409") as exc:
        client.upload_body_composition(BC)
    assert _is_permanent(exc.value)


def test_upload_409_is_permanent_when_the_confirming_lookup_fails():
    from eufy_sync.sync import PermanentSyncError

    garmin, _ = _real_garmin([CONFLICT_409, (500, {"Content-Type": "application/json"}, b"{}")])
    client = _client_on(garmin)
    with pytest.raises(PermanentSyncError, match="409"):
        client.upload_body_composition(BC)


def test_upload_429_becomes_a_rate_limit_that_sync_does_not_retry():
    from garminconnect import GarminConnectTooManyRequestsError

    from eufy_sync.sync import _is_permanent

    garmin, adapter = _real_garmin([(429, {"Content-Type": "application/json", "Retry-After": "120"}, b"{}")])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(GarminConnectTooManyRequestsError) as exc:
            client.upload_body_composition(BC)
    reauth.assert_not_called()
    assert len(adapter.sent) == 1
    assert _is_permanent(exc.value)


@pytest.mark.parametrize("status", [400, 404, 413, 422])
def test_upload_other_4xx_is_a_permanent_bad_request(status):
    from eufy_sync.sync import PermanentSyncError, _is_permanent

    garmin, adapter = _real_garmin([(status, {"Content-Type": "application/json"}, b'{"message":"bad file"}')])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(PermanentSyncError, match=str(status)) as exc:
            client.upload_body_composition(BC)
    reauth.assert_not_called()
    assert len(adapter.sent) == 1
    assert _is_permanent(exc.value)


@pytest.mark.parametrize("status", [408, 500, 502, 503])
def test_upload_408_and_5xx_stay_transient_for_retry(status):
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import _is_permanent

    garmin, _ = _real_garmin([(status, {"Content-Type": "application/json"}, b"{}")])
    client = _client_on(garmin)
    with patch.object(client._auth, "silent_reauth") as reauth:
        with pytest.raises(GarminConnectConnectionError) as exc:
            client.upload_body_composition(BC)
    reauth.assert_not_called()
    assert not _is_permanent(exc.value)


def test_upload_network_failure_stays_transient():
    from garminconnect import GarminConnectConnectionError

    fake = MagicMock()
    fake.add_body_composition.side_effect = GarminConnectConnectionError("Connection error: timed out")
    client = _client_with_fake_garmin(fake)
    with pytest.raises(GarminConnectConnectionError, match="timed out"):
        client.upload_body_composition(BC)


def test_upload_401_after_a_successful_relogin_is_permanent():
    from garminconnect import GarminConnectConnectionError

    from eufy_sync.sync import PermanentSyncError

    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    fresh = MagicMock()
    fresh.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", return_value=fresh) as reauth:
        with pytest.raises(PermanentSyncError, match="--reauth garmin"):
            client.upload_body_composition(BC)
    reauth.assert_called_once()
    fresh.add_body_composition.assert_called_once()


# ---------------------------------------------------------------------------
# The pieces underneath
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("message, expected", [
    ("API Error 403 - ", 403),
    ("API Error 409 - Duplicate Activity.", 409),
    ("API call client error (403): API Error 403", 403),
    ("Connection error: timed out", None),
    ("weight 4031 kg", None),
])
def test_status_code_reads_the_library_messages(message, expected):
    from garminconnect import GarminConnectConnectionError
    assert garmin_client._status_code(GarminConnectConnectionError(message)) == expected


@pytest.mark.parametrize("response, blocked", [
    (CF_PAGE, True),
    (CF_MITIGATED, True),
    (JSON_403, False),   # Cloudflare headers alone prove nothing
    ((403, {"Content-Type": "text/html"}, b"<html>Forbidden</html>"), False),
    ((200, {"Content-Type": "text/html"}, b"Just a moment"), False),
])
def test_cloudflare_block_detection(response, blocked):
    recorder = garmin_client._LastResponse()
    recorder.record(_FakeCffiResponse(*response))
    assert recorder.is_cloudflare_block() is blocked


def test_watch_responses_installs_its_hook_once():
    garmin, _ = _real_garmin([])
    client = _client_on(garmin)
    client._watch_responses(garmin)
    hooks = garmin.client._api_session.hooks["response"]
    assert hooks.count(client._last_response.hook) == 1


def test_curl_cffi_multipart_upload_reaches_a_local_server_intact():
    """The maintainer's reason not to use curl_cffi for data calls is that it
    cannot do files=. It cannot, but CurlMime can: send a FIT-sized binary
    through the real fallback transport to a local server and parse it back."""
    import threading
    from email.parser import BytesParser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received["content_type"] = self.headers["Content-Type"]
            received["auth"] = self.headers["Authorization"]
            received["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload = bytes(range(256)) * 8
        session = garmin_client._ImpersonatingSession(garmin_client._LastResponse())
        resp = session.request(
            "POST", f"http://127.0.0.1:{server.server_port}/upload-service/upload",
            headers={"Authorization": f"Bearer {TOKEN}"},
            files={"file": ("body_composition.fit", payload)},
            timeout=10,
        )
    finally:
        server.shutdown()

    assert resp.status_code == 200 and resp.json() == {"ok": True}
    assert received["auth"] == f"Bearer {TOKEN}"
    message = BytesParser().parsebytes(
        b"Content-Type: " + received["content_type"].encode() + b"\r\n\r\n" + received["body"]
    )
    (part,) = message.get_payload()
    assert part.get_param("name", header="content-disposition") == "file"
    assert part.get_filename() == "body_composition.fit"
    assert part.get_payload(decode=True) == payload


def test_upload_passes_a_failed_relogins_own_error_through_unclassified():
    # A login that failed with an HTTP status is not Garmin refusing the
    # upload, so it must not be dressed up as "refused after a fresh login".
    from garminconnect import GarminConnectConnectionError

    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    login_failure = GarminConnectConnectionError("Mobile login failed: HTTP 403")
    client = _client_with_fake_garmin(dead)
    client._allow_interactive = False
    with patch.object(client._auth, "silent_reauth", side_effect=login_failure):
        with pytest.raises(GarminConnectConnectionError) as exc:
            client.upload_body_composition(BC)
    assert exc.value is login_failure
