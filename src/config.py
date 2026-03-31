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


@dataclass
class GarminConfig:
    email: str
    password: str


@dataclass
class UserConfig:
    name: str
    eufy: EufyConfig
    garmin: GarminConfig


@dataclass
class AppConfig:
    sync_interval_minutes: int
    log_level: str
    users: list[UserConfig]


def _interpolate_env_vars(value: str) -> str:
    """Replace ${VAR_NAME} with environment variable values."""
    def replacer(match: re.Match) -> str:
        var_name = match.group(1)
        env_value = os.environ.get(var_name)
        if env_value is None:
            raise ValueError(f"Environment variable '{var_name}' is not set")
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


def load_config(path: Path) -> AppConfig:
    with open(path) as f:
        raw = yaml.safe_load(f)

    raw = _walk_and_interpolate(raw)

    users = []
    for u in raw["users"]:
        users.append(UserConfig(
            name=u["name"],
            eufy=EufyConfig(email=u["eufy"]["email"], password=u["eufy"]["password"]),
            garmin=GarminConfig(email=u["garmin"]["email"], password=u["garmin"]["password"]),
        ))

    return AppConfig(
        sync_interval_minutes=raw.get("sync_interval_minutes", 15),
        log_level=raw.get("log_level", "INFO"),
        users=users,
    )
