"""Tell a passing network failure apart from a problem the user must fix.

The classification is by message text because the failures arrive from three
HTTP stacks (httpx, requests through garminconnect, and the stdlib), none of
which share an exception type for "the network was not there".
"""
from __future__ import annotations

# Substrings (matched case-insensitively) that mark a failure as a transient
# network problem rather than something the user must fix. These are the
# DNS / socket / timeout messages that surface from httpx, requests, and the
# stdlib when connectivity is missing or coming back.
#
# Deliberately NOT here: "_ssl.c" on its own. A TLS handshake that TIMED OUT
# is a blip and is caught by "timed out" below, but "certificate verify
# failed" and "wrong version number" also carry "_ssl.c" and are persistent,
# user-actionable problems (a wrong system clock, a stale CA bundle, a
# TLS-intercepting proxy) that must notify, not be silenced.
_TRANSIENT_MARKERS = (
    "nodename nor servname",              # macOS getaddrinfo, no DNS
    "name or service not known",          # Linux getaddrinfo, no DNS
    "temporary failure in name resolution",
    "timed out",                          # connect / read / handshake timeouts
    "all connection attempts failed",     # httpx ConnectError, common on wake
    "connection reset",
    "connection refused",
    "connection aborted",
    "network is unreachable",
    "max retries exceeded",
    "temporarily unavailable",
)


def is_transient_network_error(msg: str) -> bool:
    """True when a failure message looks like a passing network problem."""
    lowered = (msg or "").lower()
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)
