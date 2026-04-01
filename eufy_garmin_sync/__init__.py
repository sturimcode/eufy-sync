"""Sync Eufy smart scale body composition data to Garmin Connect."""

__version__ = "1.3.0"

# Public API for programmatic use
from eufy_garmin_sync.garmin_auth import GarminAuth
from eufy_garmin_sync.eufy_client import EufyClient, EufyMeasurement
from eufy_garmin_sync.fit import FitEncoder
from eufy_garmin_sync.transform import GarminBodyComposition, transform

__all__ = [
    "GarminAuth",
    "EufyClient",
    "EufyMeasurement",
    "FitEncoder",
    "GarminBodyComposition",
    "transform",
]
