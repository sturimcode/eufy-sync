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
    # Temp file + atomic rename, matching credentials._save_vault_to_file: an
    # interrupted in-place write would truncate config.yaml, and every command
    # that rewrites it (setup, migration, --select-profile) does so on top of
    # the only copy of the user's emails and settings. The temp name carries
    # the pid so two concurrent writers never share one temp inode.
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            yaml.dump(config, f, default_flow_style=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Leave the previous config untouched; drop the partial temp file.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
