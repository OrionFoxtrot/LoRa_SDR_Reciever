# Copyright (c) 2026 Mark Lohatepanont. MIT; see licenses/DeSCENT-MIT.txt.
# Extracted flight decoder. The LR-FHSS display and ASCII handling were omitted.
from __future__ import annotations

import struct
from typing import Any

PACKET_LENGTH = 55
DATA_LENGTH = 53

# DeSCENT V2.5 binary telemetry layout (little endian).
PACKET_STRUCT = struct.Struct("<4i3h3h3h4hhHHHBBBH")


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16-CCITT-FALSE: poly 0x1021, init 0xFFFF, xorout 0."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def decode_descent_packet(payload: bytes) -> dict[str, Any]:
    """Decode one CRC-valid 55-byte DeSCENT V2.5 telemetry packet."""
    if len(payload) != PACKET_LENGTH:
        raise ValueError(
            f"Expected {PACKET_LENGTH} bytes, received {len(payload)}"
        )

    received_crc = int.from_bytes(payload[53:55], "little")
    calculated_crc = crc16_ccitt_false(payload[:53])
    if received_crc != calculated_crc:
        raise ValueError(
            f"Application CRC mismatch: received 0x{received_crc:04X}, "
            f"calculated 0x{calculated_crc:04X}"
        )

    (
        latitude_e7,
        longitude_e7,
        gps_altitude_msl_mm,
        environmental_altitude_cm,
        ax_raw, ay_raw, az_raw,
        gx_raw, gy_raw, gz_raw,
        mx_raw, my_raw, mz_raw,
        qi_raw, qj_raw, qk_raw, qr_raw,
        temperature_raw,
        pressure_raw,
        humidity_raw,
        packet_counter,
        csid,
        soc_x2,
        validity,
        crc_field,
    ) = PACKET_STRUCT.unpack(payload)

    valid = {
        "linear_acceleration": bool(validity & 0x01),
        "gyroscope": bool(validity & 0x02),
        "magnetometer": bool(validity & 0x04),
        "quaternion": bool(validity & 0x08),
        "gps": bool(validity & 0x10),
        "state_of_charge": bool(validity & 0x20),
        "environmental": bool(validity & 0x40),
        "all_data_fresh": bool(validity & 0x80),
    }

    return {
        "packet_type": "DeSCENT_V2_5",
        "length_bytes": len(payload),
        "application_crc_ok": True,
        "application_crc16": crc_field,
        "csid": csid,
        "packet_counter": packet_counter,
        "validity_raw": validity,
        "valid": valid,
        "latitude_deg": latitude_e7 / 1e7,
        "longitude_deg": longitude_e7 / 1e7,
        "gps_altitude_msl_m": gps_altitude_msl_mm / 1000.0,
        "environmental_altitude_m": environmental_altitude_cm / 100.0,
        "linear_acceleration_mps2": (
            ax_raw / 100.0,
            ay_raw / 100.0,
            az_raw / 100.0,
        ),
        "gyroscope_deg_s": (
            gx_raw / 10.0,
            gy_raw / 10.0,
            gz_raw / 10.0,
        ),
        "magnetic_field_uT": (
            mx_raw / 10.0,
            my_raw / 10.0,
            mz_raw / 10.0,
        ),
        "quaternion_ijkr": (
            qi_raw / 32767.0,
            qj_raw / 32767.0,
            qk_raw / 32767.0,
            qr_raw / 32767.0,
        ),
        "temperature_C": temperature_raw / 100.0,
        "pressure_hPa": pressure_raw / 10.0,
        "humidity_percent": humidity_raw / 100.0,
        "state_of_charge_percent": soc_x2 / 2.0,
    }

