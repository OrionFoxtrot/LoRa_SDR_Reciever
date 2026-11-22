# Copyright (c) 2026 Mark Lohatepanont. MIT; see licenses/DeSCENT-MIT.txt.
# Adapted from lrfhss_autoset.py. LoRa uses a gain-only scan and bounded headroom rechecks.
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


def manual_gain_db(value):
    try:
        gain = float(value)
    except (ValueError, TypeError):
        gain = math.nan
    if not -10 <= gain <= 60:
        raise ValueError("Enter a manual tuner gain between -10 and 60 dB.")
    return gain


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
    centred = pairs - pairs.mean(axis=0, dtype=np.float64).astype(np.float32)
    frames = centred[:len(centred)//2000*2000].reshape(-1, 2000, 2)
    rms = np.sqrt(np.mean(frames*frames, axis=(1, 2)))
    return dict(rms_lsb=float(np.quantile(rms, .2)), max_rms_lsb=float(rms.max()),
                peak_lsb=float(np.quantile(np.abs(pairs-127.5), .9999)),
                clip_fraction=float(np.mean(np.any((pairs == 0) | (pairs == 255), axis=1))))


def _safe(level):
    return level["clip_fraction"] <= CLIP_FRACTION and level["peak_lsb"] <= PEAK_LIMIT


def run_autoset(gains, initial_gain, *, set_gain, capture,
                cancelled=lambda: False, progress=print):
    gains = tuple(sorted(gains))
    if not gains:
        raise ValueError("No supported manual gain settings")

    def measure(gain, seconds):
        if cancelled():
            raise AutoSetCancelled()
        set_gain(gain)
        raw = capture(seconds)
        if cancelled():
            raise AutoSetCancelled()
        level = adc_level(raw)
        if level["max_rms_lsb"] < .1:
            raise ValueError("IQ has no measurable variation; check the source before Auto Set.")
        return level

    observations = []
    gain = min(gains, key=lambda g: abs(g-initial_gain))
    for attempt in range(5):
        progress(f"Auto Set: measuring gain {gain:g} dB ({attempt+1}/5)...")
        level = measure(gain, .25)
        observations.append(dict(gain_db=gain, **level))
        rms, peak = max(level["rms_lsb"], .1), level["peak_lsb"]
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
    best = min(safe, key=lambda row: abs(math.log(max(row["rms_lsb"], .1)/TARGET_RMS)), default=None)
    gain = best["gain_db"] if best else gains[0]
    checks = []
    # A packet may first appear during the longer measurement. Step down once
    # using its peak, then verify minimum gain if the input grows again.
    for attempt in range(3):
        progress(f"Auto Set: checking headroom at {gain:g} dB...")
        level = measure(gain, 2.)
        checks.append(dict(gain_db=gain, **level))
        if _safe(level) or gain == gains[0]:
            break
        # Leave 3 dB of margin. A clipped peak understates the input level.
        reduction = max(3., 20*math.log10(max(level["peak_lsb"], .5)/PEAK_LIMIT) + 3.)
        if level["clip_fraction"] > CLIP_FRACTION:
            reduction = max(8., reduction)
        target = gain - reduction if attempt == 0 else gains[0]
        gain = max((g for g in gains if g <= target), default=gains[0])
        progress(f"Auto Set: signal exceeded headroom. Reducing gain to {gain:g} dB.")

    warning = None
    if not _safe(level):
        issue = "clipping" if level["clip_fraction"] > CLIP_FRACTION else "limited ADC headroom"
        warning = f"Auto Set measured {issue} at minimum gain ({gain:g} dB)."
    elif gain == gains[0]:
        warning = f"Auto Set reached minimum tuner gain ({gain:g} dB)."
    progress(f"Auto Set selected {gain:g} dB.")
    if warning:
        progress(warning)
    return dict(gain_db=gain, level=level, observations=observations, headroom_checks=checks, warning=warning)
