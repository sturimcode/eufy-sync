from __future__ import annotations

from datetime import datetime, timezone

from eufy_sync.fit import FitEncoder, _crc16, _fit_timestamp, FIT_EPOCH


def test_fit_timestamp_conversion():
    dt = datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts = _fit_timestamp(dt)
    # FIT epoch is Dec 31 1989 00:00 UTC = unix 631065600
    expected = int(dt.timestamp()) - FIT_EPOCH
    assert ts == expected
    assert ts > 0


def test_crc16_known_value():
    # CRC of empty bytes should be 0
    assert _crc16(b"") == 0
    # CRC of known data should be deterministic
    crc1 = _crc16(b"hello")
    crc2 = _crc16(b"hello")
    assert crc1 == crc2
    assert crc1 != 0  # non-trivial input should produce non-zero CRC


def test_fit_file_magic_bytes():
    encoder = FitEncoder()
    encoder.write_file_info()
    encoder.write_file_creator()
    encoder.write_device_info(datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc))
    encoder.write_weight_scale(
        datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        weight=86.2,
    )
    encoder.finish()
    data = encoder.getvalue()

    # FIT header: 14 bytes, starts with header size, contains '.FIT'
    assert len(data) >= 14
    assert data[0] == 14  # header size
    assert data[8:12] == b'.FIT'


def test_fit_file_minimum_size():
    encoder = FitEncoder()
    encoder.write_file_info()
    encoder.write_file_creator()
    encoder.write_device_info(datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc))
    encoder.write_weight_scale(
        datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        weight=86.2,
    )
    encoder.finish()
    data = encoder.getvalue()

    # 14 byte header + definition messages + data messages + 2 byte CRC
    # Should be well over 50 bytes for a complete file
    assert len(data) > 50


def test_fit_file_ends_with_crc():
    encoder = FitEncoder()
    encoder.write_file_info()
    encoder.write_file_creator()
    encoder.write_device_info(datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc))
    encoder.write_weight_scale(
        datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc),
        weight=86.2,
    )
    encoder.finish()
    data = encoder.getvalue()

    # Last 2 bytes are the data CRC
    # Verify CRC matches the data section
    header_size = 14
    data_section = data[header_size:-2]
    expected_crc = _crc16(data_section)
    actual_crc = int.from_bytes(data[-2:], byteorder='little')
    assert actual_crc == expected_crc


def test_weight_scale_with_all_fields():
    encoder = FitEncoder()
    encoder.write_file_info()
    encoder.write_file_creator()
    dt = datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    encoder.write_device_info(dt)
    encoder.write_weight_scale(
        dt,
        weight=86.2,
        percent_fat=18.5,
        percent_hydration=55.3,
        bone_mass=3.2,
        muscle_mass=45.2,
        basal_met=1650,
        metabolic_age=28,
        visceral_fat_rating=8.0,
    )
    encoder.finish()
    data = encoder.getvalue()

    # Should produce a valid FIT file
    assert data[8:12] == b'.FIT'
    assert len(data) > 50


def test_weight_scale_with_none_fields():
    encoder = FitEncoder()
    encoder.write_file_info()
    encoder.write_file_creator()
    dt = datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    encoder.write_device_info(dt)
    encoder.write_weight_scale(
        dt,
        weight=86.2,
        # All optional fields default to None
    )
    encoder.finish()
    data = encoder.getvalue()

    assert data[8:12] == b'.FIT'
    assert len(data) > 50


def test_different_weights_produce_different_files():
    def make_fit(weight):
        encoder = FitEncoder()
        encoder.write_file_info()
        encoder.write_file_creator()
        dt = datetime(2024, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
        encoder.write_device_info(dt)
        encoder.write_weight_scale(dt, weight=weight)
        encoder.finish()
        return encoder.getvalue()

    fit1 = make_fit(80.0)
    fit2 = make_fit(90.0)
    assert fit1 != fit2
