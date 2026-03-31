from datetime import datetime, timezone

from src.eufy_client import EufyMeasurement
from src.transform import MAX_WEIGHT_KG, MIN_WEIGHT_KG, transform


def _make_measurement(**overrides) -> EufyMeasurement:
    defaults = {
        "measurement_id": "cust123_1711900000",
        "customer_id": "cust123",
        "device_id": "dev456",
        "timestamp": datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        "weight_kg": 86.2,
        "body_fat_pct": 18.5,
        "muscle_mass_kg": 45.2,
        "water_pct": 55.3,
        "bone_mass_kg": 3.2,
        "bmr_kcal": 1650,
        "visceral_fat_level": 8.0,
        "metabolic_age": 28,
        "bmi": 23.1,
    }
    defaults.update(overrides)
    return EufyMeasurement(**defaults)


def test_basic_transform():
    m = _make_measurement()
    result = transform(m)
    assert result is not None
    assert result.weight == 86.2
    assert result.percent_fat == 18.5
    assert result.percent_hydration == 55.3
    assert result.visceral_fat_rating == 8.0
    assert result.bone_mass == 3.2
    assert result.muscle_mass == 45.2
    assert result.basal_met == 1650
    assert result.metabolic_age == 28
    assert result.bmi is None  # We skip BMI, let Garmin calculate


def test_timestamp_is_iso():
    m = _make_measurement()
    result = transform(m)
    assert result is not None
    assert result.timestamp == "2024-04-01T12:00:00+00:00"


def test_none_fields_pass_through():
    m = _make_measurement(body_fat_pct=None, muscle_mass_kg=None, visceral_fat_level=None)
    result = transform(m)
    assert result is not None
    assert result.percent_fat is None
    assert result.muscle_mass is None
    assert result.visceral_fat_rating is None


def test_rejects_weight_too_low():
    m = _make_measurement(weight_kg=MIN_WEIGHT_KG - 1)
    assert transform(m) is None


def test_rejects_weight_too_high():
    m = _make_measurement(weight_kg=MAX_WEIGHT_KG + 1)
    assert transform(m) is None


def test_accepts_boundary_weights():
    low = _make_measurement(weight_kg=MIN_WEIGHT_KG)
    high = _make_measurement(weight_kg=MAX_WEIGHT_KG)
    assert transform(low) is not None
    assert transform(high) is not None
