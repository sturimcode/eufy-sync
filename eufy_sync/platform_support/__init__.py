"""OS-specific plumbing behind one small interface.

Notifications and the scheduled-sync agent (install/uninstall/status/offer)
differ per platform. The active implementation is chosen once from
platform.system(): Darwin uses the macOS Launch Agent and osascript
notifications; Windows uses a Task Scheduler entry; every other platform uses
a no-op generic layer.
"""
from __future__ import annotations

import platform

_active = None


def _select():
    """Pick the implementation module for the current OS (uncached)."""
    system = platform.system()
    if system == "Darwin":
        from eufy_sync.platform_support import macos as impl
    elif system == "Windows":
        from eufy_sync.platform_support import windows as impl
    else:
        from eufy_sync.platform_support import generic as impl
    return impl


def _impl():
    """Return the active implementation module, selecting it once and caching
    the choice. Tests override the selection by patching this function."""
    global _active
    if _active is None:
        _active = _select()
    return _active


def notify(title: str, message: str, command: str | None = None) -> None:
    _impl().notify(title, message, command)


def install_agent() -> None:
    _impl().install_agent()


def uninstall_agent() -> None:
    _impl().uninstall_agent()


def offer_agent() -> None:
    _impl().offer_agent()


def agent_status() -> dict | None:
    return _impl().agent_status()


def agent_installed() -> bool:
    return _impl().agent_installed()


def purge_agent() -> None:
    _impl().purge_agent()
