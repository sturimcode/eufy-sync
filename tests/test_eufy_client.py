from datetime import datetime, timezone

from eufy_garmin_sync.eufy_client import EufyClient, EufyMeasurement


def test_parse_record_basic():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc123",
        "device_id": "dev789",
        "update_time": 1711900000,
        "create_time": 1711900000,
        "scale_data": {
            "weight": 862,  # decigrams -> 86.2 kg
            "body_fat": 18.5,
            "muscle_mass": 45.2,
            "water": 55.3,
            "bone_mass": 3.2,
            "bmr": 1650,
            "visceral_fat": 8.0,
            "body_age": 28,
            "bmi": 23.1,
        },
    }
    m = client._parse_record(record)
    assert m is not None
    assert m.weight_kg == 86.2  # 862 / 10
    assert m.measurement_id == "abc123_1711900000"
    assert m.customer_id == "abc123"
    assert m.body_fat_pct == 18.5
    assert m.metabolic_age == 28
    assert m.timestamp == datetime(2024, 3, 31, 15, 46, 40, tzinfo=timezone.utc)


def test_parse_record_missing_scale_data():
    client = EufyClient.__new__(EufyClient)
    record = {"customer_id": "abc", "update_time": 100}
    assert client._parse_record(record) is None


def test_parse_record_zero_weight():
    client = EufyClient.__new__(EufyClient)
    record = {
        "customer_id": "abc",
        "update_time": 100,
        "scale_data": {"weight": 0},
    }
    assert client._parse_record(record) is None
