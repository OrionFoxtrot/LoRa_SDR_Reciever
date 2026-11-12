"""Deterministic, receive-only IQ fixtures. TX truth is written to a separate CSV."""
import binascii
import csv
from dataclasses import dataclass
import math
from pathlib import Path
import numpy as np
from scipy.signal import resample_poly
from phy import waveform
from reports import write_json

# Receiver-side golden payloads: first three exact/CRC-valid matches in
# General Diagnostics/100CS_20261109_125123_truth_comparison.csv.
GOLD = [bytes.fromhex(v) for v in (
    "df164b19c08e65d281a00a00970e0100d5fd21fd4b0105001b000c00d000f701d9ff56049449bfa8c539fd0371244312000001c67f80f5",
    "1e114c1949a866d2ed001d002fe60200fd02c4ff7502f2ffeeff070004fe05005eff33351df35c7291111c01711f1f120000028c7f35b3",
    "338a4d19cb0268d2fe201d000beb0200eefc0cfe10011b00f5ff0d00bc017d00e6fe9bb4bd0ee749da46e500601f6012000003a27f9fd2")]


def payload(node, seq):
    b = bytearray(GOLD[(node-1)%3][:53])
    b[48:50] = seq.to_bytes(2, "little")
    b[50] = node
    return bytes(b)+binascii.crc_hqx(b,0xffff).to_bytes(2, "little")


@dataclass
class Event:
    start: float
    carrier: int
    sf: int
    data: bytes
    level: float = .16   # peak complex amplitude relative to ADC full scale
    cfo: float = 0.
    drift: float = 0.
    phase: float = .5
    case: str = ""
    cr: int = 3
    crc: bool = True
    reference: Path | None = None


def generate(path, profile, events, duration, seed=916, noise=.012, description="controlled fixture"):
    path = Path(path)
    path.parent.mkdir(exist_ok=True, parents=True)
    events = sorted(events, key=lambda e:e.start)
    fs = profile.sample_rate_hz
    total = round(duration*fs)
    rng = np.random.default_rng(seed)
    truth, active = [], []
    cursor = 0
    with path.open("wb") as out:
        for at in range(0,total,131072):
            n = min(131072,total-at)
            z = rng.standard_normal(2*n,dtype=np.float32).view(np.complex64)*noise
            active = [(s,w,e) for s,w,e in active if s+len(w)>at]
            while cursor < len(events) and round(events[cursor].start*fs) < at+n:
                e = events[cursor]
                cursor += 1
                start = round(e.start*fs)
                if e.reference:
                    w = resample_poly(np.fromfile(e.reference,np.complex64),4,1)
                else:
                    w = waveform(profile,e.sf,e.data,e.cr,e.crc,fs)
                if start < 0 or start+len(w) > total:
                    raise ValueError("Synthetic event is outside capture")
                active.append((start,w,e))
                truth.append(dict(start_s=start/fs, channel_frequency_hz=e.carrier, sf=e.sf,
                                  payload_hex=e.data.hex(), case=e.case,
                                  nominal_level_dbfs=20*math.log10(e.level), cfo_hz=e.cfo,
                                  drift_hz_s=e.drift, phy_crc=e.crc, cr_denominator=e.cr+4))
            for start,w,e in active:
                a,b = max(start,at),min(start+len(w),at+n)
                t = np.arange(a-start,b-start,dtype=float)/fs
                phase = e.phase + 2*np.pi*t*(e.carrier-profile.center_hz+e.cfo+.5*e.drift*t)
                part = w[a-start:b-start]
                if profile.invert_iq:
                    part = part.conj()
                z[a-at:b-at] += part * (e.level*np.exp(1j*phase)).astype(np.complex64)
            out.write(np.clip(np.rint(z.view(np.float32)*128+127.5),0,255).astype(np.uint8).tobytes())
    if cursor != len(events):
        raise ValueError("Synthetic event starts after EOF")
    write_json(str(path)+".json",dict(format="rtl_sdr unsigned 8-bit interleaved I/Q",
               center_hz=profile.center_hz,sample_rate_hz=fs,synthetic=True,
               generator=description,noise_sigma_per_component=noise,seed=seed,
               bytes_received=2*total,bytes_written=2*total,bytes_dropped=0,gaps=[]))
    with path.with_suffix(".tx.csv").open("w",newline="",encoding="utf-8") as f:
        fields = list(truth[0]) if truth else ["start_s","channel_frequency_hz","sf","payload_hex","case"]
        out = csv.DictWriter(f,fields)
        out.writeheader()
        out.writerows(truth)


def power_events(profile):
    fc = profile.carriers_hz[0]
    events = []
    t, seq = .211813, 1
    for db in (0,-6,6,-12,12,-18,18,-24,24,-30,30):
        for trial in range(2):
            # Positive delta means SF11 is stronger. Same weak level, noise
            # distribution, CFO and waveform in the isolated control and pair.
            levels = {11:.34*10**(min(db,0)/20),12:.34*10**(-max(db,0)/20)}
            for sf in (11,12):
                if (db>0 and sf==11) or (db<0 and sf==12):
                    continue
                events.append(Event(t,fc,sf,payload(sf,seq),levels[sf],(-1823,2244)[sf-11],
                                    0.,trial+.7, f"{db}/control/{sf}/{trial}"))
                t += 3.42
            for sf in (11,12):
                off = .000731 if trial and sf==11 else 0
                events.append(Event(t+off,fc,sf,payload(sf,seq),levels[sf],(-1823,2244)[sf-11],
                                    0.,trial+.7,f"{db}/pair/{sf}/{trial}"))
            t += 3.42
            seq += 1
    return events,t+.1


def fleet_events(profile, periods=3, seed=8521):
    rng = np.random.default_rng(seed)
    events = []
    for fc_index,fc in enumerate(profile.fleet_carriers_hz):
        for j,sf in enumerate([10]*30+[11]*12+[12]*8):
            node = 1+50*fc_index+j
            phase = float(rng.uniform(.12,29.85))
            cfo = float(rng.uniform(-7500,7500))
            level = float(.16*10**(rng.uniform(-7,3)/20))
            for k in range(periods):
                start = phase+30*k+rng.uniform(-.035,.035)
                events.append(Event(start,fc,sf,payload(node,k),level,cfo,float(rng.uniform(-55,55)),
                                    float(rng.uniform(0,2*np.pi)),f"fleet/{k}"))
    return events, periods*30+3.2
