"""Console prompts that give up instead of waiting forever.

The recovery flow starts with a toast: a sync fails, the user is told to run
`eufy-sync --reauth garmin`, and the answer to whatever that command asks is
typed into a terminal window opened for one command and then easy to walk away
from. On 2026-08-28 a run did exactly that. The re-auth confirmation sat
unanswered in input() for over two hours, and the parked process kept a file
lock on the interpreter inside its own uv tool venv, so a later
`uv tool install --force` failed with "Access is denied" and left the venv
half-deleted. A prompt with nobody in front of it now expires, and the caller
takes the documented safe default.

Only the prompts on that toast-driven path use this. A setup wizard or an
--uninstall confirmation is run deliberately, with the user present.
"""
from __future__ import annotations

import threading

PROMPT_TIMEOUT_SECONDS = 300


def input_with_timeout(prompt: str = "", timeout: float = PROMPT_TIMEOUT_SECONDS) -> str | None:
    """Read one line from stdin. Return None if nothing arrives within `timeout`.

    Whatever input() raises comes back out here unchanged - EOFError on a closed
    stdin, KeyboardInterrupt on Ctrl+C - so callers keep the except clauses they
    already had around a plain input().

    A console read that has already started cannot be cancelled, so after a
    timeout the reader thread stays parked on stdin until the process exits. It
    is a daemon thread and holds no locks, so it cannot hold that exit up: this
    helper ends the wait, not the read. That parked reader also means a caller
    must not prompt again after a timeout - the old read would swallow the next
    typed line - so a timeout has to end in an exit, which every caller does.
    """
    result: list[str] = []
    error: list[BaseException] = []
    done = threading.Event()

    def _read() -> None:
        try:
            result.append(input(prompt))
        except BaseException as exc:
            # Deliberately wider than Exception: a Ctrl+C delivered while this
            # thread is inside input() arrives as KeyboardInterrupt, and the
            # caller's except clauses expect to see it.
            error.append(exc)
        finally:
            done.set()

    threading.Thread(target=_read, daemon=True).start()

    # Wait in slices of at most a second rather than one long one. On Windows a
    # blocking lock wait does not wake for Ctrl+C, so a single five-minute wait
    # would swallow the interrupt for its whole length; short slices keep the
    # caller's Ctrl+C responsive.
    remaining = timeout
    while remaining > 0:
        slice_seconds = min(1.0, remaining)
        if done.wait(slice_seconds):
            break
        remaining -= slice_seconds

    if not done.is_set():
        return None

    if error:
        raise error[0]
    return result[0]
