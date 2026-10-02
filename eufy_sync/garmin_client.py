"""Garmin Connect client. Delegates login, refresh, and upload to
python-garminconnect; keeps a same-date duplicate check.

The library logs in through curl_cffi but sends every data call through plain
requests, and on some networks (VPNs, datacenter IPs) Cloudflare refuses that
TLS fingerprint with a 403 even though the token is fine (upstream issue #444).
A call refused that way is sent once more through curl_cffi with a browser
fingerprint, reusing the session's token, before anything here treats the
session as dead.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from eufy_sync.config import GarminConfig
from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.transform import GarminBodyComposition

logger = logging.getLogger(__name__)

# A Garmin entry counts as the weigh-in we uploaded when its weight is within
# this much of ours and its own timestamp is within this many seconds of the
# instant we sent. The weight window absorbs the gram/kg rounding; the time
# window absorbs Garmin re-stamping an upload by a few seconds, while staying
# far short of a separate weigh-in.
_WEIGHT_TOLERANCE_KG = 0.1
_TIMESTAMP_TOLERANCE_SECONDS = 120


def _entry_instants(entry: dict) -> list[datetime]:
    """The instants a weigh-in entry could stand for.

    Garmin returns timestampGMT and date as epoch milliseconds, and the two
    differ by the recording device's UTC offset. Which one carries the true
    instant is not consistent across sources, so both are treated as
    candidates: matching either identifies our own upload, and a spurious
    extra match only makes the delete ambiguous, which fails open to the
    duplicate rather than removing someone else's entry.
    """
    instants = []
    for field in ("timestampGMT", "date"):
        millis = entry.get(field)
        if isinstance(millis, bool) or not isinstance(millis, (int, float)):
            continue
        try:
            instants.append(datetime.fromtimestamp(millis / 1000.0, timezone.utc))
        except (OSError, OverflowError, ValueError):
            continue
    return instants


def _match_uploaded_entry(entries: list[dict], uploaded_at: datetime) -> dict | None:
    """Pick the one entry we uploaded at uploaded_at, or None when the answer
    is not unique. Weight alone is not enough: a manual weigh-in on the same
    day within the weight window would match too, and deleting it would throw
    away data eufy-sync never created. Entries that carry a timestamp must
    therefore match on it. Only when Garmin returns no timestamps at all does
    a single weight match stand on its own - one response carries the same
    fields for every entry, so the two cases do not mix in practice."""
    matches = _entries_at(entries, uploaded_at)
    return matches[0] if len(matches) == 1 else None


def _entries_at(entries: list[dict], uploaded_at: datetime) -> list[dict]:
    """The entries whose own timestamp sits within the tolerance of
    uploaded_at, or all of them when Garmin sent no timestamps."""
    timestamped = [e for e in entries if _entry_instants(e)]
    if not timestamped:
        return list(entries)
    return [
        e for e in timestamped
        if any(
            abs((instant - uploaded_at).total_seconds()) <= _TIMESTAMP_TOLERANCE_SECONDS
            for instant in _entry_instants(e)
        )
    ]


# The library raises HTTP failures with the status only in the message:
# "API Error 403 - ..." from a direct call, "API call client error (403): ..."
# once its error decorator has rewrapped it.
_STATUS_RE = re.compile(r"(?:API Error|error \(|HTTP)\s*(\d{3})\b")

# Text that only a Cloudflare interstitial or block page carries. The API host
# sits behind Cloudflare, so cf-ray and "server: cloudflare" appear on ordinary
# JSON answers too and prove nothing on their own.
_CLOUDFLARE_BODY_MARKERS = (
    "just a moment",
    "attention required",
    "cf-chl",
    "challenge-platform",
    "cf-error-details",
    "cloudflare ray id",
)

# The browser fingerprint for the fallback. Issue #444's reporter got 200s
# with this one, the library's native headers, and the same token that plain
# requests could not use.
_IMPERSONATE = "chrome"


def _status_code(exc: BaseException) -> int | None:
    """The HTTP status behind a library error, or None for a non-HTTP one."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    match = _STATUS_RE.search(str(exc))
    return int(match.group(1)) if match else None


def _is_garmin_auth_failure(exc: Exception) -> bool:
    """True when a Garmin call failed because the session may be dead. Covers
    the dedicated auth error and the 401/403 that the library reports as a
    generic connection error ("API Error 401 - ...").

    A 403 stays ambiguous even with the response in hand: the network block
    in issue #444 answers with the same JSON ForbiddenException a refused
    token gets. By the time a 403 reaches the relogin, the browser-fingerprint
    retry has already failed, and a relogin keeps the stored token until the
    new login succeeds, so a 403 that was only a passing block costs one
    login attempt, not the session."""
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    return isinstance(exc, GarminConnectConnectionError) and _status_code(exc) in (401, 403)


