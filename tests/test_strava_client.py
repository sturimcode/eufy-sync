from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from eufy_sync.config import StravaConfig
from eufy_sync.strava_client import StravaClient


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

    with patch.object(client._client, "put", return_value=mock_response) as mock_put:
        result = client.update_weight(86.2)

    assert result == {"weight": 86.2}
    call_kwargs = mock_put.call_args
    assert "athlete" in call_kwargs[0][0]
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

    with patch.object(client._client, "put", return_value=mock_response):
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
