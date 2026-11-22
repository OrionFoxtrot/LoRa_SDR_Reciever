"""Capture-clock RF measurements and bounded event IQ captures, shared by live and replay."""
from bisect import bisect_left, bisect_right, insort
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

import numpy as np
from scipy import fft

from config import EventIQLimits
from iq_recorder import MIN_FREE_BYTES
from sources import BLOCK_SAMPLES


class FrequencyOffsets:
    """PHY-CRC-confirmed packet offsets over ten minutes of the capture clock."""
    window_s = 600

    def __init__(self, profile, warn=None):
        self.width = profile.max_cfo_hz
        self.warn = warn
        self.ppm = None
        self.recent = {fc: [] for fc in profile.carriers_hz}
        self.stats = {}
        self.dirty = set(self.recent)
        self.now = 0.

    def add(self, row):
        if row["phy_crc_ok"] is True and row.get("timing_check")!="suspect" and np.isfinite(row["cfo_hz"]):
            t, fc = row["capture_s"], row["carrier_hz"]
            if t >= self.now-self.window_s:
                insort(self.recent[fc], (t, row["cfo_hz"]))
                self.dirty.add(fc)

    def clear_packets(self):
        """Recount after SIC merges the journal; retain the clock and the last alerts."""
        for values in self.recent.values():
            values.clear()
        self.dirty.update(self.recent)

    def snapshot(self, capture_s):
        self.now = max(self.now,capture_s)
        for fc, values in self.recent.items():
            remove = bisect_left(values,(self.now-self.window_s,))
            if remove:
                del values[:remove]
                self.dirty.add(fc)
            if fc not in self.dirty:
                continue
            previous = self.stats.get(str(fc),{})
            median = low = high = tail = delta = suggested = None
            alerts = []
            if values:
                hz = np.array([offset for _,offset in values])
                low, median, high = map(float,np.percentile(hz,[5,50,95]))
                tail = float(np.percentile(abs(hz),95))
                if abs(median) > 10000:
                    alerts.append("median")
                if tail >= self.width-4000:
                    alerts.append("search_edge")
                if alerts:
                    delta = -median*1e6/fc
                    suggested = self.ppm+delta if self.ppm is not None else None
                    if self.warn and alerts != previous.get("warnings"):
                        self.warn(f"{fc/1e6:g} MHz CFO margin: median {median/1000:+.1f} kHz, "
                                  f"95th |CFO| {tail/1000:.1f} kHz, search ±{self.width/1000:g} kHz. "
                                  f"Suggested tuner adjustment {delta:+.1f} ppm. Verify with a reference "
                                  "transmitter before changing ppm.")
            self.stats[str(fc)] = dict(count=len(values),median_hz=median,p05_hz=low,p95_hz=high,
                                       p95_abs_hz=tail,warnings=alerts,suggested_delta_ppm=delta,
                                       suggested_ppm=suggested)
        self.dirty.clear()
        return dict(window_s=self.window_s,as_of_capture_s=self.now,max_cfo_hz=self.width,
                    applied_ppm=self.ppm,carriers=dict(self.stats))


