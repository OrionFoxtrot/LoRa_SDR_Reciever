"""Replay-only, single additional blind search on a fitted CRC-gated residual."""
from dataclasses import dataclass
import numpy as np
from phy import encode, symbol_plan


@dataclass
class Donor:
    start: int
    end: int
    edges: np.ndarray
    plan: list
    sf: int
    offset: float
    slope: float
    strength: float
    clock_scale: float


class Cancellation:
    def __init__(self, profile, packets):
        self.profile = p = profile
        self.donors = []
        for row in packets:
            if row["phy_crc_ok"] is not True or row["application_crc_ok"] is False:
                continue
            sf = row["sf"]
            data = bytes.fromhex(row["payload_hex"])
            symbols = encode(p, sf, data, row["cr_denominator"]-4, True)
            plan = symbol_plan(p, sf, symbols)
            lengths = [length*(p.sample_rate_hz//p.bandwidth_hz) for length, _, _ in plan]
            scale = 1+row.get("clock_error_ppm",0.)*1e-6
            edges = np.rint(np.r_[0, np.cumsum(lengths)]*scale).astype(np.int64)
            start = row["start_sample"]
            self.donors.append(Donor(start, start+int(edges[-1]), edges, plan, sf,
                                     row["carrier_hz"]-p.center_hz+row["cfo_hz"],
                                     row["drift_hz_s"], row["preamble_power_dbfs"],scale))
        self.donors.sort(key=lambda d: d.start)
        self.cursor = 0
        self.active = []
        self.fitted = self.skipped = 0
        self.removed_energy = 0.

    def apply(self, first, iq):
        end = first+len(iq)
        self.active = [d for d in self.active if d.end > first]
        while self.cursor < len(self.donors) and self.donors[self.cursor].start < end:
            donor = self.donors[self.cursor]
            if donor.end > first:
                self.active.append(donor)
            self.cursor += 1
        for donor in sorted(self.active, key=lambda d: -d.strength):
            self.subtract(first, iq, donor)

    def subtract(self, first, iq, d):
        p = self.profile
        left = max(first, d.start) - d.start
        right = min(first+len(iq), d.end) - d.start
        index = int(d.edges.searchsorted(left, side="right")-1)
        n = 1 << d.sf
        os = p.sample_rate_hz//p.bandwidth_hz
        while left < right:
            stop = min(right, int(d.edges[index+1]))
            _, direction, shift = d.plan[index]
            if stop-left >= 64:
                t = np.arange(left, stop, dtype=np.float64)
                x = ((t-d.edges[index])/(os*d.clock_scale) + shift) % n
                t /= p.sample_rate_hz
                direction *= -1 if p.invert_iq else 1
                phase = direction*.5*x*(x/n-1) + t*(d.offset + .5*d.slope*t)
                ref = np.exp(2j*np.pi*phase).astype(np.complex64)
                y = iq[d.start+left-first:d.start+stop-first]
                gain = np.vdot(ref, y)/len(y)
                energy = float(np.vdot(y, y).real)
                removed = float(abs(gain)**2)*len(y)
                # LS estimates complex channel gain independently for each
                # symbol/block intersection. Reject poorly supported fits.
                if removed > .09*energy and energy > 0:
                    y -= gain*ref
                    self.fitted += 1
                    self.removed_energy += removed
                else:
                    self.skipped += 1
            left = stop
            index += 1


def same_detection(a, b):
    return (a["carrier_hz"] == b["carrier_hz"] and a["sf"] == b["sf"]
            and abs(a["start_sample"]-b["start_sample"]) <= 64)


def merge_passes(primary, residual):
    accepted = list(primary)
    repeated = 0
    for row in residual:
        matches = [i for i, p in enumerate(accepted) if same_detection(p, row)]
        if not matches:
            accepted.append(row)
        elif row["phy_crc_ok"] is True:
            good = [i for i in matches if accepted[i]["phy_crc_ok"] is True]
            if not good:
                accepted[matches[0]] = row
            elif any(accepted[i]["payload_hex"] == row["payload_hex"] for i in good):
                repeated += 1
            else:
                accepted.append(row)
    return sorted(accepted, key=lambda r:r["start_sample"]), repeated
