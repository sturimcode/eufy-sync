from __future__ import annotations

import time
from datetime import date
from unittest.mock import MagicMock, patch

import httpx
import pytest

from eufy_sync.config import StravaConfig
from eufy_sync.strava_client import (
    STRAVA_API_BASE_NEW,
    STRAVA_API_BASE_OLD,
    StravaClient,
)


def _make_config():
    return StravaConfig(client_id="12345", client_secret="secret")


def _make_tokens(expired: bool = False):
    if expired:
        return {
            "access_token": "old_access",
            "refresh_token": "refresh_tok",
            "expires_at": time.time() - 3600,
        }
    return {
        "access_token": "valid_access",
        "refresh_token": "refresh_tok",
        "expires_at": time.time() + 3600,
    }


def test_token_status_valid():
    client = StravaClient(_make_config())
    with patch("eufy_sync.strava_client._load_tokens", return_value=_make_tokens()):
        status = client.token_status()
    assert status["state"] == "valid"
    assert "hours_remaining" in status


def test_token_status_refresh_needed():
    client = StravaClient(_make_config())
    with patch("eufy_sync.strava_client._load_tokens", return_value=_make_tokens(expired=True)):
        status = client.token_status()
    assert status["state"] == "refresh_needed"


def test_token_status_no_session():
    client = StravaClient(_make_config())
    with patch("eufy_sync.strava_client._load_tokens", return_value=None):
        status = client.token_status()
    assert status["state"] == "no_session"


def test_token_status_expired_no_refresh():
    client = StravaClient(_make_config())
    tokens = {"access_token": "old", "refresh_token": "", "expires_at": time.time() - 3600}
    with patch("eufy_sync.strava_client._load_tokens", return_value=tokens):
        status = client.token_status()
    assert status["state"] == "expired"


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_authenticate_with_valid_token(mock_load, mock_save):
    mock_load.return_value = _make_tokens()
    client = StravaClient(_make_config())
    client.authenticate()
    assert "Bearer valid_access" in client._client.headers.get("Authorization", "")
    client.close()


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_authenticate_refreshes_expired_token(mock_load, mock_save):
    mock_load.return_value = _make_tokens(expired=True)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "access_token": "new_access",
        "refresh_token": "new_refresh",
        "expires_at": time.time() + 21600,
    }

    client = StravaClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response):
        client.authenticate()

    assert "Bearer new_access" in client._client.headers.get("Authorization", "")
    mock_save.assert_called()
    client.close()


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_refresh_401_tells_user_to_reauthorize(mock_load, mock_save):
    """A 401/400 on refresh means the grant is dead - tell the user to
    re-authorize."""
    mock_load.return_value = _make_tokens(expired=True)

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"

    client = StravaClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response):
        try:
            client.authenticate()
            raise AssertionError("Should have raised")
        except RuntimeError as e:
            assert "Re-authorize" in str(e)
    client.close()


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_refresh_500_is_a_plain_retryable_failure(mock_load, mock_save):
    """A 5xx on refresh is transient - it must NOT tell the user to
    re-authorize, since their grant is probably still fine."""
    mock_load.return_value = _make_tokens(expired=True)

    mock_response = MagicMock()
    mock_response.status_code = 500
    mock_response.text = "Internal Server Error"

    client = StravaClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response):
        try:
            client.authenticate()
            raise AssertionError("Should have raised")
        except RuntimeError as e:
            assert "Re-authorize" not in str(e)
            assert "temporary" in str(e).lower() or "500" in str(e)
    client.close()


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_refresh_429_is_a_plain_retryable_failure(mock_load, mock_save):
    """A 429 on refresh is rate-limiting, not a dead grant."""
    mock_load.return_value = _make_tokens(expired=True)

    mock_response = MagicMock()
    mock_response.status_code = 429
    mock_response.text = "Too Many Requests"

    client = StravaClient(_make_config())
    with patch.object(client._client, "post", return_value=mock_response):
        try:
            client.authenticate()
            raise AssertionError("Should have raised")
        except RuntimeError as e:
            assert "Re-authorize" not in str(e)
    client.close()


