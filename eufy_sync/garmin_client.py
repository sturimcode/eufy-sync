"""Garmin Connect client. Delegates login, refresh, and upload to
python-garminconnect; keeps a same-date duplicate check.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

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
    timestamped = [e for e in entries if _entry_instants(e)]
    if timestamped:
        matches = [
            e for e in timestamped
            if any(
                abs((instant - uploaded_at).total_seconds()) <= _TIMESTAMP_TOLERANCE_SECONDS
                for instant in _entry_instants(e)
            )
        ]
    else:
        matches = entries
    return matches[0] if len(matches) == 1 else None


def _is_garmin_auth_failure(exc: Exception) -> bool:
    """True when a Garmin call failed because the session is dead. Covers the
    dedicated auth error and the 401/403 that the library reports as a generic
    connection error ("API Error 401 - ...")."""
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    return isinstance(exc, GarminConnectConnectionError) and (
        "401" in str(exc) or "403" in str(exc)
    )


class GarminClient:
    def __init__(self, config: GarminConfig):
        self.config = config
        self._auth = GarminAuth(config.email, config.password)
        self._garmin = None
        self._allow_interactive = True

    def authenticate(self, allow_interactive: bool = True) -> None:
        self._allow_interactive = allow_interactive
        self._garmin = self._auth.login(interactive=allow_interactive)
        logger.info("Authenticated to Garmin Connect as %s", self.config.email)

    def _reauth(self) -> None:
        """Replace a dead session with a fresh login, prompting when a person
        is present. A scheduled run has nobody to prompt, but a password login
        usually needs no input at all, so it tries once silently rather than
        ending the run on a re-auth nag the run could have fixed itself. When
        even that cannot proceed (MFA demanded, password wrong), the error
        already names the command to run and travels to the caller unchanged."""
        if not self._allow_interactive:
            logger.info("Garmin session expired; re-authenticating without prompts")
            self._garmin = self._auth.silent_reauth()
        else:
            logger.info("Garmin session expired; re-authenticating")
            self._garmin = self._auth.force_reauth()

    def _call_with_reauth(self, call):
        """Run a Garmin call, re-logging in once when the session is dead.

        The duplicate check is the run's first Garmin call, so a token that
        expired between runs used to fail here on every scheduled sync before
        upload's own healing could kick in. Non-auth errors, and anything the
        relogin or the retry raises, travel to the caller unchanged."""
        try:
            return call()
        except (GarminConnectAuthenticationError, GarminConnectConnectionError) as e:
            if not _is_garmin_auth_failure(e):
                raise
            self._reauth()
            return call()

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
        # Transient connection errors propagate to _retry; only a dead session
        # (token expired or revoked, seen as a 401) heals and retries here.
        result = self._call_with_reauth(lambda: self._add_body_composition(body_comp))
        logger.info(
            "Uploaded body comp to Garmin: %.1f kg at %s",
            body_comp.weight, body_comp.timestamp,
        )
        return result if isinstance(result, dict) else {"status": "ok"}

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
