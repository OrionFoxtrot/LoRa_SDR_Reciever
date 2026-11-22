"""Replay-only, single additional blind search on a fitted CRC-gated residual."""
from dataclasses import dataclass
from bisect import bisect_left, insort
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
    def __init__(self, profile, packets, cancelled=None):
        self.profile = p = profile
        self.donors = []
        for row in packets:
            if cancelled and cancelled():
                break
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


def repeat_window(row,sample_rate_hz=1_000_000):
    """One symbol bounds a weak residual's preamble jitter for an identical payload."""
    bw=row.get("bandwidth_hz")
    return max(64,round(sample_rate_hz*(1<<int(row["sf"]))/int(bw))) if bw else 64


def merge_passes(primary, residual, cancelled=None, sample_rate_hz=1_000_000):
    accepted = list(primary)
    # Search the candidate's 65-sample bucket and its two neighbours.
    # Keeping the original insertion order preserves the previous tie behaviour.
    bins={}
    fingerprints={}
    def key(row):
        return row["carrier_hz"],row["sf"],row["start_sample"]//65
    def identity(row):
        return row["carrier_hz"],row["sf"],row["payload_hex"]
    def remember(row):
        if row["phy_crc_ok"] is True:
            insort(fingerprints.setdefault(identity(row),[]),row["start_sample"])
    def add(row):
        bins.setdefault(key(row),[]).append(len(accepted))
        remember(row)
        accepted.append(row)
    for i,row in enumerate(accepted):
        bins.setdefault(key(row),[]).append(i)
        remember(row)
    repeated = 0
    for row in residual:
        if cancelled and cancelled():
            raise InterruptedError("Cancellation pass stopped")
        if row["phy_crc_ok"] is True:
            timings=fingerprints.get(identity(row),[])
            span=repeat_window(row,sample_rate_hz)
            at=row["start_sample"]
            j=bisect_left(timings,at-span)
            if j<len(timings) and timings[j]<=at+span:
                repeated+=1
                continue
        fc,sf,k=key(row)
        matches=sorted(i for b in (k-1,k,k+1) for i in bins.get((fc,sf,b),()) if same_detection(accepted[i],row))
        if not matches:
            add(row)
        elif row["phy_crc_ok"] is True:
            good = [i for i in matches if accepted[i]["phy_crc_ok"] is True]
            if not good:
                idx=matches[0]
                bins[key(accepted[idx])].remove(idx)
                accepted[idx]=row
                bins.setdefault(key(row),[]).append(idx)
                remember(row)
            else:
                add(row)
    return sorted(accepted, key=lambda r:r["start_sample"]), repeated
