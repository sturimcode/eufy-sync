"""Minimal FIT file encoder for body composition data.

Generates Garmin-compatible FIT binary files for uploading weight and
body composition measurements to Garmin Connect.

Based on the FIT SDK protocol spec and python-garminconnect's implementation.
"""
from __future__ import annotations

import io
import struct
import time as _time
from datetime import datetime
from struct import pack, unpack


# FIT epoch: Dec 31, 1989 00:00:00 UTC
FIT_EPOCH = 631065600

# FIT global message numbers
FILE_ID = 0
FILE_CREATOR = 49
DEVICE_INFO = 23
WEIGHT_SCALE = 30

# FIT base types
ENUM = 0x00
UINT8 = 0x0D
UINT16 = 0x84
UINT32 = 0x86
UINT16Z = 0x8B
STRING = 0x07


def _crc16(data: bytes) -> int:
    """Calculate FIT CRC-16."""
    crc_table = [
        0x0000, 0xCC01, 0xD801, 0x1400, 0xF001, 0x3C00, 0x2800, 0xE401,
        0xA001, 0x6C00, 0x7800, 0xB401, 0x5000, 0x9C01, 0x8801, 0x4400,
    ]
    crc = 0
    for byte in data:
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[byte & 0xF]
        tmp = crc_table[crc & 0xF]
        crc = (crc >> 4) & 0x0FFF
        crc = crc ^ tmp ^ crc_table[(byte >> 4) & 0xF]
    return crc


def _fit_timestamp(dt: datetime) -> int:
    """Convert datetime to FIT timestamp (seconds since FIT epoch)."""
    unix_ts = int(dt.timestamp()) if dt.tzinfo else int(_time.mktime(dt.timetuple()))
    return unix_ts - FIT_EPOCH


class FitEncoder:
    """Builds a FIT binary file in memory."""

    def __init__(self):
        self._buf = io.BytesIO()
        self._data_size = 0
        # Write placeholder header (14 bytes), will be updated in finish()
        self._buf.write(b'\x00' * 14)

    def _write_raw(self, data: bytes) -> None:
        self._buf.write(data)
        self._data_size += len(data)

    def _write_definition(self, local_msg: int, global_msg: int, fields: list) -> None:
        """Write a definition message.

        fields: list of (field_num, size_bytes, base_type)
        """
        # Record header: definition message, local message type
        header = 0x40 | (local_msg & 0x0F)
        self._write_raw(pack('B', header))
        # Reserved byte + architecture (0 = little-endian)
        self._write_raw(pack('BB', 0, 0))
        # Global message number
        self._write_raw(pack('<H', global_msg))
        # Number of fields
        self._write_raw(pack('B', len(fields)))
        # Field definitions
        for field_num, size, base_type in fields:
            self._write_raw(pack('BBB', field_num, size, base_type))

    def _write_data(self, local_msg: int, values: bytes) -> None:
        """Write a data message."""
        header = local_msg & 0x0F
        self._write_raw(pack('B', header))
        self._write_raw(values)

    def write_file_info(self, timestamp: int | None = None) -> None:
        """Write file_id message (required first message in FIT file)."""
        ts = timestamp or int(_time.time()) - FIT_EPOCH
        fields = [
            (0, 1, ENUM),     # type
            (1, 2, UINT16),   # manufacturer
            (2, 2, UINT16),   # product
            (3, 4, 0x8C),  # serial_number (UINT32Z)
            (4, 4, UINT32),   # time_created
        ]
        self._write_definition(0, FILE_ID, fields)
        data = pack('<BHHII', 9, 1, 1, 0, ts)  # type=9 (weight), mfr=1, prod=1
        self._write_data(0, data)

    def write_file_creator(self) -> None:
        """Write file_creator message."""
        fields = [
            (0, 2, UINT16),  # software_version
        ]
        self._write_definition(1, FILE_CREATOR, fields)
        self._write_data(1, pack('<H', 100))

    def write_device_info(self, dt: datetime) -> None:
        """Write device_info message."""
        ts = _fit_timestamp(dt)
        fields = [
            (253, 4, UINT32),  # timestamp
            (0, 2, UINT16Z),   # device_index
            (1, 1, UINT8),     # device_type  (was ENUM, using UINT8)
            (2, 2, UINT16),    # manufacturer
            (3, 2, UINT16Z),   # product
        ]
        self._write_definition(2, DEVICE_INFO, fields)
        data = pack('<IHBHH', ts, 0, 0, 1, 1)
        self._write_data(2, data)

    def write_weight_scale(
        self,
        dt: datetime,
        weight: float,
        percent_fat: float | None = None,
        percent_hydration: float | None = None,
        bone_mass: float | None = None,
        muscle_mass: float | None = None,
        basal_met: float | None = None,
        metabolic_age: float | None = None,
        visceral_fat_rating: float | None = None,
    ) -> None:
        """Write weight_scale message with body composition data."""
        ts = _fit_timestamp(dt)

        # Fields in ascending field number order per FIT spec
        fields = [
            (253, 4, UINT32),  # timestamp
            (0, 2, UINT16),    # weight (scale 100)
            (1, 2, UINT16),    # percent_fat (scale 100)
            (2, 2, UINT16),    # percent_hydration (scale 100)
            (4, 2, UINT16),    # bone_mass (scale 100)
            (5, 2, UINT16),    # muscle_mass (scale 100)
            (7, 2, UINT16),    # basal_met (scale 4)
            (10, 1, UINT8),    # metabolic_age
            (11, 1, UINT8),    # visceral_fat_rating
        ]
        self._write_definition(3, WEIGHT_SCALE, fields)

        # Convert values with scale factors, using 0xFFFF/0xFF for None (invalid)
        def u16(val, scale):
            return int(val * scale) if val is not None else 0xFFFF

        def u8(val):
            return int(val) if val is not None else 0xFF

        data = pack(
            '<IHHHHHHBB',
            ts,
            u16(weight, 100),
            u16(percent_fat, 100),
            u16(percent_hydration, 100),
            u16(bone_mass, 100),
            u16(muscle_mass, 100),
            u16(basal_met, 4),
            u8(metabolic_age),
            u8(visceral_fat_rating),
        )
        self._write_data(3, data)

    def finish(self) -> None:
        """Finalize the FIT file: write header and CRC."""
        # Get all data after the 14-byte header placeholder
        self._buf.seek(14)
        data_bytes = self._buf.read()

        # Build the real header
        header = pack(
            '<BBHI4s',
            14,           # header size
            0x20,         # protocol version (2.0)
            0x08E8,       # profile version (2280)
            len(data_bytes),  # data size
            b'.FIT',      # data type
        )
        header_crc = _crc16(header)
        header += pack('<H', header_crc)

        # Calculate CRC over data
        data_crc = _crc16(data_bytes)

        # Rewrite the file
        self._buf = io.BytesIO()
        self._buf.write(header)
        self._buf.write(data_bytes)
        self._buf.write(pack('<H', data_crc))

    def getvalue(self) -> bytes:
        """Return the complete FIT file as bytes."""
        return self._buf.getvalue()
