"""Sync Eufy smart scale body composition data to Garmin Connect and Strava."""

__version__ = "1.12.0"

# Public API for programmatic use
from eufy_sync.eufy_client import EufyClient, EufyMeasurement
from eufy_sync.garmin_auth import GarminAuth
from eufy_sync.strava_client import StravaClient
from eufy_sync.transform import GarminBodyComposition, transform

__all__ = [
    "GarminAuth",
    "EufyClient",
    "EufyMeasurement",
    "GarminBodyComposition",
    "StravaClient",
    "transform",
]