def test_authorize_strava_socket_bind_failure_names_the_port():
    """If the local OAuth callback port is already in use, HTTPServer's bind
    raises a raw OSError. That must surface as a RuntimeError that names the
    port and suggests freeing it, not an unexplained 'Address already in
    use' traceback."""
    from eufy_sync.strava_client import CALLBACK_PORT, authorize_strava

    with patch("eufy_sync.strava_client.HTTPServer",
               side_effect=OSError(48, "Address already in use")):
        try:
            authorize_strava(_make_config())
            raise AssertionError("Should have raised")
        except RuntimeError as e:
            assert str(CALLBACK_PORT) in str(e)
            assert "already in use" in str(e).lower() or "close" in str(e).lower()


@patch("eufy_sync.strava_client._load_tokens", return_value=None)
def test_authenticate_raises_without_tokens(mock_load):
    client = StravaClient(_make_config())
    try:
        client.authenticate()
        raise AssertionError("Should have raised")
    except RuntimeError as e:
        assert "--setup-strava" in str(e)
    client.close()


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_update_weight(mock_load, mock_save):
    mock_load.return_value = _make_tokens()

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"weight": 86.2}

    client = StravaClient(_make_config())
    client.authenticate()

    with patch.object(client._client, "request", return_value=mock_response) as mock_request:
        result = client.update_weight(86.2)

    assert result == {"weight": 86.2}
    method, url = mock_request.call_args[0]
    assert method == "PUT"
    assert url.endswith("/athlete")
    client.close()


@patch("eufy_sync.strava_client._save_tokens")
@patch("eufy_sync.strava_client._load_tokens")
def test_update_weight_failure_raises(mock_load, mock_save):
    mock_load.return_value = _make_tokens()

    mock_response = MagicMock()
    mock_response.status_code = 401
    mock_response.text = "Unauthorized"

    client = StravaClient(_make_config())
    client.authenticate()

    with patch.object(client._client, "request", return_value=mock_response):
        try:
            client.update_weight(86.2)
            raise AssertionError("Should have raised")
        except RuntimeError as e:
            assert "401" in str(e)
    client.close()


def test_auth_url_encodes_every_query_value():
    # Built by string concatenation, the redirect URI and scope went out raw.
    # Browsers tolerated it, but the URL must be correct by construction.
    from urllib.parse import parse_qs, urlparse

    from eufy_sync.strava_client import REDIRECT_URI, STRAVA_AUTH_URL, _auth_url
    url = _auth_url("123", "st@te/value")
    assert url.startswith(STRAVA_AUTH_URL + "?")
    assert "redirect_uri=http%3A%2F%2Flocalhost" in url
    query = parse_qs(urlparse(url).query)
    assert query["client_id"] == ["123"]
    assert query["redirect_uri"] == [REDIRECT_URI]
    assert query["scope"] == ["profile:write,profile:read_all"]
    assert query["state"] == ["st@te/value"]
    assert query["approval_prompt"] == ["force"]


# Strava's API base moves to api-v3.strava.com on a published schedule.

BEFORE_NEW_HOST = date(2027, 1, 3)
NEW_HOST_DAY_ONE = date(2027, 1, 4)
LAST_OVERLAP_DAY = date(2027, 5, 31)
LONG_AFTER = date(2028, 3, 1)


def _client_on(day: date) -> StravaClient:
    return StravaClient(_make_config(), today=lambda: day)


def _ok(json_body=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = json_body or {"weight": 86.2}
    return resp


def _status(code: int):
    resp = MagicMock()
    resp.status_code = code
    resp.text = "error"
    return resp


def _urls(mock_request) -> list[str]:
    return [c[0][1] for c in mock_request.call_args_list]


@pytest.mark.parametrize("day, expected", [
    (date(2026, 10, 2), STRAVA_API_BASE_OLD),
    (BEFORE_NEW_HOST, STRAVA_API_BASE_OLD),
    (NEW_HOST_DAY_ONE, STRAVA_API_BASE_NEW),
    (LAST_OVERLAP_DAY, STRAVA_API_BASE_NEW),
    (LONG_AFTER, STRAVA_API_BASE_NEW),
])
def test_api_base_follows_the_published_schedule(day, expected):
    client = _client_on(day)
    with patch.object(client._client, "request", return_value=_ok()) as mock_request:
        client.update_weight(86.2)
    assert _urls(mock_request) == [f"{expected}/athlete"]
    client.close()


def test_new_host_is_api_v3_strava_com_without_www():
    assert STRAVA_API_BASE_NEW == "https://api-v3.strava.com"
    assert STRAVA_API_BASE_OLD == "https://www.strava.com/api/v3"


@pytest.mark.parametrize("failure", [
    httpx.ConnectError("[Errno 8] nodename nor servname provided"),  # DNS
    httpx.ConnectError("[Errno 61] Connection refused"),
    httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),  # TLS
    httpx.ConnectTimeout("timed out connecting"),
])
def test_overlap_falls_back_to_old_host_when_new_host_is_unreachable(failure):
    client = _client_on(NEW_HOST_DAY_ONE)
    with patch.object(client._client, "request", side_effect=[failure, _ok()]) as mock_request:
        assert client.update_weight(86.2) == {"weight": 86.2}
    assert _urls(mock_request) == [
        f"{STRAVA_API_BASE_NEW}/athlete",
        f"{STRAVA_API_BASE_OLD}/athlete",
    ]
    client.close()


