"""Sync Eufy smart scale body composition data to Garmin Connect and Strava."""

__version__ = "1.15.1"

# Public API for programmatic use
from eufy_sync.eufy_client import EufyClient, EufyMeasurement
from eufy_sync.strava_client import StravaClient
from eufy_sync.transform import GarminBodyComposition, transform


def __getattr__(name):
    # GarminAuth pulls in garminconnect, and through it curl_cffi. Importing it
    # on first use keeps every command that never talks to Garmin, --update
    # above all, working on a machine where either one fails to load.
    if name == "GarminAuth":
        from eufy_sync.garmin_auth import GarminAuth
        return GarminAuth
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "GarminAuth",
    "EufyClient",
    "EufyMeasurement",
    "GarminBodyComposition",
    "StravaClient",
    "transform",
]
