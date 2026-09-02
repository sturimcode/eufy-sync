from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from eufy_sync.config import EufyConfig
from eufy_sync.eufy_client import AmbiguousProfileError, EufyClient, EufyProfile


def test_parse_record_basic():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc123",
        "device_id": "dev789",
        "update_time": 1711900000,
        "create_time": 1711900000,
        "scale_data": {
            "weight": 862,  # 0.1 kg units -> 86.2 kg
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


# ---------------------------------------------------------------------------
# Issue #56: Eufy rewrites update_time server-side in bulk, which collapsed a
# whole backfill onto one date. create_time survives those rewrites.
# ---------------------------------------------------------------------------

def test_parse_record_prefers_create_time_over_update_time():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc123",
        "device_id": "dev789",
        "create_time": 1711900000,   # the weigh-in
        "update_time": 1750000000,   # a later server-side rewrite
        "scale_data": {"weight": 862},
    }
    m = client._parse_record(record)
    assert m.timestamp == datetime(2024, 3, 31, 15, 46, 40, tzinfo=timezone.utc)
    assert m.measurement_id == "abc123_1711900000"


def test_parse_record_falls_back_to_update_time_when_create_time_missing():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc123",
        "update_time": 1711900000,
        "scale_data": {"weight": 862},
    }
    m = client._parse_record(record)
    assert m.timestamp == datetime(2024, 3, 31, 15, 46, 40, tzinfo=timezone.utc)
    assert m.measurement_id == "abc123_1711900000"


def test_parse_record_falls_back_to_update_time_when_create_time_is_zero():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc123",
        "create_time": 0,
        "update_time": 1711900000,
        "scale_data": {"weight": 862},
    }
    m = client._parse_record(record)
    assert m.timestamp == datetime(2024, 3, 31, 15, 46, 40, tzinfo=timezone.utc)
    assert m.measurement_id == "abc123_1711900000"


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
# Shared record and client helpers
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


def _raw_wifi_record(customer_id, weight_kg, timestamp):
    return {
        "id": "raw-record-id",
        "weight": f"{weight_kg:.2f}",
        "impedance": "112566",
        "timestamp": str(timestamp),
        "heart_rate": "0",
        "customer_id": customer_id,
        "device_id": "raw-device-id",
        "user_id": "raw-user-id",
        "product_code": "",
    }


# ---------------------------------------------------------------------------
# list_profiles
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
# fetch_measurements filtering and AmbiguousProfileError
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


def test_fetch_window_selects_by_weigh_in_time_not_update_time():
    """The client-side cutoff filters on the measurement timestamp, which is
    create_time (issue #56). A years-old weigh-in that Eufy rewrote yesterday
    must stay outside a recent window, so --backfill-days and --repair-days
    cover the dates the user means."""
    c = _client()
    rewritten = {
        "customer_id": "a",
        "device_id": "d",
        "create_time": 1_000_000_000,   # year 2001 weigh-in
        "update_time": 2_000_000_000,   # rewritten in 2033
        "scale_data": {"weight": 800},
    }
    recent = {
        "customer_id": "a",
        "device_id": "d",
        "create_time": 1_900_000_000,
        "update_time": 2_000_000_000,
        "scale_data": {"weight": 810},
    }
    with patch.object(c, "_get_records", return_value=[rewritten, recent]):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert [m.weight_kg for m in measurements] == [81.0]


def test_fetch_configured_profile_forwards_after_timestamp():
    from unittest.mock import MagicMock
    c = _client(customer_id="a")
    mock = MagicMock(return_value=[_record("a", 800, 2_000_000_000)])
    with patch.object(c, "_get_records", mock):
        c.fetch_measurements(after_timestamp=1_500_000_000)
    mock.assert_called_once_with(1_500_000_000)


# ---------------------------------------------------------------------------
# _list_device_ids and _get_raw_records
# ---------------------------------------------------------------------------

def _resp(status_code, json_body):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    return r


def test_list_device_ids_parses_device_v2():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(
        200, {"res_code": 1, "devices": [{"id": "dev1"}, {"id": "dev2"}, {"id": ""}]}
    )
    assert c._list_device_ids() == ["dev1", "dev2"]


def test_list_device_ids_empty_on_error_code():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(200, {"res_code": 0, "devices": []})
    assert c._list_device_ids() == []


def test_get_raw_records_extracts_list():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(200, {"res_code": 1, "list": [_record("a", 800, 100)]})
    recs = c._get_raw_records("dev1", None)
    assert len(recs) == 1
    assert recs[0]["customer_id"] == "a"


def test_get_raw_records_handles_null_list_500_and_bad_code():
    c = _client()
    c._client = MagicMock()
    c._client.get.return_value = _resp(200, {"res_code": 1, "list": None})
    assert c._get_raw_records("d", None) == []
    c._client.get.return_value = _resp(500, {})
    assert c._get_raw_records("d", None) == []
    c._client.get.return_value = _resp(200, {"res_code": 500, "message": "unavailable"})
    assert c._get_raw_records("d", None) == []


# ---------------------------------------------------------------------------
# Raw Wi-Fi fallback orchestration
# ---------------------------------------------------------------------------

def test_fetch_falls_back_to_raw_when_normal_empty():
    c = _client(customer_id="a")
    raw = _raw_wifi_record("a", 80.0, 2_000_000_000)  # year 2033, passes the window
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=[raw]):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 1
    assert measurements[0].weight_kg == 80.0
    assert measurements[0].customer_id == "a"
    assert measurements[0].body_fat_pct is None


def test_fetch_does_not_use_raw_when_normal_has_data():
    c = _client(customer_id="a")
    fallback_probe = MagicMock()
    with patch.object(c, "_get_records", return_value=[_record("a", 800, 2_000_000_000)]), \
         patch.object(c, "_fetch_raw_measurements", fallback_probe):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 1
    fallback_probe.assert_not_called()


def test_raw_fallback_drops_other_profiles():
    c = _client(customer_id="a")
    raws = [
        _raw_wifi_record("a", 80.0, 2_000_000_000),
        _raw_wifi_record("b", 60.0, 2_000_000_000),
    ]
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=raws):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert {m.customer_id for m in measurements} == {"a"}


def test_raw_fallback_degrades_when_device_list_errors():
    c = _client(customer_id="a")
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", side_effect=RuntimeError("boom")):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert measurements == []


def test_raw_fallback_degrades_on_malformed_record():
    c = _client(customer_id="a")
    bad = {"customer_id": "a", "timestamp": "2_000_000_000", "weight": "heavy"}
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=[bad]):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert measurements == []


# ---------------------------------------------------------------------------
# Issue #48: raw weight-only records must be distinguishable from processed
# ones so sync can upgrade them when the full body comp arrives later.
# ---------------------------------------------------------------------------

def test_raw_wifi_measurement_is_marked_weight_only():
    c = _client()
    m = c._parse_raw_wifi_record(_raw_wifi_record("a", 80.0, 2_000_000_000))
    assert m.weight_only is True


def test_processed_measurement_is_not_weight_only():
    c = _client()
    m = c._parse_record(_record("a", 800, 100))
    assert m.weight_only is False
