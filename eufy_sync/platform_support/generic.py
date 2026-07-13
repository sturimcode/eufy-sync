"""Generic no-op layer for platforms without a managed scheduler.

Covers every platform without a native implementation. Notifications are
silently dropped; the scheduled-sync agent is not managed here, so --doctor
skips the agent check and --uninstall skips the agent removal.
"""
from __future__ import annotations

_UNMANAGED = (
    "Auto-sync is not managed on this platform. See the Headless Linux "
    "section of the README for a systemd timer recipe."
)


def notify(title: str, message: str, command: str | None = None) -> None:
    pass


def install_agent() -> None:
    print(_UNMANAGED)


def uninstall_agent() -> None:
    print(_UNMANAGED)


def offer_agent() -> None:
    pass


def agent_status() -> dict | None:
    return None


def agent_installed() -> bool:
    return False


def purge_agent() -> None:
    pass