class SpectrumMonitor:
    """Half-second capture-clock spectra with a separate broadband noise alarm."""
    bins = 256
    frames_per_row = 32
    floor_rows = 20       # 10 s at 2 rows/s
    fast_rows = 3         # 1.5 s of recent measurements for prompt step detection
    minimum_clean_frames = 8
    minimum_reference_rows = 4
    reference_s = 30
    rise_db, clear_db = 5., 4.

    def __init__(self, folder, profile, warn=None, on_rise=None):
        self.p = profile
        self.warn = warn
        self.on_rise = on_rise
        self.stride = profile.sample_rate_hz // 64
        self.df = profile.sample_rate_hz/self.bins
        offsets = fft.fftshift(fft.fftfreq(self.bins, 1/profile.sample_rate_hz))
        self.masks = {fc: (abs(offsets + profile.center_hz - fc) <= profile.bandwidth_hz/2)
                      & (abs(offsets) > self.df) for fc in profile.carriers_hz}
        for fc, mask in self.masks.items():
            if not mask.any():
                mask[np.argmin(abs(offsets+profile.center_hz-fc))] = True
        # Include the entire FFT cell in the usable band, including its half-bin edges.
        self.guard = ((abs(offsets)+self.df/2 <= .4*profile.sample_rate_hz)
                      & (abs(offsets) > 2*self.df))
        for fc in self.masks:
            self.guard &= abs(offsets + profile.center_hz-fc) > profile.bandwidth_hz/2+profile.max_cfo_hz+self.df/2
        self.guard_bins = int(self.guard.sum())
        self.source = "out_of_channel" if self.guard_bins else "in_channel_fallback"
        self.window = np.hanning(self.bins).astype(np.float32)
        self.scale = self.bins*float(self.window @ self.window)
        self.frames = []
        self.tail = np.empty(0, np.complex64)
        self.end = None
        self.next_frame = 0
        self.recent = deque(maxlen=120)
        self.floors = {}
        self.rows = deque(maxlen=self.floor_rows)
        self.usable_rows = deque(maxlen=self.floor_rows)
        self.initial = []
        self.reference = None
        self.reference_until = None
        self.active = False
        self.rises = 0
        self.gain_db = None
        self.gain_changes = 0
        self.alarm = {}
        self.file = (Path(folder)/"rf_diagnostics.csv").open("x", newline="", encoding="utf-8")
        self.writer = csv.writer(self.file)
        self.writer.writerow(["capture_s", *[f"floor_dbfs_{fc}" for fc in self.masks],
                              *[f"raised_{fc}" for fc in self.masks],
                              *[f"dbfs_bin_{i}" for i in range(self.bins)],
                              "desense_source", "desense_floor_dbfs", "desense_fast_dbfs",
                              "desense_reference_dbfs", "desense_raised", "tuner_gain_db"])

    def set_gain(self, gain_db):
        if gain_db is None or self.gain_db == gain_db:
            return
        self.gain_changes += int(self.gain_db is not None)
        self.gain_db = gain_db
        self.end = None  # drop spectral frames from the previous gain at the next feed
        self.rows.clear()
        self.usable_rows.clear()
        self.initial.clear()
        self.reference = self.reference_until = None
        self.active = False
        self.floors.clear()
        self.alarm.clear()

    def feed(self, start, iq):
        if self.end != start:
            self.frames.clear()
            self.tail = np.empty(0, np.complex64)
            self.next_frame = start
            self.rows.clear()
            self.usable_rows.clear()
            self.active = False
        self.end = start + len(iq)
        if len(self.tail):
            start -= len(self.tail)
            iq = np.concatenate((self.tail, iq))
        while self.next_frame + self.bins <= self.end:
            at = self.next_frame-start
            self.frames.append(iq[at:at+self.bins].copy())
            self.next_frame += self.stride
            if len(self.frames) == self.frames_per_row:
                self.measure((start+at+self.bins)/self.p.sample_rate_hz)
                self.frames.clear()
        self.tail = iq[max(0, self.next_frame-start):].copy()

    def measure(self, seconds):
        samples = np.asarray(self.frames)
        spectra = fft.fft(samples*self.window, axis=1)
        frames = fft.fftshift(abs(spectra)**2, axes=1)/self.scale
        powers = frames.mean(axis=0)
        db = 10*np.log10(np.maximum(powers, 1e-15))
        densities = [np.median(powers[mask]) for mask in self.masks.values()]
        # Hard-limited chirps can depress the measured guard noise. An impulse
        # is tolerable; a repeatedly clipped FFT cannot establish a gain reference.
        components = samples.view(samples.real.dtype)
        clean = np.count_nonzero(abs(components) >= 127.5/128,axis=1) <= 2
        clean_count = int(clean.sum())
        if self.guard_bins:
            # A chirp (or its clipping products) visits individual guard bins briefly.
            # Estimate each bin from its quieter FFTs before combining bins. Normalise
            # the exponential periodogram order statistic to equivalent mean power.
            guard = frames[:,self.guard]
            if self.minimum_clean_frames <= clean_count < self.frames_per_row:
                guard = guard[clean]
            count = len(guard)
            quiet_rank = count//5
            quiet = np.partition(guard,quiet_rank,axis=0)[quiet_rank]
            expected = sum(1/k for k in range(count-quiet_rank,count+1))
            densities.append(np.median(quiet)/expected)
        else:
            densities.append(np.median(densities))
        row = 10*np.log10(np.maximum(np.asarray(densities)*self.p.bandwidth_hz/self.df, 1e-15))
        self.update_floors(seconds,row,clean_count)
        levels = np.rint(db).astype(int).tolist()
        self.recent.append([seconds, levels])
        values = list(self.floors.values())
        self.writer.writerow([f"{seconds:.6f}", *[r["dbfs"] for r in values], *[int(r["raised"]) for r in values],
                              *levels, self.source, self.alarm["dbfs"], self.alarm["fast_dbfs"],
                              self.alarm["reference_dbfs"], int(self.active), self.gain_db])
        self.file.flush()

    def update_floors(self, seconds, row, clean_count):
        usable = clean_count >= self.minimum_clean_frames
        self.rows.append(row)
        self.usable_rows.append(usable)
        history = np.asarray(self.rows)
        slow = np.percentile(history, 20, axis=0)
        valid = np.asarray(self.usable_rows)
        if valid.any() and not valid.all():
            slow[-1] = np.percentile(history[valid,-1],20)
        fast_window = history[-self.fast_rows:,-1][valid[-self.fast_rows:]]
        fast = (float(np.percentile(fast_window,20)) if len(fast_window)
                else self.alarm.get("fast_dbfs",float(row[-1])))
        if usable and self.reference_until is None:
            self.reference_until = seconds+self.reference_s
        if usable and (len(self.initial) < self.minimum_reference_rows or
                       (seconds <= self.reference_until and row[-1] < self.reference[-1]+self.clear_db)):
            self.initial.append(row)
            self.reference = np.percentile(self.initial, 20, axis=0)
        reference_row = row if self.reference is None else self.reference
        reference = float(reference_row[-1])
        level = fast if self.guard_bins else float(slow[-1])
        was_active = self.active
        ready = len(self.initial) >= self.minimum_reference_rows
        if usable and ready and len(fast_window) >= 2:
            self.active = bool(level >= reference+self.rise_db or
                               (was_active and max(level,slow[-1]) >= reference+self.clear_db))
        if self.active and not was_active:
            self.rises += 1
            if self.on_rise:
                self.on_rise("noise_floor_rise", round(seconds*self.p.sample_rate_hz)-1,
                             source=self.source, floor_dbfs=round(level,2), reference_dbfs=round(reference,2))
            if self.warn:
                band = "out of channel" if self.guard_bins else "in channel (fallback)"
                self.warn(f"Noise floor rose {level-reference:.1f} dB {band} at {seconds:.1f} s ({level:.1f} dBFS).")
        self.alarm = dict(dbfs=round(float(slow[-1]),2), fast_dbfs=round(fast,2),
                          reference_dbfs=round(reference,2), raised=self.active,
                          reference_ready=ready, usable_fft_frames=clean_count)
        for i,fc in enumerate(self.masks):
            self.floors[str(fc)] = dict(dbfs=round(float(slow[i]),2), reference_dbfs=round(float(reference_row[i]),2),
                                        raised=self.active, rises=self.rises)

    def snapshot(self):
        return dict(file="rf_diagnostics.csv", center_hz=self.p.center_hz, sample_rate_hz=self.p.sample_rate_hz,
                    bin_hz=self.df, interval_s=self.frames_per_row*self.stride/self.p.sample_rate_hz,
                    floors=dict(self.floors), waterfall=list(self.recent),
                    desense=dict(self.alarm, source=self.source, guard_bins=self.guard_bins, rises=self.rises,
                                 gain_db=self.gain_db, gain_changes=self.gain_changes,
                                 floor_window_s=10, fast_window_s=1.5, reference_window_s=self.reference_s,
                                 rise_db=self.rise_db, clear_db=self.clear_db))


