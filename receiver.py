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


def interpolated_bin(power, k, oversample):
    left, mid = float(power[k-1]), float(power[k])
    right = float(power[(k+1) % len(power)])
    frac = (left-right) / (2*(left-2*mid+right) or 1)
    return (k + max(-.5, min(.5, frac))) / oversample


def peak(samples, reference, oversample=1):
    power = np.abs(fft.fft(samples*reference, n=len(reference)*oversample))**2
    k = int(power.argmax())
    quality = float(power[k]) / max(1e-15, float(power.mean()))
    if oversample == 1:
        return k, quality
    return interpolated_bin(power, k, oversample), quality


def candidate_peaks(power, minimum):
    """Up to three distinct peaks in a twice-padded power spectrum, in native bins."""
    k = int(power.argmax())
    if power[k] < minimum:
        return []
    work = power.copy()
    peaks = [interpolated_bin(power, k, 2)]
    for _ in range(2):
        # Suppress the main lobe, wrapping around DC without copying FFT bins.
        lo, hi = k-3, k+4
        work[max(0,lo):hi] = 0
        if lo < 0:
            work[lo:] = 0
        elif hi > len(work):
            work[:hi-len(work)] = 0
        k = int(work.argmax())
        if work[k] < minimum:
            break
        peaks.append(interpolated_bin(power, k, 2))
    return peaks


def preamble_levels(dechirped):
    """Channel RSSI in dBFS and input SNR in dB, measured over complete preamble chirps."""
    n = dechirped.shape[1]
    total = np.mean(np.abs(dechirped)**2, axis=1)
    coherent = np.abs(np.mean(dechirped, axis=1))**2
    # The fitted complex tone consumes one noise degree of freedom per chirp.
    # Residual energy includes interference and any unmodelled signal distortion.
    residual = float(np.median(np.maximum(total - coherent, 0.))) * n/(n-1)
    received = float(np.median(total))
    signal_power = float(np.median(coherent)) - residual/n
    rssi = float(10*np.log10(received)) if received > 0 else None
    snr = float(10*np.log10(signal_power/residual)) if min(signal_power, residual) > 0 else None
    return rssi, snr


def normalized_rail_indices(iq):
    """Recover the exact CU8 endpoints for callers supplying normalized IQ directly."""
    rail = 127.5/128
    return np.flatnonzero((abs(iq.real) == rail) | (abs(iq.imag) == rail))


class RailHistory:
    """Sparse original ADC flags on this segment's clock, shared by the carrier's branches."""
    def __init__(self, decimation, taps):
        self.d, self.taps = decimation, taps
        self.blocks = deque()

    def append(self, first, indices):
        if len(indices):
            self.blocks.append((first, indices))

    def discard_before(self, first):
        while self.blocks and self.blocks[0][0]+self.blocks[0][1][-1] < first:
            self.blocks.popleft()

    def count(self, start, stop):
        """Unique railed raw pairs in [start, stop), in segment-relative ADC samples."""
        return sum(int(idx.searchsorted(stop-at)-idx.searchsorted(start-at))
                   for at,idx in self.blocks if at+idx[0]<stop and at+idx[-1]>=start)

    def mask(self, start, stop, step=1):
        """FIR-contaminated outputs in [start, stop), in decimated samples."""
        end = np.arange(start,stop,step,dtype=np.int64)*self.d
        flags = np.zeros(len(end),dtype=bool)
        if len(end):
            for at,idx in self.blocks:
                if at+idx[0] <= end[-1] and at+idx[-1] >= end[0]-self.taps+1:
                    pos = end-at
                    flags |= idx.searchsorted(pos,side="right") != idx.searchsorted(pos-self.taps+1)
        return flags


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
    rssi_dbfs: float | None = None
    snr_db: float | None = None
    coarse_bins: float = 0.
    timing_check: str = "ok"
    alternate: "Burst | None" = None
    result: tuple | None = None
    verified_cfo_bins: float | None = None
    phase_fitted: bool = True


@dataclass
class TimingCandidate:
    sfd: int
    start: float
    coarse: float
    original: Burst
    agreed: bool = False
    phase_conflict: bool = False
    original_evidence: "TimingEvidence | None" = None


@dataclass(frozen=True)
class TimingEvidence:
    wrap: bool | None
    sync: tuple[float | None, float | None]
    down: tuple[float | None, float | None]

    @property
    def confirmed(self):
        if self.wrap is False:
            return False
        sync = [s for s in self.sync if s is not None]
        down = [s for s in self.down if s is not None]
        if self.wrap is True:
            return (not sync or max(sync)>0) and (not down or max(down)>0)
        return len(sync)==2 and min(sync)>0 and bool(down) and max(down)>0

    @property
    def score(self):
        def support(values):
            usable = [v for v in values if v is not None]
            if not usable:
                return 0.
            # A measured pair of chirps with no coherent projection opposes
            # the hypothesis. A pair lost to the ADC supplies no vote.
            return sum(usable)/len(usable) if any(usable) else -len(usable)/2
        wrap = 0. if self.wrap is None else (1. if self.wrap else -1.)
        return wrap + support(self.sync) + support(self.down)

    def distinguishes(self, other):
        """A missing observation at the frozen fit is not contrary evidence."""
        if other.wrap is False:
            return True
        for first,second in ((self.sync,other.sync),(self.down,other.down)):
            differences = [a-b for a,b in zip(first,second) if a is not None and b is not None]
            if differences and sum(differences)/len(differences)>=.1:
                return True
        return False


