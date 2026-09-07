from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from eufy_sync.sync import PermanentSyncError
from eufy_sync.zwift_client import WRITE_URL, ZwiftClient

PROFILE = {
    "id": 123, "weight": 89450, "firstName": "Private",
    "emailAddress": "private@example.com", "privacy": {"displayWeight": False},
    "height": 1800, "ftp": 250, "useMetric": True,
}


def config():
    return SimpleNamespace(email="private@example.com", password="password-secret")


def cached(*, expired=False, email="private@example.com"):
    return {
        "access_token": "old-access", "refresh_token": "old-refresh",
        "expires_at": time.time() + (-10 if expired else 3600),
        "email": email, "profile_id": "123",
    }


def client_with(handler):
    client = ZwiftClient(config())
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def test_expired_session_refreshes_rotated_tokens_and_verifies_identity():
    saved = []
    posts = []

    def handler(request):
        if request.method == "POST":
            posts.append(request.content.decode())
            return httpx.Response(200, json={"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200})
        return httpx.Response(200, json=PROFILE)

    client = client_with(handler)
    with patch("eufy_sync.zwift_client._load_tokens", return_value=cached(expired=True)), patch("eufy_sync.zwift_client._save_tokens", side_effect=saved.append):
        client.authenticate()
    assert len(posts) == 1 and "grant_type=refresh_token" in posts[0]
    assert saved[0]["refresh_token"] == "new-refresh"
    assert saved[0]["profile_id"] == "123"
    client.close()


def test_revoked_refresh_falls_back_to_password_once_but_500_does_not():
    for refresh_status, password_calls, error in ((401, 1, None), (500, 0, RuntimeError)):
        calls = {"refresh": 0, "password": 0}

        def handler(request, status=refresh_status, state=calls):
            if request.method == "GET":
                return httpx.Response(200, json=PROFILE)
            body = request.content.decode()
            if "grant_type=refresh_token" in body:
                state["refresh"] += 1
                return httpx.Response(status, json={"error": "invalid_grant", "detail": "password-secret"})
            state["password"] += 1
            return httpx.Response(200, json={"access_token": "fresh", "refresh_token": "fresh-refresh", "expires_in": 3600})

        client = client_with(handler)
        contexts = (patch("eufy_sync.zwift_client._load_tokens", return_value=cached(expired=True)), patch("eufy_sync.zwift_client._save_tokens"))
        with contexts[0], contexts[1]:
            if error:
                with pytest.raises(error) as exc:
                    client.authenticate()
                assert "password-secret" not in str(exc.value)
            else:
                client.authenticate()
        assert calls == {"refresh": 1, "password": password_calls}
        client.close()


def test_wrong_cached_account_binding_is_permanent_without_network():
    def handler(request):
        raise AssertionError("wrong-account token must not be used")

    client = client_with(handler)
    with patch("eufy_sync.zwift_client._load_tokens", return_value=cached(email="other@example.com")):
        with pytest.raises(PermanentSyncError, match="different email"):
            client.authenticate()
    client.close()


def test_invalid_cached_token_shapes_are_rejected_without_network():
    for bad in (
        {**cached(), "access_token": ""},
        {**cached(), "refresh_token": []},
        {**cached(), "expires_at": float("nan")},
        {**cached(), "expires_at": True},
        {**cached(), "profile_id": "not-numeric"},
    ):
        client = client_with(lambda request: (_ for _ in ()).throw(AssertionError("no network")))
        with patch("eufy_sync.zwift_client._load_tokens", return_value=bad):
            with pytest.raises(PermanentSyncError, match="--reauth zwift"):
                client.authenticate()
        client.close()


def test_force_auth_validates_password_before_replacing_cached_session():
    saved = []
    password_posts = 0

    def handler(request):
        nonlocal password_posts
        if request.method == "POST":
            password_posts += 1
            return httpx.Response(
                200,
                content=b'{"access_token":"forced-access","refresh_token":"forced-refresh","expires_in":Infinity}',
                headers={"Content-Type": "application/json"},
            )
        return httpx.Response(200, json=PROFILE)

    client = client_with(handler)
    with patch("eufy_sync.zwift_client._load_tokens", return_value=cached()), patch("eufy_sync.zwift_client._save_tokens", side_effect=saved.append):
        client.authenticate(force=True)
    assert password_posts == 1
    assert saved[0]["access_token"] == "forced-access"
    assert saved[0]["expires_at"] < time.time() + 301
    client.close()

    client = client_with(lambda request: httpx.Response(401, json={"error": "invalid_grant"}))
    with patch("eufy_sync.zwift_client._load_tokens", return_value=cached()), patch("eufy_sync.zwift_client._save_tokens") as save:
        with pytest.raises(PermanentSyncError, match="--update-password"):
            client.authenticate(force=True)
    save.assert_not_called()
    client.close()


def test_token_status_rejects_corrupt_or_wrong_binding_without_crashing():
    client = client_with(lambda request: (_ for _ in ()).throw(AssertionError("no network")))
    for value in (
        {**cached(), "expires_at": float("nan")},
        {**cached(), "expires_at": float("inf")},
        {**cached(), "email": "other@example.com"},
        {**cached(), "profile_id": {}},
        {**cached(), "access_token": ""},
    ):
        with patch("eufy_sync.zwift_client._load_tokens", return_value=value):
            assert client.token_status() == {"state": "expired", "days_remaining": 0}
    client.close()


def _authenticated_client(handler):
    client = client_with(handler)
    client._tokens = cached()
    client._profile_id = "123"
    return client


def test_update_preserves_full_payload_and_verifies_fresh_readback():
    current = dict(PROFILE)
    put_requests = []

    def handler(request):
        nonlocal current
        if request.method == "PUT":
            payload = json.loads(request.content)
            put_requests.append(request)
            current = payload
            return httpx.Response(200, json={})
        return httpx.Response(200, json=current)

    client = _authenticated_client(handler)
    result = client.update_weight(89.4555)
    assert result == {"verified": True, "changed": True, "weight_grams": 89456, "write_http_status": 200}
    assert len(put_requests) == 1
    request = put_requests[0]
    assert str(request.url) == WRITE_URL.format(profile_id=123)
    assert request.headers["source"] == "zwift-web"
    assert request.headers["authorization"] == "Bearer old-access"
    assert request.headers["content-type"].startswith("application/json")
    assert json.loads(request.content) == {**PROFILE, "weight": 89456}
    client.close()


def test_matching_weight_is_verified_noop_without_put():
    methods = []

    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json=PROFILE)

    client = _authenticated_client(handler)
    assert client.update_weight(89.45) == {"verified": True, "changed": False, "weight_grams": 89450}
    assert methods == ["GET"]
    client.close()


def test_204_and_wrong_readback_never_count_as_success():
    def handler(request):
        if request.method == "PUT":
            return httpx.Response(204)
        return httpx.Response(200, json=PROFILE)

    client = _authenticated_client(handler)
    with pytest.raises(RuntimeError, match="verification failed"):
        client.update_weight(89.46)
    client.close()


def test_put_timeout_reads_back_once_and_never_retries_put():
    current = dict(PROFILE)
    counts = {"put": 0, "get": 0}

    def handler(request):
        nonlocal current
        if request.method == "PUT":
            counts["put"] += 1
            current = {**current, "weight": 89460}
            raise httpx.ReadTimeout("token-secret", request=request)
        counts["get"] += 1
        return httpx.Response(200, json=current)

    client = _authenticated_client(handler)
    result = client.update_weight(89.46)
    assert result["verified"] is True and result["write_status"] == "unknown"
    assert counts == {"put": 1, "get": 2}
    client.close()


def test_access_rejected_after_auth_refreshes_once_before_update():
    gets = 0
    refreshes = 0
    puts = 0

    def handler(request):
        nonlocal gets, refreshes, puts
        if request.method == "POST":
            refreshes += 1
            return httpx.Response(200, json={"access_token": "renewed", "refresh_token": "rotated", "expires_in": 3600})
        if request.method == "PUT":
            puts += 1
        gets += 1
        if gets == 1:
            return httpx.Response(401, json={})
        return httpx.Response(200, json=PROFILE)

    client = _authenticated_client(handler)
    with patch("eufy_sync.zwift_client._save_tokens"):
        result = client.update_weight(89.45)
    assert result["changed"] is False
    assert refreshes == 1 and puts == 0 and gets == 3
    client.close()

    gets = refreshes = puts = 0
    client = _authenticated_client(handler)
    with patch("eufy_sync.zwift_client._save_tokens"):
        client.check_connection()
    assert refreshes == 1 and puts == 0 and gets == 3
    client.close()


def test_profile_identity_and_stable_field_changes_fail_without_pii_in_error():
    for changed in (
        {"id": 999, "weight": 89460, "emailAddress": "leak@example.com"},
        {"id": 123, "weight": 89460, "privacy": {"displayWeight": True}},
    ):
        reads = 0

        def handler(request, result=changed):
            nonlocal reads
            if request.method == "PUT":
                return httpx.Response(200, json={"secret": "password-secret"})
            reads += 1
            return httpx.Response(200, json=PROFILE if reads == 1 else {**PROFILE, **result})

        client = _authenticated_client(handler)
        with pytest.raises(PermanentSyncError) as exc:
            client.update_weight(89.46)
        message = str(exc.value)
        assert "password-secret" not in message
        assert "leak@example.com" not in message
        assert "999" not in message
        client.close()
