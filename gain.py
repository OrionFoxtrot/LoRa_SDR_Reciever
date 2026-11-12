# Copyright (c) 2026 Mark Lohatepanont. MIT; see licenses/DeSCENT-MIT.txt.
# Adapted from lrfhss_autoset.py. Gain-only scan, no LR-FHSS trigger estimation.
import math
import struct
import numpy as np

CLIP_FRACTION = 1e-4

# librtlsdr's advertised gain tables (tenths of dB). rtl_tcp's RTL0 header
# provides the tuner type and table length, but does not transmit the table.
# https://github.com/osmocom/rtl-sdr/blob/master/src/librtlsdr.c
_GAIN_TABLES = {
    1: (-10, 15, 40, 65, 90, 115, 140, 165, 190, 215, 240, 290, 340, 420),
    2: (-99, -40, 71, 179, 192),
    3: (-99, -73, -65, -63, -60, -58, -54, 58, 61, 63, 65, 67, 68, 70,
        71, 179, 181, 182, 184, 186, 188, 191, 197),
    5: (0, 9, 14, 27, 37, 77, 87, 125, 144, 157, 166, 197, 207, 229,
        254, 280, 297, 328, 338, 364, 372, 386, 402, 421, 434, 439, 445, 480, 496),
}
_GAIN_TABLES[6] = _GAIN_TABLES[5]
TARGET_RMS = 4.0
PEAK_LIMIT = 64.0             # ADC centre to rail is 127.5 steps


class AutoSetCancelled(Exception):
    pass


def supported_gains(dongle_header: bytes) -> tuple[float, ...]:
    if len(dongle_header) != 12 or dongle_header[:4] != b"RTL0":
        raise ValueError("Invalid RTL-SDR dongle header")
    tuner, count = struct.unpack("!II", dongle_header[4:])
    table = _GAIN_TABLES.get(tuner, ())
    if not table or len(table) != count:
        raise ValueError("Auto Set does not recognize this tuner's gain table; set gain manually.")
    # Radio.open explicitly selects manual tuner gain over the socket, so
    # 0 dB is available even on servers that interpret "-g 0" as AGC.
    return tuple(g / 10 for g in table)


def adc_level(raw: bytes) -> dict:
    a = np.frombuffer(raw, dtype=np.uint8)
    if len(a) < 4000 or len(a) % 2:
        raise ValueError("Not enough complete IQ samples for Auto Set")
    pairs = a.reshape(-1, 2).astype(np.float32)
    # Remove DC separately in I and Q. Otherwise DC can masquerade as noise.
    centred = pairs - pairs.mean(axis=0)
    frames = centred[:len(centred)//2000*2000].reshape(-1, 2000, 2)
    rms = np.sqrt(np.mean(frames*frames, axis=(1, 2)))
    return dict(rms_lsb=float(np.quantile(rms, .2)),
                peak_lsb=float(np.quantile(np.abs(pairs-127.5), .9999)),
                clip_fraction=float(np.mean(np.any((pairs == 0) | (pairs == 255), axis=1))))


def _safe(level):
    return level["clip_fraction"] <= CLIP_FRACTION and level["peak_lsb"] <= PEAK_LIMIT


def run_autoset(gains, initial_gain, *, set_gain, capture,
                cancelled=lambda: False, progress=print):
    gains = tuple(sorted(gains))
    if not gains:
        raise ValueError("No supported manual gain settings")
    observations = []
    gain = min(gains, key=lambda g: abs(g-initial_gain))
    for attempt in range(5):
        if cancelled():
            raise AutoSetCancelled()
        progress(f"Auto Set: measuring gain {gain:g} dB ({attempt+1}/5)...")
        set_gain(gain)
        level = adc_level(capture(.25))
        observations.append(dict(gain_db=gain, **level))
        rms, peak = level["rms_lsb"], level["peak_lsb"]
        if rms < .1:
            raise ValueError("IQ has no measurable variation; check the source before Auto Set.")
        change = min(20*math.log10(TARGET_RMS/rms), 20*math.log10(PEAK_LIMIT/max(peak, .5)))
        if level["clip_fraction"] > CLIP_FRACTION:
            change = min(change, -8.)
        target = gain + change
        next_gain = min(gains, key=lambda g: abs(g-target))
        if change < 0:
            next_gain = max((g for g in gains if g <= target), default=gains[0])
        if next_gain in [row["gain_db"] for row in observations]:
            break
        gain = next_gain
    safe = [row for row in observations if _safe(row)]
    if not safe:
        raise ValueError("No gain with sufficient ADC headroom was found; lower gain or reduce the RF input.")
    best = min(safe, key=lambda row: abs(math.log(max(row["rms_lsb"], .1)/TARGET_RMS)))
    gain = best["gain_db"]
    if cancelled():
        raise AutoSetCancelled()
    progress(f"Auto Set: checking background at {gain:g} dB...")
    set_gain(gain)
    raw = capture(2.)
    final_level = adc_level(raw)
    if not _safe(final_level):
        raise ValueError("Signal levels changed during Auto Set; settings kept. Retry with representative traffic.")
    return dict(gain_db=gain, level=final_level, observations=observations)