class _LastResponse:
    """The status, headers, and start of the body of the most recent failed
    response on the library's API session or on the fallback.

    The library drops the response before raising, so this is the only place a
    Cloudflare block page and a JSON 403 from the API can be told apart."""

    _BODY_LIMIT = 4096

    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.status: int | None = None
        self.headers: dict[str, str] = {}
        self.body = ""

    def record(self, resp) -> None:
        status = getattr(resp, "status_code", None)
        if not isinstance(status, int) or status < 400:
            return
        self.status = status
        try:
            self.headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        except Exception:
            self.headers = {}
        try:
            self.body = (resp.text or "")[: self._BODY_LIMIT]
        except Exception:
            self.body = ""

    def hook(self, resp, *args, **kwargs) -> None:
        """requests response hook. Returns None so the response is unchanged."""
        self.record(resp)

    def is_cloudflare_block(self) -> bool:
        if self.status is None:
            return False
        if self.headers.get("cf-mitigated", "").lower() == "challenge":
            return True
        if "html" not in self.headers.get("content-type", "").lower():
            return False
        body = self.body.lower()
        return any(marker in body for marker in _CLOUDFLARE_BODY_MARKERS)


def _new_impersonating_session():
    """A curl_cffi session with a browser TLS fingerprint. Kept separate so
    tests can swap in a fake transport."""
    from curl_cffi import requests as cffi_requests
    return cffi_requests.Session(impersonate=_IMPERSONATE)


class _ImpersonatingSession:
    """Stands in for the library's requests.Session during one fallback call.

    The library still builds the URL, the auth headers, and its own error
    handling; only the transport changes. curl_cffi refuses requests' files=
    argument, so a file upload is rebuilt as a CurlMime multipart body, which
    curl_cffi does support."""

    def __init__(self, recorder: _LastResponse):
        self._recorder = recorder

    def request(self, method, url, headers=None, files=None, **kwargs):
        mime = _to_curl_mime(files) if files else None
        if mime is not None:
            kwargs["multipart"] = mime
        sess = _new_impersonating_session()
        try:
            resp = sess.request(method, url, headers=headers, **kwargs)
        finally:
            if mime is not None:
                mime.close()
            sess.close()
        self._recorder.record(resp)
        return resp


def _to_curl_mime(files: dict):
    """Rebuild a requests-style files= mapping as a curl_cffi multipart body.

    Accepts the shapes the library uses: {"file": (name, bytes_or_fileobj)}
    with an optional third content-type element, or a bare bytes/file value."""
    from curl_cffi import CurlMime
    mime = CurlMime()
    try:
        for field, value in files.items():
            filename, content, content_type = None, value, "application/octet-stream"
            if isinstance(value, (tuple, list)):
                filename, content = value[0], value[1]
                if len(value) > 2 and value[2]:
                    content_type = value[2]
            if hasattr(content, "read"):
                content = content.read()
            if isinstance(content, str):
                content = content.encode()
            mime.addpart(name=field, filename=filename, content_type=content_type, data=bytes(content))
    except Exception:
        mime.close()
        raise
    return mime


