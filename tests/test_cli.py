from __future__ import annotations

import os
from pathlib import Path

import yaml

from eufy_garmin_sync.cli import _write_config


def test_write_config_creates_file_with_restricted_permissions(tmp_path: Path):
    config_path = tmp_path / "subdir" / "config.yaml"
    config = {"users": [{"name": "test"}]}

    _write_config(config_path, config)

    assert config_path.exists()
    # File should be 600 (owner read/write only)
    mode = oct(config_path.stat().st_mode)[-3:]
    assert mode == "600"

    # Content should be valid YAML
    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["users"][0]["name"] == "test"


def test_write_config_parent_directory_is_restricted(tmp_path: Path):
    config_path = tmp_path / "secure_dir" / "config.yaml"
    _write_config(config_path, {"test": True})

    parent_mode = oct(config_path.parent.stat().st_mode)[-3:]
    assert parent_mode == "700"


def test_write_config_overwrites_existing(tmp_path: Path):
    config_path = tmp_path / "config.yaml"

    _write_config(config_path, {"version": 1})
    _write_config(config_path, {"version": 2})

    with open(config_path) as f:
        loaded = yaml.safe_load(f)
    assert loaded["version"] == 2
