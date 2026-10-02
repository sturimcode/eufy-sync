"""IntervalsClient against a mocked httpx transport. No request leaves the
machine: every response comes from the handler in each test."""
from __future__ import annotations

import base64
import json
from datetime import date

import httpx
import pytest

from eufy_sync.config import IntervalsConfig
from eufy_sync.intervals_client import IntervalsClient
from eufy_sync.sync import PermanentSyncError, RetryNextRunError, _is_permanent, _retry


def _client(handler, athlete_id: str = "i12345", api_key: str = "test-key") -> IntervalsClient:
    return IntervalsClient(
        IntervalsConfig(athlete_id=athlete_id, api_key=api_key),
        transport=httpx.MockTransport(handler),
    )


def _recorder(status: int = 200, body=None):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body if body is not None else {})

    return seen, handler


def test_update_wellness_puts_weight_and_body_fat_to_the_local_date():
    seen, handler = _recorder(body={"id": "2026-09-30", "weight": 80.12, "bodyFat": 18.3, "hrv": 60})
    client = _client(handler)

    result = client.update_wellness(date(2026, 9, 30), 80.1234, 18.26)

    (request,) = seen
    assert request.method == "PUT"
    assert str(request.url) == "https://intervals.icu/api/v1/athlete/i12345/wellness/2026-09-30"
    assert json.loads(request.content) == {"weight": 80.12, "bodyFat": 18.3}
    # Only what was sent is kept, never the rest of the wellness record.
    assert result == {"date": "2026-09-30", "weight": 80.12, "bodyFat": 18.3}
    client.close()


def test_requests_use_basic_auth_with_the_api_key_username():
    seen, handler = _recorder()
    client = _client(handler, api_key="secret-key")

    client.update_wellness(date(2026, 9, 30), 80.0)

    expected = base64.b64encode(b"API_KEY:secret-key").decode()
    assert seen[0].headers["Authorization"] == f"Basic {expected}"
    client.close()


@pytest.mark.parametrize("body_fat", [None, float("nan"), float("inf")])
def test_missing_body_fat_is_left_out_of_the_payload(body_fat):
    seen, handler = _recorder()
    client = _client(handler)

    result = client.update_wellness(date(2026, 9, 30), 80.0, body_fat)

    assert json.loads(seen[0].content) == {"weight": 80.0}
    assert "bodyFat" not in result
    client.close()


def test_athlete_id_in_the_path_is_escaped():
    seen, handler = _recorder()
    client = _client(handler, athlete_id="i1/../../x")

    client.update_wellness(date(2026, 9, 30), 80.0)

    assert seen[0].url.raw_path.decode() == "/api/v1/athlete/i1%2F..%2F..%2Fx/wellness/2026-09-30"
    client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_permanent_and_names_setup(status):
    _, handler = _recorder(status=status)
    client = _client(handler)

    with pytest.raises(PermanentSyncError, match="--setup-intervals"):
        client.update_wellness(date(2026, 9, 30), 80.0)
    client.close()


def test_rate_limit_waits_for_the_next_run():
    calls, handler = _recorder(status=429)
    client = _client(handler)

    with pytest.raises(RetryNextRunError, match="429"):
        _retry(lambda: client.update_wellness(date(2026, 9, 30), 80.0), "test")

    # Not retried within the run, and still queued for the next one.
    assert len(calls) == 1
    client.close()


def test_rate_limit_is_not_classified_permanent():
    _, handler = _recorder(status=429)
    client = _client(handler)
    with pytest.raises(RetryNextRunError) as exc:
        client.update_wellness(date(2026, 9, 30), 80.0)
    assert not _is_permanent(exc.value)
    client.close()


@pytest.mark.parametrize("status", [400, 404, 422])
def test_other_client_errors_are_permanent_without_setup_hint(status):
    _, handler = _recorder(status=status)
    client = _client(handler)

    with pytest.raises(PermanentSyncError) as exc:
        client.update_wellness(date(2026, 9, 30), 80.0)
    assert f"HTTP {status}" in str(exc.value)
    assert "--setup-intervals" not in str(exc.value)
    client.close()


@pytest.mark.parametrize("status", [500, 502, 503])
def test_server_errors_are_retryable(status):
    _, handler = _recorder(status=status)
    client = _client(handler)

    with pytest.raises(RuntimeError) as exc:
        client.update_wellness(date(2026, 9, 30), 80.0)
    assert not _is_permanent(exc.value)
    assert not isinstance(exc.value, RetryNextRunError)
    client.close()


def test_network_errors_are_retryable():
    def handler(request):
        raise httpx.ConnectError("All connection attempts failed", request=request)

    client = _client(handler)
    with pytest.raises(httpx.ConnectError) as exc:
        client.update_wellness(date(2026, 9, 30), 80.0)
    assert not _is_permanent(exc.value)
    client.close()


def test_athlete_id_resolves_the_keys_own_athlete():
    seen, handler = _recorder(body={"athlete": {"id": "i777", "name": "Someone"}})
    client = _client(handler, athlete_id="0")

    assert client.athlete_id() == "i777"
    assert seen[0].method == "GET"
    assert str(seen[0].url) == "https://intervals.icu/api/v1/athlete/0/profile"
    client.close()


@pytest.mark.parametrize("body", [{}, {"athlete": {}}, {"athlete": {"id": ""}}, [], {"athlete": "i1"}])
def test_profile_without_an_id_is_an_error(body):
    _, handler = _recorder(body=body)
    client = _client(handler)
    with pytest.raises(RuntimeError, match="athlete id"):
        client.athlete_id()
    client.close()


def test_check_connection_reads_the_configured_athlete():
    seen, handler = _recorder(body={"athlete": {"id": "i12345"}})
    client = _client(handler)

    client.check_connection()

    assert str(seen[0].url) == "https://intervals.icu/api/v1/athlete/i12345/profile"
    client.close()


def test_check_connection_refuses_a_key_for_another_athlete():
    _, handler = _recorder(body={"athlete": {"id": "i999"}})
    client = _client(handler)

    with pytest.raises(PermanentSyncError, match="i999.*--setup-intervals"):
        client.check_connection()
    client.close()


@pytest.mark.parametrize("status", [401, 403])
def test_check_connection_rejected_key_names_setup(status):
    _, handler = _recorder(status=status)
    client = _client(handler)
    with pytest.raises(PermanentSyncError, match="--setup-intervals"):
        client.check_connection()
    client.close()


def test_authenticate_refuses_an_empty_key_without_a_request():
    calls, handler = _recorder()
    client = _client(handler, api_key="")
    with pytest.raises(PermanentSyncError, match="--setup-intervals"):
        client.authenticate()
    assert calls == []
    client.close()


def test_authenticate_makes_no_request():
    calls, handler = _recorder()
    client = _client(handler)
    client.authenticate()
    assert calls == []
    client.close()
