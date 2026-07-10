"""Path constants and small helpers shared across the cli package."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

DATA_DIR = Path.home() / ".garmin-sync"
DEFAULT_CONFIG = DATA_DIR / "config.yaml"
DEFAULT_DB = DATA_DIR / "state.db"
LOG_FILE = DATA_DIR / "sync.log"

UPDATE_CHECK_INTERVAL = 604800  # check once per week


def _write_config(path: Path, config: dict) -> None:
    """Write config with restricted permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        yaml.dump(config, f, default_flow_style=False)


def _configure_logging(verbose: bool) -> None:
    """Set up logging. On normal runs, quiet chatty library loggers; under
    --verbose, show full detail. The garminconnect library logs a warning for
    each login strategy that hits a 429 before a later strategy succeeds, which
    looks alarming on an otherwise successful login."""
    import logging
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if verbose:
        logging.basicConfig(level=logging.DEBUG, format=fmt)
        logging.getLogger("garminconnect").setLevel(logging.DEBUG)
    else:
        logging.basicConfig(level=logging.WARNING, format=fmt)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("garminconnect").setLevel(logging.ERROR)