class GarminClient:
    def __init__(self, config: GarminConfig):
        self.config = config
        self._auth = GarminAuth(config.email, config.password)
        self._garmin = None
        self._allow_interactive = True
        # At most one relogin per run, whether or not it worked. After a
        # failed one, later calls raise its error again: a second attempt
        # minutes later meets the same MFA demand or wrong password. After a
        # successful one, a later 401/403 means the fresh session did not help
        # (a Cloudflare block on the API while SSO still lets logins through),
        # so the call's own error is raised. Every extra login raises the odds
        # of a Garmin 429.
        self._reauth_attempted = False
        self._reauth_error: Exception | None = None
        self._last_response = _LastResponse()
        # Set once a browser-fingerprint retry succeeds. The block is a
        # property of the network, not the session, so from then on every call
        # in this run goes straight through curl_cffi instead of paying a
        # refused plain request first. A new GarminClient (a new run) starts
        # over on plain requests.
        self._impersonate_always = False

    def authenticate(self, allow_interactive: bool = True) -> None:
        self._allow_interactive = allow_interactive
        self._garmin = self._auth.login(interactive=allow_interactive)
        self._watch_responses(self._garmin)
        logger.info("Authenticated to Garmin Connect as %s", self.config.email)

    def _watch_responses(self, garmin) -> None:
        """Hook the library's API session so a failed call's response can be
        inspected after the library has raised without it. _api_session is
        private but present in every release we allow (0.3.10 onward); without
        it the hook is skipped and every 403 counts as ambiguous."""
        sess = getattr(getattr(garmin, "client", None), "_api_session", None)
        hooks = getattr(sess, "hooks", None)
        if not isinstance(hooks, dict):
            return
        response_hooks = hooks.setdefault("response", [])
        if self._last_response.hook not in response_hooks:
            response_hooks.append(self._last_response.hook)

    def _reauth(self) -> None:
        """Replace a dead session with a fresh login, prompting when a person
        is present. A scheduled run has nobody to prompt, but a password login
        usually needs no input at all, so it tries once silently rather than
        ending the run on a re-auth nag the run could have fixed itself. When
        even that cannot proceed (MFA demanded, password wrong), the error
        already names the command to run and travels to the caller unchanged.

        Only one relogin is tried per run. After a failure, every later call
        that needs one gets the same error back without contacting Garmin."""
        self._reauth_attempted = True
        try:
            if not self._allow_interactive:
                logger.info("Garmin session expired; re-authenticating without prompts")
                self._garmin = self._auth.silent_reauth()
            else:
                logger.info("Garmin session expired; re-authenticating")
                self._garmin = self._auth.force_reauth()
        except Exception as e:
            self._reauth_error = e
            raise
        self._watch_responses(self._garmin)

    def _attempt(self, call):
        """Run call once, forgetting the response of any earlier failure so a
        network error is never judged by a stale block page."""
        self._last_response.clear()
        return call()

    def _call_impersonating(self, call):
        """Run call once more with the library's API session swapped for a
        curl_cffi one. The token, URL, and headers stay the library's."""
        client = self._garmin.client
        original = client._api_session
        client._api_session = _ImpersonatingSession(self._last_response)
        try:
            return self._attempt(call)
        finally:
            client._api_session = original

    def _can_impersonate(self) -> bool:
        client = getattr(self._garmin, "client", None)
        if getattr(client, "_api_session", None) is None:
            return False
        try:
            import curl_cffi  # noqa: F401
        except ImportError:
            return False
        return True

    def _call_with_fallback(self, call):
        """Run call; when the network refused it, run it once more through a
        browser fingerprint. A Cloudflare block page qualifies, and so does
        any 403, because the #444 block answers with a JSON 403 that looks
        exactly like a refused token. Trying the fingerprint first costs one
        request; a relogin costs a login and risks a 429, and on a blocked
        network its own token check fails the same way."""
        if self._impersonate_always and self._can_impersonate():
            return self._call_impersonating(call)
        try:
            return self._attempt(call)
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            blocked = self._last_response.is_cloudflare_block()
            if not (blocked or _status_code(e) == 403) or not self._can_impersonate():
                raise
            logger.info(
                "Garmin refused the call (%s%s); retrying once with a browser fingerprint",
                e, ", Cloudflare block page" if blocked else "",
            )
            result = self._call_impersonating(call)
            if not self._impersonate_always:
                logger.info("Browser fingerprint got through; using it for the rest of this run")
                self._impersonate_always = True
            return result

    def _call_with_reauth(self, call):
        """Run a Garmin call, re-logging in once when the session is dead.

        The duplicate check is the run's first Garmin call, so a token that
        expired between runs used to fail here on every scheduled sync before
        upload's own healing could kick in. Non-auth errors, and anything the
        relogin or the retry raises, travel to the caller unchanged.

        Each session gets one browser-fingerprint retry before its 403 counts
        against it (see _call_with_fallback). A Cloudflare block page that
        survives that retry never triggers a relogin: the token was not the
        problem, and a new one would meet the same block."""
        try:
            return self._call_with_fallback(call)
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            if not _is_garmin_auth_failure(e) or self._last_response.is_cloudflare_block():
                raise
            if self._reauth_attempted:
                if self._reauth_error is not None:
                    # This run already tried to log in again and failed.
                    # Report that failure, which names the fix, rather than
                    # this call's 401 or 403.
                    raise self._reauth_error from e
                # The relogin worked and Garmin still refuses: another login
                # would not change that.
                raise
            self._reauth()
            return self._call_with_fallback(call)

    def check_connection(self) -> None:
        """Verify an authenticated read, propagating failures to diagnostics."""
        today = datetime.now().astimezone().strftime("%Y-%m-%d")
        self._call_with_reauth(lambda: self._garmin.get_daily_weigh_ins(today))

    def has_weight_on_date(self, dt: datetime) -> bool:
        """Whether Garmin already has a weight entry for the date."""
        # Query by LOCAL calendar date: uploads are now filed under the local
        # date (see transform.py), so the duplicate check must match that.
        date_str = dt.astimezone().strftime("%Y-%m-%d")

        def read() -> bool:
            data = self._garmin.get_body_composition(date_str, date_str)
            entries = data.get("dateWeightList", data.get("dailyWeightSummaries", []))
            return len(entries) > 0

        try:
            return self._call_with_reauth(read)
        except Exception as e:
            # Fail open: let the upload proceed; Garmin de-dupes by timestamp.
            # A failed relogin is remembered, so the upload reports it with its
            # fix-it hint instead of logging in a second time.
            logger.warning("Garmin duplicate-check failed for %s: %s", date_str, e)
            return False

    def delete_weight_entry(self, dt: datetime, weight_kg: float) -> bool:
        """Delete the weigh-in we uploaded at dt, matched by weight and by the
        entry's own timestamp. Used to replace a weight-only (raw Wi-Fi)
        upload once the full body-comp record for the same weigh-in arrives
        (issue #48).

        dt is the instant that was uploaded (the stored
        measurement_timestamp), not just any time on the right day - it is
        what tells our entry apart from a manual weigh-in that happens to sit
        within the weight window.

        Fail-open: returns False when nothing matched, when the match was not
        unique, or when the API errored, and the caller uploads anyway - the
        worst case is the duplicate we would have had without this method,
        which beats deleting an entry eufy-sync did not create."""
        uploaded_at = dt if dt.tzinfo else dt.astimezone()
        date_str = uploaded_at.astimezone().strftime("%Y-%m-%d")

        def find_and_delete() -> bool:
            # Safe to retry whole after a relogin: a dead session fails on the
            # lookup, before anything was deleted.
            data = self._garmin.get_daily_weigh_ins(date_str)
            near = [
                entry for entry in data.get("dateWeightList", [])
                if abs(entry.get("weight", 0) / 1000.0 - weight_kg) <= _WEIGHT_TOLERANCE_KG
            ]  # Garmin stores grams
            match = _match_uploaded_entry(near, uploaded_at)
            if match is None:
                if near:
                    logger.warning(
                        "%d entries near %.1f kg on %s, none uniquely ours; leaving them alone",
                        len(near), weight_kg, date_str,
                    )
                else:
                    logger.warning("No weight entry near %.1f kg found on %s to replace", weight_kg, date_str)
                return False
            self._garmin.delete_weigh_in(match["samplePk"], date_str)
            logger.info("Deleted weight-only entry (%.1f kg on %s) ahead of full body comp",
                        weight_kg, date_str)
            return True

        try:
            return self._call_with_reauth(find_and_delete)
        except Exception as e:
            logger.warning("Could not delete weight entry on %s: %s", date_str, e)
            return False

    def _add_body_composition(self, body_comp: GarminBodyComposition):
        # add_body_composition also accepts visceral_fat_mass, active_met, and
        # physique_rating; the Eufy scale does not provide those, so they are omitted.
        return self._garmin.add_body_composition(
            timestamp=body_comp.timestamp,
            weight=body_comp.weight,
            percent_fat=body_comp.percent_fat,
            percent_hydration=body_comp.percent_hydration,
            visceral_fat_rating=body_comp.visceral_fat_rating,
            bone_mass=body_comp.bone_mass,
            muscle_mass=body_comp.muscle_mass,
            basal_met=body_comp.basal_met,
            metabolic_age=body_comp.metabolic_age,
            bmi=body_comp.bmi,
        )

    def upload_body_composition(self, body_comp: GarminBodyComposition) -> dict:
        """Upload one body-composition FIT and sort out what the answer means.

        Modeled on scalebridge-sync's upload outcomes:
          - 2xx: uploaded. A 409 counts too, but only once a lookup finds
            the weigh-in on Garmin; an unconfirmed 409 is PermanentSyncError,
            so nothing is recorded as synced on Garmin's word alone.
          - 401, or 403: a dead session or a blocked network. The fingerprint
            retry and the run's one relogin heal what they can; a refusal that
            outlives both raises PermanentSyncError, since _retry asking again
            seconds later would meet the same answer.
          - 429: GarminConnectTooManyRequestsError, which sync treats as
            permanent for this run, as it does a login 429. Retrying a rate
            limit in a loop only extends it.
          - 408 and 5xx, and network failures: raised unchanged for _retry.
          - Any other 4xx: Garmin rejected this upload itself; asking again
            sends the same bytes. PermanentSyncError."""
        def upload():
            try:
                return self._add_body_composition(body_comp)
            except GarminConnectConnectionError as e:
                if _status_code(e) == 409:
                    return self._confirm_duplicate(body_comp, e)
                raise

        try:
            result = self._call_with_reauth(upload)
        except GarminConnectTooManyRequestsError:
            raise
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            if e is not self._reauth_error:
                # A failed relogin's own error already says what went wrong
                # and travels unchanged; only Garmin's answer to the upload
                # is sorted here.
                self._classify_upload_failure(e)
            raise
        logger.info(
            "Uploaded body comp to Garmin: %.1f kg at %s",
            body_comp.weight, body_comp.timestamp,
        )
        return result if isinstance(result, dict) else {"status": "ok"}

    def _confirm_duplicate(self, body_comp: GarminBodyComposition, conflict: Exception) -> dict:
        """Accept a 409 only when Garmin really holds this weigh-in: an entry
        within the weight window whose own timestamp matches the instant we
        sent, the same test delete_weight_entry trusts. Runs inside the upload
        attempt, so it rides whichever transport the upload used."""
        from eufy_sync.sync import PermanentSyncError

        uploaded_at = datetime.fromisoformat(body_comp.timestamp)
        if uploaded_at.tzinfo is None:
            uploaded_at = uploaded_at.astimezone()
        date_str = uploaded_at.astimezone().strftime("%Y-%m-%d")
        try:
            data = self._garmin.get_daily_weigh_ins(date_str)
            near = [
                entry for entry in data.get("dateWeightList", [])
                if abs(entry.get("weight", 0) / 1000.0 - body_comp.weight) <= _WEIGHT_TOLERANCE_KG
            ]  # Garmin stores grams
            found = bool(_entries_at(near, uploaded_at))
        except Exception as e:
            raise PermanentSyncError(
                f"Garmin answered the body comp upload with 409 Conflict ({conflict}), and the "
                f"lookup to confirm it already holds the weigh-in failed: {e}"
            ) from conflict
        if not found:
            raise PermanentSyncError(
                f"Garmin answered the body comp upload with 409 Conflict ({conflict}), but has no "
                f"{body_comp.weight:.1f} kg weigh-in at {body_comp.timestamp}; not counting it as uploaded"
            ) from conflict
        logger.info("Garmin already holds this weigh-in (409, confirmed by lookup); counting it as uploaded")
        return {"status": "duplicate"}

    def _classify_upload_failure(self, exc: Exception) -> None:
        """Raise the right error for an upload Garmin refused, or return to
        let a transient failure travel to _retry unchanged."""
        from eufy_sync.sync import PermanentSyncError

        status = _status_code(exc)
        if status == 429:
            retry_after = self._last_response.headers.get("retry-after")
            logger.warning(
                "Garmin rate-limited the upload%s; leaving it for the next run",
                f" (Retry-After: {retry_after}s)" if retry_after else "",
            )
            raise GarminConnectTooManyRequestsError(f"Garmin upload rate limited: {exc}") from exc
        if isinstance(exc, GarminConnectAuthenticationError) or status in (401, 403):
            if self._last_response.is_cloudflare_block():
                raise PermanentSyncError(
                    f"Cloudflare is blocking Garmin uploads from this network (HTTP {status}). "
                    "A VPN or datacenter connection is the usual cause; try another network."
                ) from exc
            raise PermanentSyncError(
                f"Garmin still refused the upload ({exc}) after a fresh login. "
                "If you are on a VPN, try without it; otherwise run: eufy-sync --reauth garmin"
            ) from exc
        if status is not None and 400 <= status < 500 and status != 408:
            raise PermanentSyncError(f"Garmin rejected the body comp upload (HTTP {status}): {exc}") from exc

    def close(self) -> None:
        # Last chance to keep whatever the library rotated mid-run: its refresh
        # can hand back a new refresh token that lives in memory only. sync_user
        # closes every client it constructed, including ones whose authenticate()
        # never ran or failed, so _garmin is often None here.
        if self._garmin is not None:
            try:
                self._auth.save_if_changed(self._garmin)
            except Exception as e:
                # Closing must not be the thing that fails a finished run.
                logger.debug("Garmin token save on close failed: %s", e)
        self._garmin = None
