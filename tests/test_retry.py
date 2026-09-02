from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from eufy_sync.sync import PermanentSyncError, _retry


def test_retry_succeeds_first_try():
    result = _retry(lambda: 42, "test")
    assert result == 42


def test_retry_succeeds_after_failures():
    attempts = [0]

    def flaky():
        attempts[0] += 1
        if attempts[0] < 3:
            raise RuntimeError("fail")
        return "ok"

    with patch("eufy_sync.sync.time.sleep"):
        result = _retry(flaky, "flaky op")
    assert result == "ok"
    assert attempts[0] == 3


def test_retry_raises_after_max_attempts():
    def always_fail():
        raise RuntimeError("permanent failure")

    with patch("eufy_sync.sync.time.sleep"):
        with pytest.raises(RuntimeError, match="permanent failure"):
            _retry(always_fail, "doomed op")


def test_retry_does_not_retry_permanent_sync_error():
    """PermanentSyncError signals a known-permanent failure - don't waste time retrying."""
    attempts = [0]

    def fail_permanently():
        attempts[0] += 1
        raise PermanentSyncError("bad password")

    with patch("eufy_sync.sync.time.sleep") as mock_sleep:
        with pytest.raises(PermanentSyncError, match="bad password"):
            _retry(fail_permanently, "doomed op")

    assert attempts[0] == 1, "PermanentSyncError must raise on first attempt"
    mock_sleep.assert_not_called()


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"HTTP {status}", request=request, response=response)


def test_retry_does_not_retry_4xx_except_429():
    """Auth failures and bad requests won't recover by retrying."""
    attempts = [0]

    def fail_with_401():
        attempts[0] += 1
        raise _http_status_error(401)

    with patch("eufy_sync.sync.time.sleep") as mock_sleep:
        with pytest.raises(httpx.HTTPStatusError):
            _retry(fail_with_401, "doomed op")

    assert attempts[0] == 1
    mock_sleep.assert_not_called()


def test_retry_does_retry_5xx():
    """Server errors are transient - retry helps."""
    attempts = [0]

    def fail_with_503():
        attempts[0] += 1
        if attempts[0] < 3:
            raise _http_status_error(503)
        return "ok"

    with patch("eufy_sync.sync.time.sleep"):
        result = _retry(fail_with_503, "flaky op")
    assert result == "ok"
    assert attempts[0] == 3


def test_retry_does_retry_429():
    """Rate-limit responses can succeed after backoff."""
    attempts = [0]

    def fail_with_429():
        attempts[0] += 1
        if attempts[0] < 3:
            raise _http_status_error(429)
        return "ok"

    with patch("eufy_sync.sync.time.sleep"):
        result = _retry(fail_with_429, "rate-limited op")
    assert result == "ok"
    assert attempts[0] == 3
