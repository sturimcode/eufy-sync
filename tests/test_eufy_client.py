from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from eufy_sync.config import EufyConfig
from eufy_sync.eufy_client import AmbiguousProfileError, EufyClient, EufyMeasurement, EufyProfile


def test_parse_record_basic():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc123",
        "device_id": "dev789",
        "update_time": 1711900000,
        "create_time": 1711900000,
        "scale_data": {
            "weight": 862,  # decigrams -> 86.2 kg
            "body_fat": 18.5,
            "muscle_mass": 45.2,
            "water": 55.3,
            "bone_mass": 3.2,
            "bmr": 1650,
            "visceral_fat": 8.0,
            "body_age": 28,
            "bmi": 23.1,
        },
    }
    m = client._parse_record(record)
    assert m is not None
    assert m.weight_kg == 86.2  # 862 / 10
    assert m.measurement_id == "abc123_1711900000"
    assert m.customer_id == "abc123"
    assert m.body_fat_pct == 18.5
    assert m.metabolic_age == 28
    assert m.timestamp == datetime(2024, 3, 31, 15, 46, 40, tzinfo=timezone.utc)


def test_parse_record_missing_scale_data():
    client = EufyClient.__new__(EufyClient)
    record = {"customer_id": "abc", "update_time": 100}
    assert client._parse_record(record) is None


def test_parse_record_zero_weight():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc",
        "update_time": 100,
        "scale_data": {"weight": 0},
    }
    assert client._parse_record(record) is None


# ---------------------------------------------------------------------------
# Helpers shared by Task 2 and Task 3 tests
# ---------------------------------------------------------------------------

def _client(customer_id=None):
    c = EufyClient.__new__(EufyClient)
    c.config = EufyConfig(email="e@example.com", password="pw", customer_id=customer_id)
    c.access_token = "tok"
    c.user_id = "uid"
    return c


def _record(customer_id, weight_dg, update_time):
    return {
        "customer_id": customer_id,
        "device_id": "d",
        "update_time": update_time,
        "scale_data": {"weight": weight_dg},
    }


# ---------------------------------------------------------------------------
# Task 2: list_profiles
# ---------------------------------------------------------------------------

def test_list_profiles_groups_by_customer_id_newest_first():
    c = _client()
    records = [_record("a", 800, 100), _record("a", 810, 200), _record("b", 600, 150)]
    with patch.object(c, "_get_records", return_value=records):
        profiles = c.list_profiles()
    assert {p.customer_id for p in profiles} == {"a", "b"}
    a = next(p for p in profiles if p.customer_id == "a")
    assert a.last_weight_kg == 81.0  # most recent record for "a" (810 -> 81.0)
    assert profiles[0].last_measured >= profiles[1].last_measured  # newest first
    assert isinstance(profiles[0], EufyProfile)


# ---------------------------------------------------------------------------
# Task 3: fetch_measurements filtering and AmbiguousProfileError
# ---------------------------------------------------------------------------

def test_fetch_filters_to_configured_profile():
    c = _client(customer_id="a")
    records = [_record("a", 800, 100), _record("b", 600, 150)]
    with patch.object(c, "_get_records", return_value=records):
        measurements = c.fetch_measurements()
    assert {m.customer_id for m in measurements} == {"a"}


def test_fetch_single_profile_returns_all_when_unconfigured():
    c = _client()
    records = [_record("a", 800, 100), _record("a", 810, 200)]
    with patch.object(c, "_get_records", return_value=records):
        measurements = c.fetch_measurements()
    assert len(measurements) == 2


def test_fetch_raises_ambiguous_when_multiple_profiles_unconfigured():
    c = _client()
    records = [_record("a", 800, 100), _record("b", 600, 150)]
    with patch.object(c, "_get_records", return_value=records):
        with pytest.raises(AmbiguousProfileError) as exc_info:
            c.fetch_measurements()
    assert {p.customer_id for p in exc_info.value.profiles} == {"a", "b"}


def test_fetch_single_profile_windowed_by_after_timestamp():
    c = _client()
    old = _record("a", 800, 1_000)              # long ago
    new = _record("a", 810, 2_000_000_000)      # year 2033
    with patch.object(c, "_get_records", return_value=[old, new]):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 1
    assert measurements[0].weight_kg == 81.0


def test_fetch_configured_profile_forwards_after_timestamp():
    from unittest.mock import MagicMock
    c = _client(customer_id="a")
    mock = MagicMock(return_value=[_record("a", 800, 2_000_000_000)])
    with patch.object(c, "_get_records", mock):
        c.fetch_measurements(after_timestamp=1_500_000_000)
    mock.assert_called_once_with(1_500_000_000)
