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
class UserConfig:
    name: str
    eufy: EufyConfig
    garmin: GarminConfig | None = None
    strava: StravaConfig | None = None


@dataclass
class AppConfig:
    sync_interval_minutes: int
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


def _get_password(user_name: str, service: str, email: str, yaml_password: str | None) -> str:
    """Resolve password: credential store first, then YAML fallback."""
    from eufy_sync.credentials import get_password

    key = f"{user_name}:{service}"
    stored = get_password(key)
    if stored:
        return stored

    if yaml_password:
        return yaml_password

    raise ValueError(
        f"No {service} password found for user '{user_name}'. "
        f"Run: eufy-sync --update-password"
    )


def load_config(path: Path) -> AppConfig:
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
                password=_get_password(name, "garmin", u["garmin"]["email"], u["garmin"].get("password")),
            )

        strava = None
        if "strava" in u:
            strava = StravaConfig(
                client_id=str(u["strava"]["client_id"]),
                client_secret=u["strava"]["client_secret"],
            )

        if not garmin and not strava:
            raise ValueError(
                f"User '{name}' has no sync targets configured. "
                f"Add a 'garmin' and/or 'strava' section to your config."
            )

        users.append(UserConfig(
            name=name,
            eufy=EufyConfig(
                email=u["eufy"]["email"],
                password=_get_password(name, "eufy", u["eufy"]["email"], u["eufy"].get("password")),
                customer_id=str(u["eufy"]["customer_id"]) if u["eufy"].get("customer_id") is not None else None,
            ),
            garmin=garmin,
            strava=strava,
        ))

    return AppConfig(
        sync_interval_minutes=raw.get("sync_interval_minutes", 15),
        users=users,
    )
