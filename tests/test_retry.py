from __future__ import annotations

from unittest.mock import patch

import pytest

from eufy_garmin_sync.sync import _retry


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

    with patch("eufy_garmin_sync.sync.time.sleep"):
        result = _retry(flaky, "flaky op")
    assert result == "ok"
    assert attempts[0] == 3


def test_retry_raises_after_max_attempts():
    def always_fail():
        raise RuntimeError("permanent failure")

    with patch("eufy_garmin_sync.sync.time.sleep"):
        with pytest.raises(RuntimeError, match="permanent failure"):
            _retry(always_fail, "doomed op")
