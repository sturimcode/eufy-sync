from __future__ import annotations

import threading
import time

import pytest

from eufy_sync.prompt import PROMPT_TIMEOUT_SECONDS, input_with_timeout


def test_returns_the_typed_line(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    assert input_with_timeout("Really? ") == "y"


def test_default_timeout_is_five_minutes():
    """Both call sites quote "5 minutes" in the message they print on timeout,
    so the constant and that copy have to stay in step."""
    assert PROMPT_TIMEOUT_SECONDS == 300


def test_returns_none_when_nobody_answers(monkeypatch):
    """The 2026-08-28 case: the prompt was never answered and input() held the
    process open for hours. The caller must get control back at the timeout."""
    released = threading.Event()

    def _block(prompt):
        try:
            released.wait(10)
            return "far too late"
        finally:
            released.set()

    monkeypatch.setattr("builtins.input", _block)

    started = time.monotonic()
    try:
        assert input_with_timeout("", 0.2) is None
        elapsed = time.monotonic() - started
    finally:
        # Release the daemon reader rather than leaving it parked for the rest
        # of the test run.
        released.set()

    assert elapsed < 5   # returned on the timeout, not when the reader finished


def test_reraises_what_input_raises(monkeypatch):
    """A closed stdin still has to surface as EOFError: callers wrap this in
    the same except clauses they used around a plain input()."""
    def _eof(prompt):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    with pytest.raises(EOFError):
        input_with_timeout("", 5)
