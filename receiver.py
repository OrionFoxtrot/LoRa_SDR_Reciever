"""Continuous channelization, blind preamble/SFD search and burst decoding.

The stream coordinates in Branch are at 2*BW. Channel translates them back to
the first captured I/Q pair, including the linear-phase channel filter delay.
"""
from collections import deque, Counter
from dataclasses import dataclass
import numpy as np
from scipy import fft, signal
from phy import Codec, upchirp


def signed(value, period):
    return (value + period/2) % period - period/2


def peak(samples, reference, oversample=1):
    power = np.abs(fft.fft(samples*reference, n=len(reference)*oversample))**2
    k = int(power.argmax())
    quality = float(power[k]) / max(1e-15, float(power.mean()))
    if oversample == 1:
        return k, quality
    left, mid, right = (float(power[(k+j) % len(power)]) for j in (-1, 0, 1))
    frac = np.clip((left-right) / (2*(left-2*mid+right) or 1), -.5, .5)
    return (k+frac)/oversample, quality


@dataclass
class Burst:
    sfd: int
    start: float
    freq_bins: float
    data_bins: float
    drift_bins_per_symbol: float
    clock_bins_per_symbol: float
    power: float
    header: tuple | None = None


class Branch:
    def __init__(self, profile, carrier, sf):
        self.cfg, self.carrier, self.sf = profile, carrier, sf
        self.n = 1 << sf
        self.length = 2*self.n
        self.up = upchirp(sf, 1)
        self.down = self.up.conj()
        self.codec = Codec(profile, carrier, sf)
        self.n_grid = 0
        self.recent = deque(maxlen=8)
        self.pending = []
        self.last_sfd = deque(maxlen=20)
        self.counts = Counter()
        self.chip_phase = np.arange(self.n, dtype=np.float64) * (-2j*np.pi/self.n)

    def detect(self, buf, base):
        length, n = self.length, self.n
        end = base + len(buf)
        while self.n_grid + length <= end:
            g = self.n_grid
            self.n_grid += length
            window = buf[g-base:g-base+length:2]
            ku_now, q = peak(window, self.down)
            # Five and six symbols before a complete downchirp grid are preamble.
            if len(self.recent) >= 6:
                ku, prev = self.recent[-5], self.recent[-6]
                if ku is not None and prev is not None and abs(signed(ku-prev, n)) <= 2:
                    kd, dq = peak(window, self.up)
                    fbin = signed(ku+kd, n)/2
                    if dq >= 16 and abs(fbin*self.cfg.bandwidth_hz/n) <= self.cfg.max_cfo_hz:
                        edge = g - round(2*((ku-fbin) % n))
                        for sfd in (edge, edge-length, edge+length):
                            if (sfd-(self.cfg.preamble_symbols+2)*length < base-2 or
                                    sfd+length > end or
                                    any(abs(sfd-x) < 6 for x in self.last_sfd)):
                                continue
                            burst = self.synchronize(buf, base, sfd, fbin)
                            if burst is not None:
                                self.last_sfd.append(burst.sfd)
                                self.pending.append(burst)
                                self.counts["syncs"] += 1
                                break
            self.recent.append(ku_now if q >= 16 else None)

    def synchronize(self, buf, base, sfd, coarse):
        n, length, p = self.n, self.length, self.cfg
        def sym(index, offset=0):
            i = sfd-base + index*length + offset
            return buf[i:i+length:2]

        for j, net in ((-2, 8*(p.sync_word >> 4)), (-1, 8*(p.sync_word & 15))):
            k, quality = peak(sym(j), self.down)
            if quality < 12 or abs(signed(k-coarse-net, n)) > 2:
                return None
        up_bin, _ = peak(sym(-3), self.down, 4)
        dn_bin, _ = peak(sym(0), self.up, 4)
        up_bin = coarse + signed(up_bin-coarse, n)
        dn_bin = coarse + signed(dn_bin-coarse, n)
        # Opposite slopes separate timing from frequency. Refine to a half chip
        # for slicing and retain the fractional edge for RF timestamps and SIC.
        error = up_bin - dn_bin
        corrected = sfd - round(error)
        fractional_sfd = sfd - error
        coarse = (up_bin+dn_bin)/2
        pre_indices = range(-min(p.preamble_symbols, 7)-2+1, -2)
        pre = np.stack([sym(i, corrected-sfd) for i in pre_indices])
        # Project onto this SF and frequency before taking phase differences.
        # The raw inter-symbol correlation also contains a coherent term from
        # an overlapping lower SF whose preamble repeats inside ours.
        reference = self.down * np.exp(self.chip_phase*coarse)
        phasors = pre @ reference.astype(np.complex64)
        # At one sample/chip, carriers exactly one BW apart alias to the same
        # dechirped tone. Half-chip samples retain the sign flip of that image.
        # Compare the two coherent preamble projections before accepting sync.
        odd = np.stack([sym(i, corrected-sfd+1) for i in pre_indices])
        odd_ref = upchirp(self.sf,2)[1::2].conj() * np.exp((self.chip_phase-1j*np.pi/n)*coarse)
        half_chip_phasors = odd @ odd_ref.astype(np.complex64)
        if np.vdot(phasors,half_chip_phasors).real <= 0:
            return None
        products = phasors[1:] * phasors[:-1].conj()
        phases = np.unwrap(np.angle(products)) / (2*np.pi)
        phases += round(coarse - np.median(phases))
        # Phase differences refer to the boundary between the two chirps.
        positions = np.arange(-2-len(phases), -2, dtype=float)
        trend, freq = np.polyfit(positions, phases, 1)
        max_trend = 1200*(n/p.bandwidth_hz)**2
        if abs(trend) > max_trend:
            trend, freq = 0., float(np.median(phases))
        up_positions = np.arange(-2-len(pre), -2, dtype=float)+.5
        measured = [coarse+signed(peak(row,self.down,4)[0]-coarse,n) for row in pre]
        up_trend, data_bins = np.polyfit(up_positions, measured, 1)
        # Sample-clock error shifts the dechirped up-tone in addition to
        # carrier drift. Estimate symbol slips independently of RF Doppler.
        clock = float(np.clip(up_trend-trend, -100e-6*n, 100e-6*n))
        if abs(clock) < 3e-6*n:
            clock = 0.
        power = float(np.median(abs(phasors)/n)**2)
        return Burst(corrected, fractional_sfd-(p.preamble_symbols+2)*length,
                     float(freq), float(data_bins), float(trend), clock, power)

    def symbols(self, buf, base, burst, count, trend=None, track=False, clock=None):
        start = burst.sfd + self.length*9//4 - base
        slope = burst.drift_bins_per_symbol if trend is None else trend
        clock = burst.clock_bins_per_symbol if clock is None else clock
        corrected = np.empty((count,self.n),np.complex64)
        for i in range(count):
            pos = i+2.75
            # Keep the sampling phase near the chip center across the whole
            # packet, including the residual half-chip quantization at SFD.
            shift = round(-2*(burst.data_bins-burst.freq_bins+clock*pos))
            at = min(start+i*self.length+shift, len(buf)-self.length)
            shift = at-start-i*self.length
            freq = burst.data_bins + (slope+clock)*pos + shift/2
            ref = self.down * np.exp(self.chip_phase*freq).astype(np.complex64)
            corrected[i] = buf[at:at+self.length:2] * ref
        spectra = fft.fft(corrected, axis=1)
        bins = np.abs(spectra).argmax(axis=1)
        if track:
            # Fractional-bin tracking only on CRC retry. Header/LDRO symbols
            # lie on a four-bin grid; payload symbols otherwise use one bin.
            idx = np.arange(count)
            mid = spectra[idx,bins]
            left, right = spectra[idx,(bins-1)%self.n], spectra[idx,(bins+1)%self.n]
            frac = np.clip(((left-right)/(2*mid-left-right+1e-15)).real,-.5,.5)
            bias = 0.
            for i in range(count):
                spacing = 4 if i < 8 or self.cfg.de(self.sf) else 1
                error = signed(bins[i]+float(frac[i])-1-bias,spacing)
                bias += .38*error
                bins[i] = round(bins[i]+float(frac[i])-bias) % self.n
        return bins.astype(np.uint16)

    def decode(self, buf, base, emit):
        end = base + len(buf)
        pending = []
        for burst in self.pending:
            data = burst.sfd + self.length*9//4
            if burst.header is None and data + 8*self.length <= end:
                burst.header = self.codec.header(self.symbols(buf, base, burst, 8))
                if burst.header is None:
                    self.counts["header_rejects"] += 1
                    continue
                self.counts["headers"] += 1
            if burst.header is None or data + burst.header[3]*self.length > end:
                pending.append(burst)
                continue
            length, cr, has_crc, count = burst.header
            symbols = self.symbols(buf, base, burst, count)
            if self.codec.header(symbols[:8]) != burst.header:
                self.counts["unstable_headers"] += 1
                continue
            payload, crc = self.codec.payload(symbols, burst.header)
            # Short noisy preambles can bias a long extrapolation. Accept a
            # constant-CFO retry only if the on-air header agrees and CRC passes.
            if crc is False:
                hypotheses = ((burst.drift_bins_per_symbol, burst.clock_bins_per_symbol),
                              (0., 0.), (burst.drift_bins_per_symbol, 0.))
                for slope,clock in dict.fromkeys(hypotheses):
                    retry = self.symbols(buf,base,burst,count,slope,track=True,clock=clock)
                    if self.codec.header(retry[:8]) == burst.header:
                        alt, ok = self.codec.payload(retry, burst.header)
                        if ok:
                            payload, crc = alt, ok
                            burst.drift_bins_per_symbol = slope
                            burst.clock_bins_per_symbol = clock
                            break
            self.counts["packets"] += 1
            emit(self, burst, payload, crc)
        self.pending = pending

    def keep_from(self):
        history = self.n_grid - (self.cfg.preamble_symbols+6)*self.length
        return min([history] + [b.sfd for b in self.pending])