class ClipSkipped(Exception):
    """A storage guard declined a clip before it was saved."""


class EventIQ:
    """Two seconds before and one after a trigger, clipped at real segment boundaries."""
    cooldown_s = 30

    def __init__(self, folder, profile, limits=EventIQLimits()):
        self.folder = Path(folder)/"events"
        self.fs = profile.sample_rate_hz
        self.limits = limits
        self.max_bytes = limits.max_mb*1_000_000
        # Only the single writer charges these totals, including exact sidecar bytes.
        self.saved_bytes = self.saved_iq_bytes = 0
        self.before, self.after = 2*self.fs, self.fs
        self.blocks = deque()
        self.end = 0
        self.pending = []
        self.trigger_samples = defaultdict(list)
        self.counts = Counter()
        self.skipped = Counter()
        self.skipped_by_kind = defaultdict(Counter)
        self.saved = []
        self.futures = []
        self.error = None
        self.writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="event-iq")
        self.metadata = dict(center_hz=profile.center_hz, receiver_profile=profile.report())
        self.origin = None

    def feed(self, start, raw):
        if self.end != start:
            self.flush(partial=True)
            self.blocks.clear()
        self.blocks.append((start, raw))
        self.end = start + len(raw)//2
        # A full input block of slack also covers delayed FIR callbacks.
        earliest = self.end-self.before-self.after-BLOCK_SAMPLES
        while self.blocks and self.blocks[0][0]+len(self.blocks[0][1])//2 <= earliest:
            self.blocks.popleft()

    def trigger(self, kind, sample, **details):
        self.counts[kind] += 1
        if not self.blocks or sample < self.blocks[0][0] or sample > self.end:
            self.skip(kind, "outside_ring")
            return
        self.collect()
        # The SIC pass revisits earlier times. Check every one-hour window that
        # could contain the proposed clip, including saved or reserved future clips.
        times = self.trigger_samples[kind]
        i = bisect_left(times, sample)
        cooldown = self.cooldown_s*self.fs
        if (i and sample-times[i-1] < cooldown) or (i < len(times) and times[i]-sample < cooldown):
            self.skip(kind, "cooldown")
            return
        hour = 3600*self.fs
        lo, hi = bisect_right(times,sample-hour), bisect_left(times,sample+hour)
        nearby = times[lo:i]+[sample]+times[i:hi]
        limit = self.limits.per_kind_hour
        if any(b-a < hour for a,b in zip(nearby,nearby[limit:])):
            self.skip(kind, "hourly_limit")
            return
        if self.saved_bytes >= self.max_bytes:
            self.skip(kind, "size_cap")
            return
        times.insert(i,sample)
        self.pending.append(dict(reason=kind, sample=sample, **details))

    def skip(self, kind, reason):
        self.skipped[reason] += 1
        self.skipped_by_kind[kind][reason] += 1
        self.counts["skipped_captures"] += 1

    def release(self, event, reason):
        kind = event["reason"]
        times = self.trigger_samples[kind]
        times.pop(bisect_left(times,event["sample"]))
        self.skip(kind, reason)

    def write_clip(self, data, meta, name):
        meta.update(bytes_written=len(data), bytes_received=len(data), bytes_dropped=0, gaps=[],
                    sha256=hashlib.sha256(data).hexdigest())
        sidecar = (json.dumps(meta, indent=2, allow_nan=False)+"\n").encode("utf-8")
        size = len(data)+len(sidecar)
        if self.saved_bytes+size > self.max_bytes:
            raise ClipSkipped("size_cap")
        self.folder.mkdir(exist_ok=True)
        if shutil.disk_usage(self.folder).free-size < MIN_FREE_BYTES:
            raise ClipSkipped("low_disk")
        path = self.folder/name
        info = path.with_suffix(".iq.json")
        if path.exists() or info.exists():
            raise FileExistsError(name)
        # An IQ filename becomes visible only once its bytes and sidecar are complete.
        with TemporaryDirectory(dir=self.folder) as work:
            staged = Path(work)/name
            staged.write_bytes(data)
            staged_info = staged.with_suffix(".iq.json")
            staged_info.write_bytes(sidecar)
            self.publish(staged_info,info)
            try:
                self.publish(staged,path)
            except OSError:
                info.unlink()  # This attempt exclusively published the sidecar.
                raise
        self.saved_bytes += size
        self.saved_iq_bytes += len(data)
        return str(Path("events") / name)

    @staticmethod
    def publish(staged, target):
        if os.name == "nt":
            staged.rename(target)  # Windows rename fails if the target exists, also on exFAT.
        else:
            os.link(staged,target)  # POSIX rename would replace a pre-existing file.

    def collect(self, wait=False):
        remaining = []
        for future,event in self.futures:
            if future.done() or wait:
                try:
                    self.saved.append(future.result())
                except ClipSkipped as exc:
                    self.release(event,str(exc))
                    if str(exc) == "low_disk":
                        self.error = "Low disk space for event IQ"
                        self.counts["write_errors"] += 1
                except FileExistsError:
                    self.release(event,"file_exists")
                except OSError as exc:
                    self.error = str(exc)
                    self.counts["write_errors"] += 1
                    self.release(event,"write_error")
            else:
                remaining.append((future,event))
        self.futures = remaining

    def flush(self, partial=False):
        self.collect()
        waiting = []
        for event in self.pending:
            sample = event["sample"]
            if not partial and self.end < sample+self.after:
                waiting.append(event)
                continue
            first = max(self.blocks[0][0], sample-self.before)
            last = min(self.end, sample+self.after)
            if first >= last or len(self.futures) >= 2:
                self.release(event, "outside_ring" if first >= last else "writer_busy")
                continue
            chunks = (raw[2*max(0,first-start):2*(last-start)] for start,raw in self.blocks
                      if start < last and start+len(raw)//2 > first)
            name = f"{event['reason']}_{sample}.iq"
            meta = dict(self.metadata, format="cu8", file=name, sample_rate_hz=self.fs,
                        event=event, source_start_sample=first, source_end_sample=last,
                        event_offset_samples=sample-first, parent_run=self.folder.parent.name)
            if self.origin:
                meta["started_utc"] = (self.origin + timedelta(seconds=first/self.fs)).isoformat()
            future = self.writer.submit(self.write_clip, b"".join(chunks), meta, name)
            self.futures.append((future,event))
        self.pending = waiting

    def close(self):
        self.flush(partial=True)
        self.writer.shutdown(wait=True)
        self.collect(wait=True)
        self.blocks.clear()
        return dict(files=self.saved, observed=dict(self.counts), error=self.error, cooldown_s=self.cooldown_s,
                    max_iq_bytes=self.max_bytes, max_total_bytes=self.max_bytes,
                    max_per_kind_per_hour=self.limits.per_kind_hour, saved_bytes=self.saved_bytes,
                    saved_iq_bytes=self.saved_iq_bytes, skipped_by_reason=dict(self.skipped),
                    skipped_by_kind={kind:dict(counts) for kind,counts in self.skipped_by_kind.items()})
