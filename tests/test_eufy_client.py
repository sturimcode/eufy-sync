from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
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


def test_fetch_merges_new_raw_readings_and_prefers_processed_duplicates():
    c = _client(customer_id="a")
    raws = [_raw_wifi_record("a", 80.0, t) for t in (2_000_000_000, 2_000_086_400)]
    with patch.object(c, "_get_records", return_value=[_record("a", 800, 2_000_000_000)]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=raws):
        measurements = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert len(measurements) == 2
    by_id = {m.measurement_id: m for m in measurements}
    assert by_id["a_2000000000"].weight_only is False
    assert by_id["a_2000086400"].weight_only is True


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


@pytest.mark.parametrize("other_time", [1_000, 2_000_000_000])
def test_raw_fallback_requires_selection_even_for_an_older_second_profile(other_time):
    c = _client()
    raws = [_raw_wifi_record("a", 80.0, 2_000_000_000), _raw_wifi_record("b", 60.0, other_time)]
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=raws) as get_raw:
        with pytest.raises(AmbiguousProfileError) as exc:
            c.fetch_measurements(after_timestamp=1_500_000_000)
    assert {p.customer_id for p in exc.value.profiles} == {"a", "b"}
    get_raw.assert_called_once_with("dev1", None)


def test_raw_fallback_checks_processed_history_for_other_profiles():
    c = _client()
    with patch.object(c, "_get_records", return_value=[_record("a", 800, 1_000)]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=[_raw_wifi_record("b", 60.0, 2_000_000_000)]):
        with pytest.raises(AmbiguousProfileError) as exc:
            c.fetch_measurements(after_timestamp=1_500_000_000)
    assert {p.customer_id for p in exc.value.profiles} == {"a", "b"}


def test_single_raw_profile_is_filtered_to_requested_window():
    c = _client()
    raws = [_raw_wifi_record("a", 81.0, 1_000), _raw_wifi_record("a", 80.0, 2_000_000_000)]
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=raws):
        result = c.fetch_measurements(after_timestamp=1_500_000_000)
    assert [m.weight_kg for m in result] == [80.0]


def test_unassigned_raw_weight_is_not_synced():
    c = _client()
    with patch.object(c, "_get_records", return_value=[]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=[_raw_wifi_record(None, 80.0, 2_000_000_000)]):
        assert c.fetch_measurements() == []


def test_list_profiles_includes_raw_profiles_even_with_existing_selection():
    c = _client(customer_id="a")
    raws = [_raw_wifi_record("a", 81.0, 200), _raw_wifi_record("b", 60.0, 300)]
    with patch.object(c, "_get_records", return_value=[_record("a", 800, 100)]), \
         patch.object(c, "_list_device_ids", return_value=["dev1"]), \
         patch.object(c, "_get_raw_records", return_value=raws):
        profiles = c.list_profiles()
    assert {p.customer_id for p in profiles} == {"a", "b"}
    assert next(p for p in profiles if p.customer_id == "a").last_weight_kg == 81.0


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


# ---------------------------------------------------------------------------
# Login host fallback: EufyLife 3.3.12 logs in at home-api.eufylife.com. Use it
# only when the original endpoint looks moved or gone, never after a rejected
# password or a rate limit.
# ---------------------------------------------------------------------------

PRIMARY_LOGIN = "https://api.eufylife.com/v1/user/v2/email/login"
FALLBACK_LOGIN = "https://home-api.eufylife.com/v1/user/v2/email/login/"


def _login_client():
    c = EufyClient.__new__(EufyClient)
    c.config = EufyConfig(email="e@example.com", password="pw")
    c.access_token = None
    c.user_id = None
    c._client = MagicMock()
    c._save_token = MagicMock()
    return c


def _http_resp(status_code, json_body=None, *, url=PRIMARY_LOGIN):
    request = httpx.Request("POST", url)
    if json_body is None:
        return httpx.Response(status_code, text="<html>Not Found</html>", request=request)
    return httpx.Response(status_code, json=json_body, request=request)


def _login_ok(token="tok-1", user_id="uid-1", **extra):
    return {"res_code": 1, "access_token": token, "user_id": user_id, **extra}


def _posted_urls(c):
    return [call.args[0] for call in c._client.post.call_args_list]


def test_login_primary_success_does_not_fall_back():
    c = _login_client()
    c._client.post.return_value = _http_resp(200, _login_ok(expires_in=86400))
    c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN]
    assert (c.access_token, c.user_id) == ("tok-1", "uid-1")
    c._save_token.assert_called_once_with(86400)