def test_fallback_is_per_call_and_the_next_call_tries_the_new_host_again():
    client = _client_on(NEW_HOST_DAY_ONE)
    responses = [httpx.ConnectError("refused"), _ok(), _ok()]
    with patch.object(client._client, "request", side_effect=responses) as mock_request:
        client.update_weight(86.2)
        client.update_weight(86.0)
    assert _urls(mock_request) == [
        f"{STRAVA_API_BASE_NEW}/athlete",
        f"{STRAVA_API_BASE_OLD}/athlete",
        f"{STRAVA_API_BASE_NEW}/athlete",
    ]
    client.close()


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_overlap_does_not_fall_back_on_http_errors(code):
    """An HTTP status means the new host answered; retrying elsewhere would
    only hide a real auth, rate-limit or server problem."""
    client = _client_on(NEW_HOST_DAY_ONE)
    with patch.object(client._client, "request", return_value=_status(code)) as mock_request:
        with pytest.raises(RuntimeError, match=str(code)):
            client.update_weight(86.2)
    assert _urls(mock_request) == [f"{STRAVA_API_BASE_NEW}/athlete"]
    client.close()


def test_overlap_does_not_fall_back_on_read_timeout():
    """A read timeout may mean the PUT already reached Strava."""
    client = _client_on(NEW_HOST_DAY_ONE)
    with patch.object(client._client, "request", side_effect=httpx.ReadTimeout("slow")) as mock_request:
        with pytest.raises(httpx.ReadTimeout):
            client.update_weight(86.2)
    assert _urls(mock_request) == [f"{STRAVA_API_BASE_NEW}/athlete"]
    client.close()


def test_connection_failure_before_the_switch_is_not_retried():
    client = _client_on(BEFORE_NEW_HOST)
    with patch.object(client._client, "request", side_effect=httpx.ConnectError("refused")) as mock_request:
        with pytest.raises(httpx.ConnectError):
            client.update_weight(86.2)
    assert _urls(mock_request) == [f"{STRAVA_API_BASE_OLD}/athlete"]
    client.close()


def test_old_host_stays_as_fallback_with_no_end_date():
    client = _client_on(LONG_AFTER)
    with patch.object(client._client, "request", side_effect=[httpx.ConnectError("refused"), _ok()]) as mock_request:
        client.update_weight(86.2)
    assert _urls(mock_request) == [f"{STRAVA_API_BASE_NEW}/athlete", f"{STRAVA_API_BASE_OLD}/athlete"]
    client.close()


def test_both_hosts_unreachable_raises_the_old_host_error():
    client = _client_on(NEW_HOST_DAY_ONE)
    failures = [httpx.ConnectError("new down"), httpx.ConnectError("old down")]
    with patch.object(client._client, "request", side_effect=failures):
        with pytest.raises(httpx.ConnectError, match="old down"):
            client.update_weight(86.2)
    client.close()


def test_check_connection_uses_the_same_fallback():
    client = _client_on(NEW_HOST_DAY_ONE)
    responses = [httpx.ConnectError("refused"), _ok({"id": 1})]
    with patch.object(client._client, "request", side_effect=responses) as mock_request:
        client.check_connection()
    assert [c[0][0] for c in mock_request.call_args_list] == ["GET", "GET"]
    assert _urls(mock_request)[-1] == f"{STRAVA_API_BASE_OLD}/athlete"
    client.close()


def test_oauth_urls_stay_on_www_strava_com_after_the_move():
    from eufy_sync.strava_client import STRAVA_AUTH_URL, STRAVA_TOKEN_URL
    assert STRAVA_AUTH_URL == "https://www.strava.com/oauth/authorize"
    assert STRAVA_TOKEN_URL == "https://www.strava.com/oauth/token"
