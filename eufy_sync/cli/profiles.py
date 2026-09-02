"""Eufy multi-profile selection: listing, prompting, and persisting a choice."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from eufy_sync.cli import shared

if TYPE_CHECKING:
    from eufy_sync.eufy_client import EufyProfile


def _format_profile(profile: EufyProfile, index: int) -> str:
    lb = profile.last_weight_kg * 2.20462
    when = profile.last_measured.strftime("%Y-%m-%d")
    label = profile.name or f"profile ...{profile.customer_id[-4:]}"
    return f"  {index}. {label}  -  {profile.last_weight_kg:.1f} kg ({lb:.1f} lb), last weigh-in {when}"


def _prompt_profile_choice(profiles: list) -> str:
    """Print the profiles and return the customer_id the user picks."""
    print("")
    print("Multiple profiles were found on this Eufy account:")
    for i, p in enumerate(profiles, 1):
        print(_format_profile(p, i))
    print("")
    while True:
        choice = input(f"Which profile is yours? [1-{len(profiles)}] ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(profiles):
            return profiles[int(choice) - 1].customer_id
        print("Enter a number from the list.")


def _save_customer_id(config_path: Path, customer_id: str) -> None:
    """Persist the chosen Eufy customer_id into the config file (single user)."""
    with open(config_path) as f:
        config = yaml.safe_load(f)
    user = config["users"][0]
    user.setdefault("eufy", {})
    user["eufy"]["customer_id"] = customer_id
    shared._write_config(config_path, config)


def _select_profile(config_path: Path) -> None:
    """Choose which Eufy profile to sync, for an existing install."""
    if not config_path.exists():
        print("No config found. Run eufy-sync first to set up.")
        sys.exit(1)

    from eufy_sync.config import load_config
    from eufy_sync.eufy_client import EufyClient

    cfg = load_config(config_path)
    eufy = EufyClient(cfg.users[0].eufy)
    try:
        eufy.authenticate()
        profiles = eufy.list_profiles()
    except Exception as e:
        print(f"Could not reach Eufy: {e}")
        sys.exit(1)
    finally:
        eufy.close()

    if not profiles:
        print("No profiles found yet. Weigh in and open the Eufy app, then try again.")
        return

    if len(profiles) == 1:
        _save_customer_id(config_path, profiles[0].customer_id)
        print("Only one profile found on this account; saved it as yours.")
        return

    _save_customer_id(config_path, _prompt_profile_choice(profiles))
    print("Saved. Future syncs will use only your profile.")