class Channel:
    def __init__(self, profile, carrier, origin, emit):
        self.cfg, self.fc, self.origin, self.emit = profile, carrier, origin, emit
        self.d = profile.sample_rate_hz // (2*profile.bandwidth_hz)
        self.taps = signal.firwin(20*self.d+1, .74*profile.bandwidth_hz,
                                  fs=profile.sample_rate_hz).astype(np.float32)
        self.state = np.zeros(len(self.taps)-1, dtype=np.complex64)
        self.base = self.raw_pos = 0
        self.buffer = self.remainder = np.empty(0, dtype=np.complex64)
        self.osc = np.empty(0, dtype=np.complex64)
        self.branches = [Branch(profile, carrier, sf) for sf in profile.spreading_factors]

    def feed(self, raw):
        if len(self.remainder):
            raw = np.concatenate((self.remainder, raw))
        count = len(raw)//self.d*self.d
        self.remainder = raw[count:].copy()
        if not count:
            return
        fs = self.cfg.sample_rate_hz
        delta = self.fc - self.cfg.center_hz
        if len(self.osc) != count:
            self.osc = np.exp((-2j*np.pi*delta/fs)*np.arange(count)).astype(np.complex64)
        phase = np.complex64(np.exp(-2j*np.pi*((delta*self.raw_pos) % fs)/fs))
        mixed = raw[:count] * self.osc * phase
        self.raw_pos += count
        x = np.concatenate((self.state, mixed))
        self.state = x[-len(self.state):].copy()
        y = signal.upfirdn(self.taps, x, down=self.d)[20:20+count//self.d]
        if self.cfg.invert_iq:
            y = y.conj()
        self.buffer = np.concatenate((self.buffer, y))
        for branch in self.branches:
            branch.detect(self.buffer, self.base)
            branch.decode(self.buffer, self.base, self.packet)
        drop = max(0, min(b.keep_from() for b in self.branches)-self.base)
        if drop:
            self.buffer = self.buffer[drop:]
            self.base += drop

    def packet(self, branch, burst, payload, crc):
        p = self.cfg
        position = self.origin + burst.start*self.d - (len(self.taps)-1)/2
        start = max(self.origin, round(position))
        bin_hz = p.bandwidth_hz / branch.n
        slope = burst.drift_bins_per_symbol * bin_hz**2
        t_to_sfd = (p.preamble_symbols+2)/bin_hz
        sign = -1 if p.invert_iq else 1
        self.emit(dict(start_sample=start, capture_s=start/p.sample_rate_hz, carrier_hz=self.fc,
                       sf=branch.sf, bandwidth_hz=p.bandwidth_hz, sync_word=f"0x{p.sync_word:02X}",
                       explicit_header=p.explicit_header, cr_denominator=burst.header[1]+4,
                       ldro=p.de(branch.sf), preamble_symbols=p.preamble_symbols,
                       phy_crc_ok=crc, payload_hex=payload.hex(),
                       cfo_hz=sign*(burst.freq_bins*bin_hz - slope*t_to_sfd),
                       drift_hz_s=sign*slope, clock_error_ppm=-burst.clock_bins_per_symbol/branch.n*1e6,
                       preamble_power_dbfs=10*np.log10(max(burst.power, 1e-15))))


class Receiver:
    def __init__(self, profile, emit):
        self.profile, self.emit = profile, emit
        self.channels = []
        self.counts = Counter()
        self.next_sample = None

    def feed(self, start, iq):
        if self.next_sample != start:
            self.close_segment()
            self.channels = [Channel(self.profile, fc, start, self.emit) for fc in self.profile.carriers_hz]
        for channel in self.channels:
            channel.feed(iq)
        self.next_sample = start + len(iq)

    def close_segment(self):
        for channel in self.channels:
            # Emit the FIR's group delay at an actual boundary. No LoRa
            # symbol is synthesized across a gap or read from its far side.
            channel.feed(np.zeros((len(channel.taps)-1)//2, dtype=np.complex64))
            for branch in channel.branches:
                for key, value in branch.counts.items():
                    self.counts[f"{channel.fc}/SF{branch.sf}/{key}"] += value
                if branch.pending:
                    self.counts[f"{channel.fc}/SF{branch.sf}/truncated"] += len(branch.pending)
        self.channels = []
        self.next_sample = None
