from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class EufyConfig:
    email: str
    password: str
    customer_id: str | None = None


@dataclass
class GarminConfig:
    email: str
    password: str


@dataclass
class StravaConfig:
    client_id: str
    client_secret: str


@dataclass
class ZwiftConfig:
    email: str
    password: str


@dataclass
class IntervalsConfig:
    athlete_id: str
    api_key: str


@dataclass
class UserConfig:
    name: str
    eufy: EufyConfig
    garmin: GarminConfig | None = None
    strava: StravaConfig | None = None
    zwift: ZwiftConfig | None = None
    intervals: IntervalsConfig | None = None


@dataclass
class AppConfig:
    users: list[UserConfig]


def _interpolate_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(
                f"Environment variable '{var_name}' referenced in config is not set."
            )
        return env_value

    return re.sub(r"\$\{(\w+)}", replacer, value)


def _walk_and_interpolate(obj: dict | list | str) -> dict | list | str:
    """Recursively interpolate env vars in all string values."""
    if isinstance(obj, str):
        return _interpolate_env_vars(obj)
    if isinstance(obj, dict):
        return {k: _walk_and_interpolate(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_interpolate(item) for item in obj]
    return obj


def _get_password(
    user_name: str, service: str, email: str, yaml_password: str | None, migrate: bool = True
) -> str:
    """Resolve password: credential store first, then YAML fallback.

    migrate=False reads a legacy keychain item without moving it into the
    vault, for callers that run without the sync lock."""
    from eufy_sync.credentials import get_password

    key = f"{user_name}:{service}"
    stored = get_password(key, migrate=migrate)
    if stored:
        return stored

    if yaml_password:
        return yaml_password

    raise ValueError(
        f"No {service} password found for user '{user_name}'. "
        f"Run: eufy-sync --update-password"
    )


def _get_strava_secret(user_name: str, yaml_secret: str | None, migrate: bool = True) -> str:
    """Resolve the Strava API client secret: credential store first, then the
    YAML fallback for configs not yet migrated.

    Its own function rather than _get_password's `service` argument because
    the fix is a different command: --update-password prompts for the Eufy and
    Garmin account passwords, never for a Strava app secret.
    """
    from eufy_sync.credentials import get_password

    stored = get_password(f"{user_name}:strava", migrate=migrate)
    if stored:
        return stored

    if yaml_secret:
        return yaml_secret

    raise ValueError(
        f"No Strava client secret found for user '{user_name}'. "
        f"Run: eufy-sync --setup-strava"
    )


def _get_intervals_key(user_name: str, yaml_key: str | None, migrate: bool = True) -> str:
    """Resolve the Intervals.icu API key: credential store first, then a YAML
    fallback (normally a ${VAR} reference on a headless machine).

    Like the Strava secret, the fix is its own setup command rather than
    --update-password, which only handles account passwords."""
    from eufy_sync.credentials import get_password

    stored = get_password(f"{user_name}:intervals", migrate=migrate)
    if stored:
        return stored

    if yaml_key:
        return yaml_key

    raise ValueError(
        f"No Intervals.icu API key found for user '{user_name}'. "
        f"Run: eufy-sync --setup-intervals"
    )


def load_config(path: Path, migrate: bool = True) -> AppConfig:
    """Parse the config and resolve its secrets.

    The default migrates legacy keychain items into the vault, which writes
    the vault, so it belongs inside the sync lock. Unlocked read-only
    commands (--status, --history) pass migrate=False."""
    with open(path) as f:
        raw = yaml.safe_load(f)

    # An empty file parses to None and a stray top-level list parses to a list;
    # both used to reach raw.get("users") and die on AttributeError, and a
    # missing or empty users list died on a bare KeyError. None of those name
    # the file or say what to do about it.
    if not isinstance(raw, dict) or not isinstance(raw.get("users"), list) or not raw["users"]:
        raise ValueError(
            f"No users found in {path}. The file is empty or malformed. "
            f"Restore it from a backup, or delete it and run eufy-sync to set up again."
        )

    raw = _walk_and_interpolate(raw)

    if len(raw.get("users", [])) > 1:
        raise ValueError(
            "eufy-sync supports a single user per installation. "
            "Found multiple entries under 'users:' - edit your config to keep only one."
        )

    users = []
    for u in raw["users"]:
        name = u["name"]

        garmin = None
        if "garmin" in u:
            garmin = GarminConfig(
                email=u["garmin"]["email"],
                password=_get_password(name, "garmin", u["garmin"]["email"], u["garmin"].get("password"), migrate),
            )

        strava = None
        if "strava" in u:
            strava = StravaConfig(
                client_id=str(u["strava"]["client_id"]),
                client_secret=_get_strava_secret(name, u["strava"].get("client_secret"), migrate),
            )

        zwift = None
        if "zwift" in u:
            zwift = ZwiftConfig(
                email=u["zwift"]["email"],
                password=_get_password(name, "zwift", u["zwift"]["email"], u["zwift"].get("password"), migrate),
            )

        intervals = None
        if "intervals" in u:
            section = u["intervals"] or {}
            athlete_id = section.get("athlete_id")
            if athlete_id is None or not str(athlete_id).strip():
                raise ValueError(
                    f"The intervals section for user '{name}' has no athlete_id. "
                    f"Run: eufy-sync --setup-intervals"
                )
            intervals = IntervalsConfig(
                athlete_id=str(athlete_id).strip(),
                api_key=_get_intervals_key(name, section.get("api_key"), migrate),
            )

        if not garmin and not strava and not zwift and not intervals:
            raise ValueError(
                f"User '{name}' has no sync targets configured. "
                f"Add a 'garmin', 'strava', 'zwift', and/or 'intervals' section to your config."
            )

        users.append(UserConfig(
            name=name,
            eufy=EufyConfig(
                email=u["eufy"]["email"],
                password=_get_password(name, "eufy", u["eufy"]["email"], u["eufy"].get("password"), migrate),
                customer_id=str(u["eufy"]["customer_id"]) if u["eufy"].get("customer_id") is not None else None,
            ),
            garmin=garmin,
            strava=strava,
            zwift=zwift,
            intervals=intervals,
        ))

    return AppConfig(users=users)
