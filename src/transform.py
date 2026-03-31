from __future__ import annotations

from dataclasses import dataclass

from src.eufy_client import EufyMeasurement


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


def transform(measurement: EufyMeasurement) -> GarminBodyComposition | None:
    """Map a Eufy measurement to Garmin body composition fields.

    Returns None if the measurement fails validation.
    """
    if not MIN_WEIGHT_KG <= measurement.weight_kg <= MAX_WEIGHT_KG:
        return None

    return GarminBodyComposition(
        timestamp=measurement.timestamp.isoformat(),
        weight=measurement.weight_kg,
        percent_fat=measurement.body_fat_pct,
        percent_hydration=measurement.water_pct,
        visceral_fat_rating=measurement.visceral_fat_level,
        bone_mass=measurement.bone_mass_kg,
        muscle_mass=measurement.muscle_mass_kg,
        basal_met=measurement.bmr_kcal,
        metabolic_age=measurement.metabolic_age,
        bmi=None,  # Let Garmin calculate from weight + height
    )
