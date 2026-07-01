from __future__ import annotations

from datetime import datetime, timezone
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


def test_upload_raises_permanent_on_auth_error_when_headless():
    from garminconnect import GarminConnectAuthenticationError
    from eufy_sync.sync import PermanentSyncError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectAuthenticationError("dead")
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with pytest.raises(PermanentSyncError):
        client.upload_body_composition(bc)


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


def test_upload_raises_permanent_with_reauth_hint_on_401_when_headless():
    from garminconnect import GarminConnectConnectionError
    from eufy_sync.sync import PermanentSyncError
    client = GarminClient(GarminConfig(email="g@example.com", password="pw"))
    dead = MagicMock()
    dead.add_body_composition.side_effect = GarminConnectConnectionError("API Error 401 - ")
    client._garmin = dead
    client._allow_interactive = False
    bc = GarminBodyComposition(timestamp="2026-06-10T08:00:00+00:00", weight=80.0)
    with pytest.raises(PermanentSyncError) as exc:
        client.upload_body_composition(bc)
    assert "--reauth" in str(exc.value)  # actionable message the CLI notification keys on


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