class Branch:
    # Rounded outwards from 4.21 native FFT bins and 8.68 half-chip samples
    # across the rail-free recorded and fixture population.
    cfo_tolerance_bins = 4.5
    timing_tolerance = 9.

    def __init__(self, profile, carrier, sf, rails=None):
        self.cfg, self.carrier, self.sf = profile, carrier, sf
        self.rails = rails
        self.n = 1 << sf
        self.length = 2*self.n
        self.up = upchirp(sf, 1)
        self.down = self.up.conj()
        self.codec = Codec(profile, carrier, sf)
        self.n_grid = 0
        self.recent = deque(maxlen=8)
        self.up_power = deque(maxlen=4)
        self.pending = []
        self.last_sfd = deque(maxlen=20)
        self.counts = Counter()
        self.chip_phase = np.arange(self.n, dtype=np.float64) * (-2j*np.pi/self.n)
        self.chip_angles = self.chip_phase.imag.copy()
        self.edge_at = np.arange(-20,20)
        chips = self.edge_at/2
        self.edge_ref = np.exp(-1j*np.pi*chips*(chips/self.n-1))
        self.edge_parity = np.where(self.edge_at%2,-1.,1.)

    def detect(self, buf, base):
        length = self.length
        end = base + len(buf)
        while (count := min(32, (end-self.n_grid)//length)) > 0:
            start = self.n_grid-base
            windows = buf[start:start+count*length].reshape(count, length)[:, ::2]
            powers = np.abs(fft.fft(windows*self.down, n=2*self.n, axis=1))**2
            powers /= np.maximum(powers.mean(axis=1, keepdims=True), 1e-15)
            sync_peaks = {}  # Several CFO hypotheses can test the very same sync-symbol window.
            for window, power in zip(windows, powers):
                if len(self.recent) >= 6:
                    single, shortlist = self.recent[-5]
                    previous = self.recent[-6][0]
                    paired = (single is not None and previous is not None
                              and abs(signed(single-previous, self.n)) <= 2)
                    if shortlist or paired:
                        down = np.abs(fft.fft(window*self.up, n=2*self.n))**2
                        down /= max(float(down.mean()), 1e-15)
                        if paired and down.max() >= 16:
                            self.try_sfd(buf, base, single, interpolated_bin(down, int(down.argmax()), 2), sync_peaks)
                        for kd in candidate_peaks(down, 10) if shortlist else ():
                            for ku in shortlist:
                                self.try_sfd(buf, base, ku, kd, sync_peaks)

                # Keep the full normalized spectrum of each chirp. A second packet
                # can be below the strongest peak in *every* individual window.
                self.up_power.append(power)
                candidates = []
                if len(self.up_power) == 4:
                    a, b, c, d = self.up_power
                    candidates = candidate_peaks(a+b+c+d, 4*9)
                k = int(power.argmax())
                single = interpolated_bin(power, k, 2) if power[k] >= 16 else None
                self.recent.append((single, candidates))
                self.n_grid += length

    def try_sfd(self, buf, base, ku, kd, sync_peaks):
        n, length = self.n, self.length
        fbin = signed(ku+kd, n)/2
        if abs(fbin*self.cfg.bandwidth_hz/n) > self.cfg.max_cfo_hz:
            return
        edge = self.n_grid - round(2*((ku-fbin) % n))
        for sfd in (edge, edge-length, edge+length):
            if (sfd-(self.cfg.preamble_symbols+2)*length < base-2 or
                    sfd+length > base+len(buf) or any(abs(sfd-x) < 6 for x in self.last_sfd)):
                continue
            burst = self.synchronize(buf, base, sfd, fbin, sync_peaks)
            if burst is None:
                continue
            # Keep the acquisition and duplicate gates on their original clock.
            # Refining a packet must not open or close another acquisition path.
            detected = burst.original.sfd if isinstance(burst,TimingCandidate) else burst.sfd
            if not any(abs(detected-x)<6 for x in self.last_sfd):
                self.last_sfd.append(detected)
                self.pending.append(burst)
                self.counts["syncs"] += 1
                return

    def synchronize(self, buf, base, sfd, coarse, sync_peaks=None):
        n, length, p = self.n, self.length, self.cfg
        if sync_peaks is None:
            sync_peaks = {}
        def sym(index, offset=0):
            i = sfd-base + index*length + offset
            return buf[i:i+length:2]

        for j, net in ((-2, 8*(p.sync_word >> 4)), (-1, 8*(p.sync_word & 15))):
            at = sfd+j*length
            result = sync_peaks.get(at)
            if result is None:
                result = sync_peaks[at] = peak(sym(j), self.down, 2)
            k, quality = result
            if quality < 12 or abs(signed(k-coarse-net, n)) > 2:
                return None
        up = coarse+signed(peak(sym(-3),self.down,4)[0]-coarse,n)
        down = coarse+signed(peak(sym(0),self.up,4)[0]-coarse,n)
        error, freq = up-down,(up+down)/2
        original = self.preamble_fit(buf,base,sfd,error,freq,coarse)
        consistent = abs(freq-coarse)<=self.cfo_tolerance_bins and abs(error)<=self.timing_tolerance
        if original is None:
            return None  # preserve the half-chip image rejection
        agreed = consistent and self.cfo_consistent(original)
        phase_conflict = False
        if agreed:
            pre_rails = sfd_rails = 0
            if self.rails is not None and self.rails.blocks:
                delay = (self.rails.taps-1)//2
                first = round(original.start*self.rails.d-delay)
                sync = first+p.preamble_symbols*length*self.rails.d
                pre_rails = self.rails.count(first,sync)
                sfd_rails = self.rails.count(sync,sync+4*length*self.rails.d)
            # An independently modulated, stronger SFD can yield mutually
            # consistent up/down estimates on the wrong chip. Require a chirp
            # boundary witness when the ADC clipped or SFD power rose by 1.5 dB.
            # On steady-power IQ a local edge fit can be ambiguous by one chip.
            first_down = sym(0)
            excess = (original.rssi_dbfs is not None and
                      np.vdot(first_down,first_down).real/n > 10**((original.rssi_dbfs+1.5)/10))
            if pre_rails:
                clean = self.clean_start_cfo(buf,base,original,freq)
                reported = self.start_cfo(original)
                # Only a fit from wholly clean chirps can contradict the
                # reported start CFO. A blanked fit remains available for a
                # payload retry even when the phase comparison is inconclusive.
                phase_conflict = (clean is not None and abs(clean-freq)<=.75
                                  and abs(clean-reported)>.75)
            elif not (sfd_rails or excess) or self.wrap_check(buf,base,original) is True:
                return original
        original.timing_check = "suspect"
        return TimingCandidate(sfd,sfd-(p.preamble_symbols+2)*length,coarse,original,agreed,phase_conflict)

    def start_cfo(self, burst):
        return burst.freq_bins-burst.drift_bins_per_symbol*(self.cfg.preamble_symbols+2)

    def cfo_consistent(self, burst):
        anchor = burst.coarse_bins if burst.verified_cfo_bins is None else burst.verified_cfo_bins
        return abs(self.start_cfo(burst)-anchor)<=self.cfo_tolerance_bins

    def wrap_check(self, buf, base, burst):
        """Independently locate the frequency reset between repeated preamble upchirps.

        At two samples/chip the two sides differ by an alternating sign after
        dechirping. A wrong but mutually consistent up/down pair can miss this
        boundary entirely. Noise-only local fits carry no timing evidence.
        """
        indices = np.arange(-min(self.cfg.preamble_symbols,7)-1,-2)
        starts = burst.sfd + indices*self.length
        if starts[0]+self.edge_at[0]<base or starts[-1]+self.edge_at[-1]>=base+len(buf):
            return None
        if self.rails is not None and self.rails.blocks:
            starts = [at for at in starts if not self.rails.mask(at+self.edge_at[0],at+self.edge_at[-1]+1).any()]
            if len(starts)<3:
                return None
            starts = np.asarray(starts)
        gates = buf[starts[:,None]-base+self.edge_at]
        z = gates*(self.edge_ref*np.exp(-1j*np.pi*self.edge_at*burst.data_bins/self.n))
        power = np.sum(abs(z)**2,axis=1)
        left = np.cumsum(z*self.edge_parity,axis=1)
        right = np.cumsum(z[:,::-1],axis=1)[:,::-1]
        size = len(self.edge_at)
        k = np.arange(1,size)
        scores = np.column_stack((abs(right[:,0])**2/size,
                    abs(left[:,:-1])**2/k+abs(right[:,1:])**2/(size-k), abs(left[:,-1])**2/size))
        score = np.median(scores/np.maximum(power[:,None],1e-20),axis=0)
        best = int(score.argmax())
        if score[best]<.22:
            return None
        expected = burst.start+(self.cfg.preamble_symbols+2)*self.length-burst.sfd
        rough = best+self.edge_at[0]
        if abs(rough)>12:
            return False
        # The phase jump supplies the fractional delay modulo one chip. The
        # change point resolves its integer ambiguity. Omit the FIR transition.
        a = left[:,best-4]
        b = right[:,best+3]
        cross = b*a.conj()
        phase = np.mean(cross/np.maximum(abs(cross),1e-20))
        if abs(phase)<.55:
            return None
        frac = np.angle(phase)/np.pi
        # Adjacent chip hypotheses share this phase. Compare their predicted
        # waveforms across the reset, including the sample at the transition.
        shifts = frac+2*np.arange(-7,8)
        splits = self.edge_at.searchsorted(shifts)
        projected = left[:,splits-1]*np.exp(1j*np.pi*frac)+right[:,splits]
        quality = np.median(abs(projected)**2/np.maximum(power[:,None]*size,1e-20),axis=0)
        index = int(quality.argmax())
        measured = shifts[index]
        nearest = int(abs(shifts-expected).argmin())
        # A neighbouring chip can win a noisy local fit by a small fraction
        # of its residual energy. Count independent chips across clean resets.
        advantage = (quality[index]-quality[nearest])*len(starts)*size/2
        if nearest!=index and advantage < 9*max(1-quality[index],1e-12):
            return None
        return bool(abs(measured-expected)<=1.)

    def masked_symbol(self, buf, base, start, odd=0):
        pos = start+odd
        samples = buf[pos-base:pos-base+self.length:2]
        if self.rails is None or not self.rails.blocks:
            return samples,self.n
        flags = self.rails.mask(pos,pos+self.length,2)
        good = self.n-int(np.count_nonzero(flags))
        return (np.where(flags,0,samples) if good<self.n else samples),good

    def clean_start_cfo(self, buf, base, burst, anchor):
        """Extrapolate only consecutive, wholly rail-free preamble phase pairs."""
        length,rail = self.length,self.rails
        reference = (self.down*np.exp(self.chip_phase*anchor)).astype(np.complex64)
        products,positions = [],[]
        previous = None
        for j in range(-min(8,self.cfg.preamble_symbols)-2,-2):
            at = burst.sfd+j*length
            if (at<base or at+length>base+len(buf) or
                    rail.count(at*rail.d-rail.taps+1,(at+length-1)*rail.d+1)):
                previous = None
                continue
            window = buf[at-base:at-base+length:2]
            phasor = np.dot(window,reference)
            if abs(phasor)**2 < 12*np.vdot(window,window).real:
                previous = None
                continue
            if previous is not None:
                products.append(phasor*previous.conjugate())
                positions.append(j)
            previous = phasor
        if len(products)<3:
            return None
        trend,freq = self.phase_fit(products,positions,anchor)
        return freq-trend*(self.cfg.preamble_symbols+2)

    def phase_fit(self, products, positions, anchor):
        phases = np.unwrap(np.angle(products))/(2*np.pi)
        phases += round(anchor-np.median(phases))
        trend,freq = np.polyfit(positions,phases,1)
        if abs(trend)>1200*(self.n/self.cfg.bandwidth_hz)**2:
            return 0.,float(np.median(phases))
        return trend,freq

    def timing_alternatives(self, buf, base, candidate):
        n,length,sfd,coarse = self.n,self.length,candidate.sfd,candidate.coarse
        up_samples,good = self.masked_symbol(buf,base,sfd-3*length)
        up,q = peak(up_samples,self.down,4)
        up = coarse+signed(up-coarse,n)
        if good>=n//4 and q>=12:
            for at in (sfd,sfd+length):
                if at+length>base+len(buf):
                    break
                samples,good = self.masked_symbol(buf,base,at)
                if good<n//4:
                    continue
                down,q = peak(samples,self.up,4)
                down = coarse+signed(down-coarse,n)
                error,freq = up-down,(up+down)/2
                if q<12:
                    continue
                b = self.preamble_fit(buf,base,sfd,error,freq,coarse,blank=True)
                if b:
                    b.timing_check = "refit"
                    yield b
        # Phase advance between upchirps supplies the fractional CFO around the
        # coarse bin. Their FFT tone differs from RF CFO by the timing offset.
        b = self.preamble_fit(buf,base,sfd,0.,coarse,coarse,blank=True)
        if b:
            error = 2*(b.data_bins-b.freq_bins)
            if abs(error)<=self.timing_tolerance:
                b = self.preamble_fit(buf,base,sfd,error,b.freq_bins,coarse,blank=True)
        if b is None:
            # All usable phase pairs may be gone. Still test the actual grid
            # hypothesis at its own time and frequency against the known chirps.
            b = Burst(sfd,candidate.start,coarse,coarse,0.,0.,candidate.original.power,
                      coarse_bins=coarse,rssi_dbfs=candidate.original.rssi_dbfs,
                      snr_db=candidate.original.snr_db,phase_fitted=False)
        b.timing_check = "fallback"
        yield b

    def timing_evidence(self, buf, base, burst):
        """Correlate each known chirp at its hypothesized fractional time and CFO."""
        n, length = self.n,self.length
        fractional_sfd = burst.start+(self.cfg.preamble_symbols+2)*length
        offset = (burst.sfd-fractional_sfd)/2
        def energy(j, direction, shift=0):
            at = burst.sfd+j*length
            if at<base or at+length>base+len(buf):
                return None
            k = np.arange(length)
            if self.rails is not None and self.rails.blocks:
                k = k[~self.rails.mask(at,at+length)]
            if len(k)<max(48,n//32):
                return None
            samples = buf[at-base+k]
            total = float(np.vdot(samples,samples).real)
            if total<=1e-20:
                return None
            pos = j+.5
            chip = (k*.5+shift+offset+burst.clock_bins_per_symbol*pos)%n
            rf = burst.freq_bins+burst.drift_bins_per_symbol*pos
            reference = np.exp(-1j*np.pi*(direction*chip*(chip/n-1)+k*rf/n))
            coherent = float(abs(np.dot(reference,samples))**2)/(len(k)*total)
            # Two samples/chip share channel noise. Require at least the
            # existing 12:1 tone-to-noise threshold in independent chips.
            return coherent if coherent*len(k)/2>=12 else 0.
        net = self.cfg.sync_word
        return TimingEvidence(self.wrap_check(buf,base,burst),
                              (energy(-2,1,8*(net>>4)),energy(-1,1,8*(net&15))),
                              (energy(0,-1),energy(1,-1)))

    def resolve_timing(self, buf, base, candidate):
        original = candidate.original
        fits = [original,*self.timing_alternatives(buf,base,candidate)]
        first = candidate.original_evidence or self.timing_evidence(buf,base,original)
        evidence = [first, *(self.timing_evidence(buf,base,b) for b in fits[1:])]
        def same(a,b):
            return abs(a.start-b.start)<=1 and abs(self.start_cfo(a)-self.start_cfo(b))<=.5
        confirmed = [i for i,e in enumerate(evidence) if e.confirmed]
        if candidate.phase_conflict:
            confirmed = [i for i in confirmed if i]
        winner = None
        if confirmed:
            best = max(confirmed,key=lambda i:evidence[i].score)
            if all(same(fits[best],fits[i]) for i in confirmed):
                # Independent measurements of the same solution aren't rivals.
                if 0 in confirmed:
                    best = 0
                rivals = [e.score for i,e in enumerate(evidence) if not same(fits[best],fits[i])]
                if not rivals or evidence[best].score-max(rivals)>=.1:
                    if best==0 or candidate.phase_conflict or evidence[best].distinguishes(evidence[0]):
                        winner = best
                    elif same(original,fits[best]):
                        winner = 0
        if (winner is None and candidate.agreed and not candidate.phase_conflict and
                not confirmed and evidence[0].wrap is not False):
            # Missing clean SFD or preamble evidence cannot invalidate a
            # mutually consistent original timing and frequency estimate.
            winner = 0
        chosen = fits[winner] if winner is not None else original
        if winner is not None:
            if winner==0:
                chosen.timing_check = "ok"
            if confirmed:
                chosen.verified_cfo_bins = self.start_cfo(chosen)
        else:
            chosen.timing_check = "suspect"
        if chosen is original:
            # An unfitted grid estimate is a comparison point. Without
            # independent confirmation it cannot open a new payload path.
            backups = [i for i in range(1,len(fits)) if fits[i].phase_fitted or i in confirmed]
            if backups:
                backup = max(backups,key=lambda i:evidence[i].score)
                chosen.alternate = fits[backup]
                if winner is None or backup not in confirmed or not same(chosen,chosen.alternate):
                    chosen.alternate.timing_check = "suspect"
        else:
            chosen.alternate = original
        return chosen

    def prepare_timing(self, buf, base, candidate, reject, final):
        # The original FFT peaks can move an SFD by up to N half-chips.
        # Allow the second full downchirp of every hypothesis to arrive.
        if not final and candidate.sfd+self.n+2*self.length>base+len(buf):
            return None
        original = candidate.original
        if candidate.agreed and not candidate.phase_conflict:
            if candidate.original_evidence is None:
                candidate.original_evidence = self.timing_evidence(buf,base,original)
            e = candidate.original_evidence
            # All four known chirps independently place at least one fifth of
            # their usable power in the predicted tone. With a consistent fit
            # and no adverse wrap, build masked retries only on payload failure.
            if e.wrap is not False and all(v is not None and v>=.2 for v in e.sync+e.down):
                result = self.read_burst(buf,base,original,reject)
                if result is None and not final:
                    return None
                if result and result[1] is True:
                    original.timing_check = "ok"
                    return original
        return self.resolve_timing(buf,base,candidate)

    def preamble_fit(self, buf, base, sfd, error, coarse, gate, blank=False):
        n,length,p = self.n,self.length,self.cfg
        corrected = sfd-round(error)
        fractional_sfd = sfd-error
        pre_indices = range(-min(p.preamble_symbols, 7)-2+1, -2)
        starts = [corrected+i*length for i in pre_indices]
        if starts[0]<base or starts[-1]+length>=base+len(buf):
            return None
        def windows(odd):
            if blank:
                rows,good = zip(*(self.masked_symbol(buf,base,at,odd) for at in starts))
                return np.stack(rows),np.asarray(good)>=n//4
            return np.stack([buf[at-base+odd:at-base+odd+length:2] for at in starts]),None
        pre,usable = windows(0)
        # Project onto this SF and frequency before taking phase differences.
        # The raw inter-symbol correlation also contains a coherent term from
        # an overlapping lower SF whose preamble repeats inside ours.
        reference = self.down * np.exp(self.chip_phase*coarse)
        phasors = pre @ reference.astype(np.complex64)
        # At one sample/chip, carriers exactly one BW apart alias to the same
        # dechirped tone. Half-chip samples retain the sign flip of that image.
        # Compare the two coherent preamble projections before accepting sync.
        odd,odd_usable = windows(1)
        odd_ref = upchirp(self.sf,2)[1::2].conj() * np.exp((self.chip_phase-1j*np.pi/n)*coarse)
        half_chip_phasors = odd @ odd_ref.astype(np.complex64)
        if blank:
            usable &= odd_usable
            if np.count_nonzero(usable[1:] & usable[:-1])<2:
                return None
            image_test = np.vdot(phasors[usable],half_chip_phasors[usable])
        else:
            image_test = np.vdot(phasors,half_chip_phasors)
        if image_test.real <= 0:
            return None
        products = phasors[1:] * phasors[:-1].conj()
        # Phase differences refer to the boundary between the two chirps.
        positions = np.arange(-2-len(products), -2, dtype=float)
        up_positions = np.arange(-2-len(pre), -2, dtype=float)+.5
        if blank:
            pairs = usable[1:] & usable[:-1]
            positions,products = positions[pairs],products[pairs]
            up_positions,pre,phasors = up_positions[usable],pre[usable],phasors[usable]
        trend,freq = self.phase_fit(products,positions,coarse)
        measured = [coarse+signed(peak(row,self.down,4)[0]-coarse,n) for row in pre]
        up_trend, data_bins = np.polyfit(up_positions, measured, 1)
        # Sample-clock error shifts the dechirped up-tone in addition to
        # carrier drift. Estimate symbol slips independently of RF Doppler.
        clock = float(np.clip(up_trend-trend, -100e-6*n, 100e-6*n))
        if abs(clock) < 3e-6*n:
            clock = 0.
        power = float(np.median(abs(phasors)/n)**2)
        # Remove each chirp's measured fractional offset before separating signal and noise.
        ref = self.down * np.exp(np.asarray(measured)[:, None] * self.chip_phase).astype(np.complex64)
        rssi, snr = preamble_levels(pre * ref)
        return Burst(corrected, fractional_sfd-(p.preamble_symbols+2)*length,
                     float(freq), float(data_bins), float(trend), clock, power,
                     rssi_dbfs=rssi, snr_db=snr, coarse_bins=gate)

    def symbol_spectra(self, buf, base, burst, count, trend=None, clock=None, blank_above=None):
        start = burst.sfd + self.length*9//4 - base
        slope = burst.drift_bins_per_symbol if trend is None else trend
        clock = burst.clock_bins_per_symbol if clock is None else clock
        corrected = np.empty((count,self.n),np.complex64)
        angles = np.empty_like(self.chip_angles)
        ref = np.empty_like(self.down)
        previous_freq = None
        for i in range(count):
            pos = i+2.75
            # Keep the sampling phase near the chip center across the whole
            # packet, including the residual half-chip quantization at SFD.
            shift = round(-2*(burst.data_bins-burst.freq_bins+clock*pos))
            at = min(start+i*self.length+shift, len(buf)-self.length)
            shift = at-start-i*self.length
            freq = burst.data_bins + (slope+clock)*pos + shift/2
            if freq != previous_freq:
                np.multiply(self.chip_angles, freq, out=angles)
                np.cos(angles, out=ref.real)
                np.sin(angles, out=ref.imag)
                np.multiply(self.down, ref, out=ref)
                previous_freq = freq
            corrected[i] = buf[at:at+self.length:2] * ref
        if blank_above is not None:
            # Blank only the private demodulation windows for this CRC retry.
            corrected[np.abs(corrected) > blank_above] = 0.
        return fft.fft(corrected, axis=1)

    def symbols(self, buf, base, burst, count, trend=None, track=False, clock=None, blank_above=None):
        spectra = self.symbol_spectra(buf, base, burst, count, trend, clock, blank_above)
        return self.spectral_symbols(spectra, track)

    def spectral_symbols(self, spectra, track):
        bins = np.abs(spectra).argmax(axis=1)
        if track:
            # Fractional-bin tracking only on CRC retry. Header/LDRO symbols
            # lie on a four-bin grid; payload symbols otherwise use one bin.
            idx = np.arange(len(bins))
            mid = spectra[idx,bins]
            left, right = spectra[idx,(bins-1)%self.n], spectra[idx,(bins+1)%self.n]
            frac = np.clip(((left-right)/(2*mid-left-right+1e-15)).real,-.5,.5)
            bias = 0.
            for i in range(len(bins)):
                spacing = 4 if i < 8 or self.cfg.de(self.sf) else 1
                error = signed(bins[i]+float(frac[i])-1-bias,spacing)
                bias += .38*error
                bins[i] = round(bins[i]+float(frac[i])-bias) % self.n
        return bins.astype(np.uint16)

    def payload_bin_slope(self, buf, base, burst, symbols):
        """Estimate the change in dechirped frequency (FFT bins per symbol)."""
        length = self.length
        start = burst.sfd - base + length*9//4
        gates = buf[start:start+len(symbols)*length].reshape(-1, length)
        down = np.tile(upchirp(self.sf, 2).conj(), 2)
        lag = length//4
        phases = np.zeros(len(symbols), dtype=np.complex128)
        for i, (gate, symbol) in enumerate(zip(gates, symbols)):
            shift = 2*int(symbol)
            tone = gate * down[shift:shift+length]
            # A cyclic LoRa chirp folds at this point. Correlate only samples
            # on the same side so the fold's unknown phase jump cancels.
            split = length-shift
            corr = 0j
            for part in (tone[:split], tone[split:]):
                if len(part) > lag:
                    corr += np.vdot(part[:-lag], part[lag:])
            if corr:
                # A quarter-symbol lag measures a quarter turn per bin.
                # Four times that phase removes integer symbol-bin ambiguity.
                phases[i] = (corr/abs(corr))**4
        slope = float(np.angle(np.vdot(phases[:-1], phases[1:]))/(2*np.pi))
        coherence = abs(np.mean(phases*np.exp(-2j*np.pi*slope*np.arange(len(phases)))))
        return slope if coherence >= .5 else None

    def retry_corrections(self, buf, base, burst, symbols):
        drift, clock = burst.drift_bins_per_symbol, burst.clock_bins_per_symbol
        # A short preamble can overestimate either correction. Try independent
        # drift and clock corrections, then a smaller clock estimate.
        candidates = [(drift, clock, True), (0., 0., True), (drift, 0., True)]
        if clock:
            candidates.extend(((0., clock, True), (0., clock/2, False), (drift, clock/2, False)))
        for rf_drift, slip, track in dict.fromkeys(candidates):
            yield rf_drift, slip, track, None

        # Only measure the payload if the cheaper retries all failed. Its
        # upchirps observe the sum of RF drift and sample-clock slip.
        slope = self.payload_bin_slope(buf, base, burst, symbols)
        if slope is not None:
            for rf_drift in dict.fromkeys((0., drift)):
                slip = slope-rf_drift
                if abs(slip) <= 100e-6*self.n:
                    yield rf_drift, slip, False, None

        # Short, strong bursts can dominate the symbol FFT for a few symbols.
        # Measure their occupancy before spending one extra decode on blanking.
        start = burst.sfd - base + self.length*9//4
        gates = buf[start:start+len(symbols)*self.length].reshape(len(symbols), self.length)
        levels = np.abs(gates[:, ::8])
        limit = 4 * np.median(levels)  # 12 dB above the packet's median amplitude
        outliers = levels > limit
        if limit > 0 and outliers.mean() <= 1/8 and outliers.mean(axis=1).max() >= 1/16:
            yield drift, clock, False, limit

    def read_burst(self, buf, base, burst, reject, primary=True):
        # None means more IQ is needed, () means rejected, otherwise (payload, CRC).
        if burst.result is not None:
            return burst.result
        data = burst.sfd + self.length*9//4
        end = base+len(buf)
        if burst.header is None:
            if data+8*self.length>end:
                return None
            burst.header = self.codec.header(self.symbols(buf,base,burst,8))
            if burst.header is None:
                burst.result = ()
                if primary:
                    self.counts["header_rejects"] += 1
                    if reject:
                        reject("header_reject",self.sf,data+8*self.length)
                return burst.result
            if primary:
                self.counts["headers"] += 1
        if data+burst.header[3]*self.length>end:
            return None
        count = burst.header[3]
        spectra = self.symbol_spectra(buf, base, burst, count)
        magnitudes = np.abs(spectra)
        symbols = magnitudes.argmax(axis=1).astype(np.uint16)
        if self.codec.header(symbols[:8]) != burst.header:
            burst.result = ()
            if primary:
                self.counts["unstable_headers"] += 1
                if reject:
                    reject("unstable_header",self.sf,data+count*self.length)
            return burst.result
        payload, crc = self.codec.payload(symbols, burst.header)
        if crc is False:
            for slope,clock,track,limit in self.retry_corrections(buf,base,burst,symbols):
                if (slope, clock, limit) == (burst.drift_bins_per_symbol, burst.clock_bins_per_symbol, None):
                    retry = self.spectral_symbols(spectra, track)
                else:
                    retry = self.symbols(buf,base,burst,count,slope,track=track,clock=clock,blank_above=limit)
                if self.codec.header(retry[:8]) == burst.header:
                    alt, ok = self.codec.payload(retry, burst.header)
                    if ok:
                        payload, crc = alt, ok
                        burst.drift_bins_per_symbol = slope
                        burst.clock_bins_per_symbol = clock
                        break
            else:
                # The header already passed. Retry once with symbol confidence
                # from the original IQ after the timing and interference retries.
                alt = self.codec.soft_payload(symbols, magnitudes, burst.header)
                if alt is not None:
                    payload, crc = alt, True
                    self.counts["soft_fec_recoveries"] += 1
        burst.result = (payload,crc)
        return burst.result

    def decode(self, buf, base, emit, reject=None, final=False):
        pending = []
        for burst in self.pending:
            if isinstance(burst,TimingCandidate):
                fitted = self.prepare_timing(buf,base,burst,reject,final)
                if fitted is None:
                    pending.append(burst)
                    continue
                burst = fitted
            result = self.read_burst(buf,base,burst,reject)
            if result is None and not final:
                pending.append(burst)
                continue
            if burst.alternate is not None and (not result or result[1] is not True):
                alt = self.read_burst(buf,base,burst.alternate,None,primary=False)
                if alt is None and not final:
                    pending.append(burst)
                    continue
                if alt and (alt[1] is True or not result):
                    burst,result = burst.alternate,alt
            if result:
                self.counts["packets"] += 1
                emit(self,burst,*result)
            elif result is None:
                pending.append(burst)
        self.pending = pending

    def keep_from(self):
        history = self.n_grid - (self.cfg.preamble_symbols+6)*self.length
        for b in self.pending:
            if isinstance(b,TimingCandidate):
                history=min(history,int(b.start)-self.length)
            else:
                history=min(history,b.sfd,b.alternate.sfd if b.alternate else b.sfd)
        return history


class Channel:
    def __init__(self, profile, carrier, origin, emit, on_reject=None):
        self.cfg, self.fc, self.origin, self.emit = profile, carrier, origin, emit
        self.on_reject = on_reject
        self.d = profile.sample_rate_hz // (2*profile.bandwidth_hz)
        self.taps = signal.firwin(20*self.d+1, .74*profile.bandwidth_hz,
                                  fs=profile.sample_rate_hz).astype(np.float32)
        self.state = np.zeros(len(self.taps)-1, dtype=np.complex64)
        self.base = self.raw_pos = 0
        self.buffer = self.remainder = np.empty(0, dtype=np.complex64)
        self.osc = np.empty(0, dtype=np.complex64)
        self.rails = RailHistory(self.d,len(self.taps))
        self.branches = [Branch(profile, carrier, sf, self.rails) for sf in profile.spreading_factors]
        self.preamble_history = (profile.preamble_symbols+2)*max(b.length for b in self.branches)

    def feed(self, raw, rail_at=None):
        # Indices refer to the new input, before mixing, remainder handling or SIC.
        self.rails.append(self.raw_pos+len(self.remainder),
                          normalized_rail_indices(raw) if rail_at is None else rail_at)
        if len(self.remainder):
            raw = np.concatenate((self.remainder, raw))
        count = len(raw)//self.d*self.d
        self.remainder = raw[count:].copy()
        if not count:
            return
        fs = self.cfg.sample_rate_hz
        delta = self.fc - self.cfg.center_hz
        mixed = raw[:count]
        if delta:
            if len(self.osc) != count:
                self.osc = np.exp((-2j*np.pi*delta/fs)*np.arange(count)).astype(np.complex64)
            phase = np.complex64(np.exp(-2j*np.pi*((delta*self.raw_pos) % fs)/fs))
            mixed = mixed * self.osc
            mixed *= phase
        self.raw_pos += count
        x = np.concatenate((self.state, mixed))
        self.state = x[-len(self.state):].copy()
        # Only the valid decimated outputs are consumed. Each strided row
        # is a view of the same FIR history, without allocating an IQ matrix.
        windows = np.lib.stride_tricks.sliding_window_view(x, len(self.taps))[::self.d]
        y = np.einsum("ij,j->i", windows, self.taps[::-1])
        if self.cfg.invert_iq:
            y = y.conj()
        self.buffer = np.concatenate((self.buffer, y))
        for branch in self.branches:
            branch.detect(self.buffer, self.base)
            branch.decode(self.buffer, self.base, self.packet, self.rejection if self.on_reject else None)
        drop = max(0, min(b.keep_from() for b in self.branches)-self.base)
        if drop:
            self.buffer = self.buffer[drop:]
            self.base += drop
            self.rails.discard_before((self.base-self.preamble_history)*self.d-len(self.taps)+1)

    def rejection(self, reason, sf, boundary):
        sample = self.origin + boundary*self.d - (len(self.taps)-1)//2
        self.on_reject(reason, sample, carrier_hz=self.fc, sf=sf)

    def packet(self, branch, burst, payload, crc):
        p = self.cfg
        position = self.origin + burst.start*self.d - (len(self.taps)-1)/2
        start = max(self.origin, round(position))
        bin_hz = p.bandwidth_hz / branch.n
        slope = burst.drift_bins_per_symbol * bin_hz**2
        t_to_sfd = (p.preamble_symbols+2)/bin_hz
        sign = -1 if p.invert_iq else 1
        sync_start = start-self.origin
        sync_end = sync_start+round((p.preamble_symbols+4.25)*branch.length*self.d)
        timing = burst.timing_check if branch.cfo_consistent(burst) else "suspect"
        self.emit(dict(start_sample=start, capture_s=start/p.sample_rate_hz, carrier_hz=self.fc,
                       sf=branch.sf, bandwidth_hz=p.bandwidth_hz, sync_word=f"0x{p.sync_word:02X}",
                       explicit_header=p.explicit_header, cr_denominator=burst.header[1]+4,
                       ldro=p.de(branch.sf), preamble_symbols=p.preamble_symbols,
                       phy_crc_ok=crc, payload_hex=payload.hex(),
                       cfo_hz=sign*(burst.freq_bins*bin_hz - slope*t_to_sfd),
                       drift_hz_s=sign*slope, clock_error_ppm=-burst.clock_bins_per_symbol/branch.n*1e6,
                       preamble_power_dbfs=10*np.log10(max(burst.power, 1e-15)),
                       rssi_dbfs=burst.rssi_dbfs, snr_db=burst.snr_db,
                       timing_check=timing, sync_rail_samples=self.rails.count(sync_start,sync_end),
                       coarse_cfo_hz=sign*burst.coarse_bins*bin_hz))


class Receiver:
    def __init__(self, profile, emit, on_reject=None):
        self.profile, self.emit = profile, emit
        self.on_reject = on_reject
        self.channels = []
        self.counts = Counter()
        self.next_sample = None

    def feed(self, start, iq, rail_at=None):
        if self.next_sample != start:
            self.close_segment()
            self.channels = [Channel(self.profile, fc, start, self.emit, self.on_reject) for fc in self.profile.carriers_hz]
        if rail_at is None:
            rail_at = normalized_rail_indices(iq)
        for channel in self.channels:
            channel.feed(iq,rail_at)
        self.next_sample = start + len(iq)

    def settled_before(self):
        """No pending or future detection can have a preamble earlier than this sample."""
        limit=self.next_sample or 0
        for c in self.channels:
            delay=(len(c.taps)-1)/2
            for b in c.branches:
                # Include not-yet-recognized preambles in the detector's search history.
                edge=b.n_grid-(b.cfg.preamble_symbols+6)*b.length
                earliest=min([edge]+[burst.start for burst in b.pending])
                for burst in b.pending:
                    if isinstance(burst,TimingCandidate):
                        # A deferred comparison still owns the entire possible
                        # refinement interval, including while its payload arrives.
                        earliest=min(earliest,burst.start-b.n)
                    elif burst.alternate is not None:
                        earliest=min(earliest,burst.alternate.start)
                limit=min(limit,max(c.origin,int(c.origin+earliest*c.d-delay)-2))
        return limit

    def close_segment(self):
        for channel in self.channels:
            # Emit the FIR's group delay at an actual boundary. No LoRa
            # symbol is synthesized across a gap or read from its far side.
            channel.feed(np.zeros((len(channel.taps)-1)//2, dtype=np.complex64))
            for branch in channel.branches:
                if any(isinstance(b,TimingCandidate) or b.alternate is not None for b in branch.pending):
                    branch.decode(channel.buffer,channel.base,channel.packet,channel.rejection if channel.on_reject else None,
                                  final=True)
                for key, value in branch.counts.items():
                    self.counts[f"{channel.fc}/SF{branch.sf}/{key}"] += value
                if branch.pending:
                    self.counts[f"{channel.fc}/SF{branch.sf}/truncated"] += len(branch.pending)
        self.channels = []
        self.next_sample = None
