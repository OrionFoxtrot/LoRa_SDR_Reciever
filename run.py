"""Receiver lifecycle shared by Windows launchers, GUI and acceptance tests."""
from collections import Counter, defaultdict, deque
import csv
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import shutil
import threading
import time
import numpy as np

from gain import AutoSetCancelled, run_autoset, supported_gains
from receiver import Receiver
from reports import FIELDS, directory, periods, telemetry, write_json, write_packets
from sic import Cancellation, merge_passes
from sources import BLOCK_SAMPLES, Layout, LiveInput, Radio, clipped, complex_iq, read_exact


class Run:
    def __init__(self, profile, parent, mode, evidence, notify, *, folder=None, name="", sic=False):
        self.path = Path(folder) if folder is not None else directory(parent)
        self.p, self.mode, self.notify = profile, mode, notify
        # Only the optional offline cancellation pass needs a waveform donor list.
        self.rows = [] if sic else None
        self.journal = "packets_primary.csv" if sic else "packets.csv"
        self.committed = 0
        self.valid_by_period = defaultdict(set)
        self.origin = None
        self.clock_source = "replay_decode_utc"
        self.counts = Counter()
        self.recent = deque(maxlen=12)
        self.total = self.processed = self.clips = 0
        self.latest_clip = self.max_clip = self.busy = 0.
        self.watermark = 0.
        self.dropped = 0
        self.dropped_blocks = 0
        self.recorder_dropped = 0
        self.recorder_warning = None
        self.backlogs = deque(maxlen=4096)
        self.last_update = -10.
        self.started = time.perf_counter()
        self.file = (self.path/self.journal).open("x", newline="", encoding="utf-8")
        self.writer = csv.DictWriter(self.file, FIELDS)
        self.writer.writeheader()
        self.file.flush()
        self.summary = dict(mode=mode, evidence=evidence, run_id=self.path.name, run_name=name or self.path.name, profile=profile.report(),
                            run_directory=str(self.path.resolve()), started_utc=datetime.now(timezone.utc).isoformat(),
                            decoder_version=importlib.metadata.version("lora-phy"), status="running",
                            branches={}, false_decodes=None, duplicate_decodes=None,
                            upstream_usb_dropped_samples=None)
        self.branches = {f"{fc}/SF{sf}": {"packets":0,"phy_pass":0,"app_pass":0}
                         for fc in profile.carriers_hz for sf in profile.spreading_factors}
        self.rx = Receiver(profile, self.packet)
        self.pass_name = "primary"
        self.hash = hashlib.sha256()
        self.dsp_counts = Counter()
        write_json(self.path/"summary.json", self.summary)

    def packet(self, row):
        row = telemetry(row, self.pass_name)
        stamp = self.origin + timedelta(seconds=row["capture_s"]) if self.origin else datetime.now(timezone.utc)
        row.update(run_id=self.path.name, run_name=self.summary["run_name"],
                   receive_utc=stamp.isoformat(timespec="microseconds"), timestamp_source=self.clock_source)
        if self.rows is not None:
            self.rows.append(row)
        if self.pass_name == "primary":
            self.writer.writerow(row)
            self.file.flush()
            self.committed += 1
            self.record_row(row)

    def record_row(self, row):
        branch = self.branches[f"{row['carrier_hz']}/SF{row['sf']}"]
        branch["packets"] += 1
        branch["phy_pass"] += int(row["phy_crc_ok"] is True)
        branch["app_pass"] += int(row["application_crc_ok"] is True)
        for key in ("phy_crc_ok", "application_crc_ok"):
            self.counts[f"{key}/{row[key]}"] += 1
        if row["phy_crc_ok"] and row["application_crc_ok"]:
            self.recent.append(row["chipsat_id"])
            self.valid_by_period[int(row["capture_s"]//30)].add(row["chipsat_id"])

    def block(self, start, raw, cancel=None):
        t0 = time.perf_counter()
        n = len(raw)//2
        if cancel is None:
            rails = clipped(raw)
            self.clips += rails
            self.latest_clip = rails/n
            self.max_clip = max(self.max_clip, self.latest_clip)
            self.total = start+n
            self.processed += n
        iq = complex_iq(raw)
        if cancel is not None:
            cancel.apply(start, iq)
        self.rx.feed(start, iq)
        self.busy += time.perf_counter()-t0

    def progress(self, lag=0., live=False, force=False):
        now = time.perf_counter()-self.started
        self.watermark = max(self.watermark, lag)
        if now-self.last_update >= .45 or force:
            for channel in self.rx.channels:
                for b in channel.branches:
                    key = f"{channel.fc}/SF{b.sf}"
                    self.branches[key]["syncs"] = self.rx.counts[f"{key}/syncs"] + b.counts["syncs"]
            if live:
                self.backlogs.append((now, lag))
            if self.notify:
                self.notify(dict(path=str(self.path), capture_s=self.total/self.p.sample_rate_hz,
                                 journal=self.journal, committed=self.committed,
                                 evidence=self.summary["evidence"], dropped_blocks=self.dropped_blocks,
                                 dsp_realtime=self.processed/(self.p.sample_rate_hz*self.busy) if self.busy else None,
                                 gain_db=self.summary.get("gain_db", self.summary.get("sidecar", {}).get("gain_db")),
                                 peak_clip_fraction=self.max_clip,
                                 branches={k:dict(v) for k,v in self.branches.items()}, recent_ids=list(self.recent),
                                 counts=dict(self.counts), clipped_fraction=self.latest_clip,
                                 dropped_samples=self.dropped, recorder_dropped_samples=self.recorder_dropped,
                                 recorder_warning=self.recorder_warning, backlog_s=lag, pass_name=self.pass_name))
            self.last_update = now

    def close(self, status, error=None):
        self.rx.close_segment()
        self.dsp_counts.update(self.rx.counts)
        for key,branch in self.branches.items():
            branch["syncs"] = self.dsp_counts[f"{key}/syncs"]
        self.file.close()
        if self.journal == "packets_primary.csv":
            shutil.copyfile(self.path/self.journal, self.path/"packets.csv")
        duration = self.total/self.p.sample_rate_hz
        elapsed = time.perf_counter()-self.started
        growth = slope = None
        values = list(self.backlogs)
        if len(values) >= 8 and values[-1][0]-values[0][0] >= 5:
            t, lag = np.asarray(values).T
            slope = float(np.polyfit(t, lag, 1)[0])
            growth = bool(slope > .01 and np.mean(lag[-3:])-np.mean(lag[:3]) > .13)
        ids = ((period*30, node) for period,nodes in self.valid_by_period.items() for node in nodes)
        self.summary.update(status=status, error=error, capture_seconds=duration,
            processed_samples=self.processed, dropped_samples=self.dropped, dropped_blocks=self.dropped_blocks,
            processing_seconds=self.busy,
            processing_x_realtime=duration/self.busy if self.busy else None,
            elapsed_seconds=elapsed, elapsed_x_realtime=duration/elapsed if elapsed else None,
            clipped_samples=self.clips, clipped_sample_fraction=self.clips/max(1,self.processed),
            peak_block_clip_fraction=self.max_clip, peak_backlog_s=self.watermark,
            backlog_slope_s_per_s=slope, backlog_growing=growth, backlog_history_s=values,
            branches=self.branches, crc_counts=dict(self.counts), decoder_counters=dict(self.dsp_counts),
            packets=self.committed, **periods(ids,duration))
        write_json(self.path/"summary.json", self.summary)
        self.progress(force=True)
        self.rx = self.rows = None


def calibration(radio, stop, progress=print):
    fs = radio.p.sample_rate_hz
    def capture(seconds):
        length = 2*round(fs*seconds)
        raw = read_exact(radio.sock, length, stop)
        if len(raw) != length:
            raise InterruptedError("Capture stopped during gain setup")
        return raw
    def set_gain(gain):
        radio.command(4, round(gain*10))
        capture(.35)
    return run_autoset(supported_gains(radio.header), radio.gain, set_gain=set_gain,
                       capture=capture, cancelled=stop.is_set, progress=progress)


def live(profile, parent, gain=10., ppm=0, device=0, seconds=None, connect=None,
         record=True, auto_gain=False, stop=None, notify=None, evidence="live hardware",
         gain_progress=print, folder=None, name=""):
    profile.checked()
    if seconds is not None and (not math.isfinite(seconds) or seconds<=0):
        raise ValueError("Live duration must be positive")
    if not math.isfinite(gain) or not -10 <= gain <= 60:
        raise ValueError("Invalid manual tuner gain")
    stop = stop or threading.Event()
    run = Run(profile, parent, "live", evidence, notify, folder=folder, name=name)
    radio = Radio(profile,gain,ppm,connect,device)
    status, error, source = "complete", None, None
    run.summary.update(requested_gain_db=gain, auto_gain_requested=auto_gain)
    try:
        radio.open(run.path, stop)
        if auto_gain:
            scan = calibration(radio, stop, gain_progress)
            radio.gain = scan["gain_db"]
            run.summary["gain_setup"] = scan
            gain_progress(f"Gain set to {radio.gain:g} dB. Starting capture…")
        # External rtl_tcp connections may contain pre-command buffered samples.
        # Start capture after the tuning/gain settling interval.
        read_exact(radio.sock, 2*round(.25*profile.sample_rate_hz), stop)
        if stop.is_set():
            raise InterruptedError("Stopped before capture")
        run.summary.update(gain_db=radio.gain, ppm=ppm, device=device,
                           rtl0_header_hex=radio.header.hex(), rtl_tcp_endpoint=connect or "local child")
        source = LiveInput(radio,run.path,stop,seconds,record,synthetic=evidence.startswith("synthetic"))
        run.origin = (datetime.fromisoformat(source.recorder.started_utc) if source.recorder
                      else datetime.now(timezone.utc))
        run.clock_source = "live_capture_utc"
        run.summary["capture_start_utc"] = run.origin.isoformat()
        for start, raw in source.blocks():
            run.hash.update(raw)
            run.block(start,raw)
            run.dropped = source.dropped
            run.dropped_blocks = source.dropped_blocks
            if source.recorder:
                run.recorder_dropped = (source.recorder.dropped+1)//2
                run.recorder_warning = source.recorder.stopped_reason
            lag = max(0.,(source.produced-run.total)/profile.sample_rate_hz)
            run.progress(lag, live=not source.finished.is_set())
        if seconds is None or source.produced < round(seconds*profile.sample_rate_hz):
            status = "stopped"
    except KeyboardInterrupt:
        status = "stopped"
    except (AutoSetCancelled, InterruptedError) as exc:
        if not stop.is_set():
            status, error = "failed", str(exc)
            raise
        status = "stopped"
    except Exception as exc:
        status, error = "failed", str(exc)
        raise
    finally:
        stop.set()
        radio.close()
        if source:
            run.dropped, run.total = source.dropped, source.produced
            run.dropped_blocks = source.dropped_blocks
            if source.record_stats:
                run.recorder_warning = source.record_stats.get("stopped_reason")
                run.recorder_dropped = (source.record_stats["bytes_dropped"]+1)//2
            run.summary.update(recording=source.record_stats, queue_highwater_blocks=source.highwater,
                               capture_sha256=(source.record_stats or {}).get("sha256"),
                               trailing_unpaired_bytes=source.odd_bytes,
                               processed_iq_sha256=run.hash.hexdigest())
        run.close(status,error)
    return run.path


def replay(profile, path, parent, *, sic=False, paced=False, stop=None, notify=None,
           block_samples=BLOCK_SAMPLES, folder=None, name=""):
    path = Path(path)
    layout = Layout.read(path)
    profile = profile.tuning(layout.metadata.get("center_hz", profile.center_hz))
    stop = stop or threading.Event()
    evidence = "synthetic IQ replay" if layout.metadata.get("synthetic") else "recorded IQ replay"
    run = Run(profile,parent,"replay",evidence,notify,folder=folder,name=name,sic=sic)
    status, error = "complete", None
    try:
        stamp = layout.metadata.get("started_utc") or layout.metadata.get("capture_start_utc")
        if stamp:
            try:
                origin = datetime.fromisoformat(stamp)
            except (TypeError, ValueError):
                origin = None
            if origin and origin.tzinfo:
                run.origin = origin.astimezone(timezone.utc)
                run.clock_source = "recorded_capture_utc"
        run.dropped_blocks = None if layout.unavailable else 0
        fingerprint = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(1024*1024):
                if stop.is_set():
                    status = "stopped"
                    return run.path
                fingerprint.update(chunk)
        digest = fingerprint.hexdigest()
        if layout.metadata.get("sha256") and digest != layout.metadata["sha256"]:
            raise ValueError("Recording SHA-256 disagrees with its sidecar")
        run.summary.update(capture_path=str(path.resolve()), capture_sha256=digest, paced=paced,
                           sidecar=layout.metadata, iq_format="CU8", gap_count=len(layout.metadata.get("gaps", [])),
                           recorded_unavailable_samples=layout.unavailable, sic_enabled=sic)
        t0 = time.perf_counter()
        for start, raw in layout.blocks(path,block_samples):
            if stop.is_set():
                break
            if paced:
                if stop.wait(max(0, t0+start/profile.sample_rate_hz-time.perf_counter())):
                    break
            if sic:
                run.hash.update(raw)
            run.block(start,raw)
            run.dropped = run.total-run.processed
            lag = max(0., time.perf_counter()-t0-run.total/profile.sample_rate_hz) if paced else 0.
            run.progress(lag, live=paced)
        if not stop.is_set():
            run.total = layout.samples
            run.dropped = layout.unavailable
        if sic and not stop.is_set():
            run.rx.close_segment()
            run.dsp_counts.update(run.rx.counts)
            primary = run.rows
            cancel = Cancellation(profile,primary)
            run.pass_name = "sic"
            run.rx = Receiver(profile,run.packet)
            run.rows = []
            residual_input = hashlib.sha256()
            for start, raw in layout.blocks(path,block_samples):
                if stop.is_set():
                    break
                residual_input.update(raw)
                run.block(start,raw,cancel)
                run.progress()
            run.rx.close_segment()
            identical = residual_input.digest() == run.hash.digest()
            if not stop.is_set() and not identical:
                raise ValueError("IQ changed between the primary and cancellation passes")
            write_packets(run.path/"packets_residual.csv",run.rows)
            run.rows, repeated = merge_passes(primary,run.rows)
            run.summary["sic"] = dict(donors=len(cancel.donors), fit_segments=cancel.fitted,
                    identical_input_bytes=identical, primary_input_sha256=run.hash.hexdigest(),
                    residual_input_sha256=residual_input.hexdigest(),
                    rejected_segments=cancel.skipped, repeated_detections=repeated,
                    removed_energy_adc_normalized=cancel.removed_energy, primary_packets=len(primary))
            run.counts.clear()
            run.recent.clear()
            run.valid_by_period.clear()
            for branch in run.branches.values():
                branch.update(packets=0,phy_pass=0,app_pass=0)
            for row in run.rows:
                run.record_row(row)
            run.file.close()
            write_packets(run.path/"packets.csv",run.rows)
            run.journal = "packets.csv"
            run.committed = len(run.rows)
        if stop.is_set():
            status = "stopped"
    except KeyboardInterrupt:
        status = "stopped"
    except Exception as exc:
        status, error = "failed", str(exc)
        raise
    finally:
        run.close(status,error)
    return run.path
