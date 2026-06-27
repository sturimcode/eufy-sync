from __future__ import annotations

from dataclasses import dataclass

from eufy_sync.eufy_client import EufyMeasurement


@dataclass
class GarminBodyComposition:
    timestamp: str  # ISO 8601
    weight: float  # kg
    percent_fat: float | None = None
    percent_hydration: float | None = None
    visceral_fat_rating: float | None = None
    bone_mass: float | None = None  # kg
    muscle_mass: float | None = None  # kg
    basal_met: float | None = None  # kcal/day
    metabolic_age: float | None = None
    bmi: float | None = None


# Reject readings outside plausible range (catches scale errors / phantom readings)
MIN_WEIGHT_KG = 22.7  # ~50 lbs
MAX_WEIGHT_KG = 181.4  # ~400 lbs


def _clamp_or_none(val: float | None, lo: float, hi: float) -> float | None:
    """Return val if within bounds, None otherwise (bad reading)."""
    if val is None:
        return None
    return val if lo <= val <= hi else None


def transform(measurement: EufyMeasurement) -> GarminBodyComposition | None:
    """Map a Eufy measurement to Garmin body composition fields.

    Returns None if the measurement fails weight validation.
    Other metrics are silently dropped if out of range.
    """
    if not MIN_WEIGHT_KG <= measurement.weight_kg <= MAX_WEIGHT_KG:
        return None

    return GarminBodyComposition(
        timestamp=measurement.timestamp.isoformat(),
        weight=measurement.weight_kg,
        percent_fat=_clamp_or_none(measurement.body_fat_pct, 3.0, 60.0),
        percent_hydration=_clamp_or_none(measurement.water_pct, 20.0, 80.0),
        visceral_fat_rating=_clamp_or_none(measurement.visceral_fat_level, 1.0, 59.0),
        bone_mass=_clamp_or_none(measurement.bone_mass_kg, 0.5, 10.0),
        muscle_mass=_clamp_or_none(measurement.muscle_mass_kg, 10.0, 120.0),
        basal_met=_clamp_or_none(measurement.bmr_kcal, 500, 5000),
        metabolic_age=_clamp_or_none(measurement.metabolic_age, 10, 120),
        bmi=None,  # Let Garmin calculate from weight + height
    )