@pytest.mark.parametrize("status", [404, 410])
def test_login_primary_gone_falls_back_to_home_api(status):
    c = _login_client()
    c._client.post.side_effect = [
        _http_resp(status),
        _http_resp(200, _login_ok("tok-2", "uid-2"), url=FALLBACK_LOGIN),
    ]
    c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN, FALLBACK_LOGIN]
    assert (c.access_token, c.user_id) == ("tok-2", "uid-2")

    fallback = c._client.post.call_args_list[1]
    assert fallback.kwargs["headers"]["User-Agent"] == "EufyLife-Android-3.3.12"
    assert fallback.kwargs["headers"]["Country"] == "US"
    assert fallback.kwargs["headers"]["Category"] == "Health"
    assert fallback.kwargs["json"]["ab"] == "us"
    assert fallback.kwargs["json"]["email"] == "e@example.com"


def test_login_connection_failure_falls_back():
    c = _login_client()
    c._client.post.side_effect = [
        httpx.ConnectError("Name or service not known"),
        _http_resp(200, _login_ok(), url=FALLBACK_LOGIN),
    ]
    c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN, FALLBACK_LOGIN]


def test_login_non_json_200_falls_back():
    c = _login_client()
    c._client.post.side_effect = [
        _http_resp(200),  # HTML body
        _http_resp(200, _login_ok(), url=FALLBACK_LOGIN),
    ]
    c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN, FALLBACK_LOGIN]


def test_login_deprecated_message_falls_back():
    c = _login_client()
    c._client.post.side_effect = [
        _http_resp(200, {"res_code": 26050, "message": "This API is deprecated, please upgrade the app"}),
        _http_resp(200, _login_ok(), url=FALLBACK_LOGIN),
    ]
    c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN, FALLBACK_LOGIN]


def test_login_wrong_password_does_not_fall_back():
    from eufy_sync.sync import PermanentSyncError
    c = _login_client()
    c._client.post.return_value = _http_resp(200, {"res_code": 26006, "message": "Incorrect password"})
    with pytest.raises(PermanentSyncError):
        c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN]
    c._save_token.assert_not_called()


@pytest.mark.parametrize("status", [401, 403])
def test_login_auth_rejected_status_does_not_fall_back(status):
    c = _login_client()
    c._client.post.return_value = _http_resp(status, {"res_code": 0})
    with pytest.raises(httpx.HTTPStatusError):
        c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN]


def test_login_rate_limited_does_not_fall_back():
    c = _login_client()
    c._client.post.return_value = _http_resp(429, {"res_code": 0, "message": "Too many requests"})
    with pytest.raises(httpx.HTTPStatusError):
        c._fresh_login()
    assert _posted_urls(c) == [PRIMARY_LOGIN]


def test_login_fallback_wrong_password_raises_permanent_error():
    from eufy_sync.sync import PermanentSyncError
    c = _login_client()
    c._client.post.side_effect = [
        _http_resp(404),
        _http_resp(200, {"res_code": 26006, "message": "Incorrect password"}, url=FALLBACK_LOGIN),
    ]
    with pytest.raises(PermanentSyncError):
        c._fresh_login()
    assert len(_posted_urls(c)) == 2


def test_home_api_response_shape_parses():
    """Field names from the EufyLife 3.3.12 login response, as read by
    m4ary/eufylife-api-hacs and osjayaprakash/eufylife-scale-mcp."""
    c = _login_client()
    home_api_body = {
        "res_code": 1,
        "message": "success",
        "access_token": "home-tok",
        "user_id": "home-uid",
        "expires_in": 2592000,
        "user_center_id": "center-id",
        "user_center_token": "center-tok",
        "device_id": "phone-id",
        "customers": [{"id": "cust-a", "name": "A"}, {"id": "cust-b", "name": "B"}],
    }
    c._client.post.side_effect = [_http_resp(404), _http_resp(200, home_api_body, url=FALLBACK_LOGIN)]
    c._fresh_login()
    # The data endpoints take the login access_token and user_id, not user_center_token.
    assert c.access_token == "home-tok"
    assert c.user_id == "home-uid"
    c._save_token.assert_called_once_with(2592000)


def test_login_response_nested_under_data_parses_and_defaults_ttl():
    c = _login_client()
    c._client.post.return_value = _http_resp(
        200, {"res_code": 1, "data": {"access_token": "n-tok", "user_id": 12345}},
    )
    c._fresh_login()
    assert (c.access_token, c.user_id) == ("n-tok", "12345")
    c._save_token.assert_called_once_with(2592000)


def test_login_logs_host_without_secrets(caplog):
    c = _login_client()
    c._client.post.side_effect = [_http_resp(404), _http_resp(200, _login_ok("secret-tok"), url=FALLBACK_LOGIN)]
    with caplog.at_level("DEBUG", logger="eufy_sync.eufy_client"):
        c._fresh_login()
    assert "home-api.eufylife.com" in caplog.text
    assert "secret-tok" not in caplog.text
    assert "pw" not in caplog.text.split()
