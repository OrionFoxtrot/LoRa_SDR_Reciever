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

from gain import AutoSetCancelled, manual_gain_db, run_autoset, supported_gains
from fleet_view import Packet
from config import EventIQLimits, carrier_plan_warnings
from front_end import FrontEndPreferences, gain_reminder, preset_for
from mission import Mission, PeriodBook
from receiver import Receiver
from reports import FIELDS, directory, periods, telemetry, write_json, write_packets
from rf_diagnostics import EventIQ, FrequencyOffsets, SpectrumMonitor
from sic import Cancellation, merge_passes
from sources import BLOCK_SAMPLES, Layout, LiveInput, Radio, complex_iq, rail_indices, read_exact


class Run:
    def __init__(self, profile, parent, mode, evidence, notify, *, folder=None, name="", sic=False, mission=None,
                 plan_warning=None, front_end=None, rf_warning=None, plan_roster=None, event_limits=EventIQLimits()):
        self.path = Path(folder) if folder is not None else directory(parent)
        self.p, self.mode, self.notify = profile, mode, notify
        # Only the optional offline cancellation pass needs a waveform donor list.
        self.rows = [] if sic else None
        self.journal = "packets_primary.csv" if sic else "packets.csv"
        self.committed = 0
        self.valid_by_period = defaultdict(set)
        self.origin = None
        self.book=PeriodBook(mission or Mission())
        self.gaps=[]
        self.recorded_gaps=None
        self.recorded_dropouts=None
        self.last_iq_wall=self.last_valid_wall=None
        self.clip_streak=0.
        self.pass_fraction=0.
        self.rf_clock=0.
        self.active_dropout = None
        self.dropout_count = 0
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
        self.branches = {f"{fc}/SF{sf}": {"packets":0,"phy_pass":0,"app_pass":0,
                                         "phy_crc_pass":0,"phy_crc_fail":0,"trusted_packets":0,"suspect_packets":0}
                         for fc in profile.carriers_hz for sf in profile.spreading_factors}
        self.summary["front_end"] = front_end
        self.event_iq = EventIQ(self.path, profile, event_limits)
        self.spectrum = SpectrumMonitor(self.path, profile, rf_warning, self.header_event)
        self.offsets = FrequencyOffsets(profile,rf_warning)
        self.event_iq.metadata["synthetic"] = evidence.startswith("synthetic")
        self.event_iq.metadata["front_end"] = front_end
        self.rx = Receiver(profile, self.packet, self.header_event)
        self.pass_name = "primary"
        self.hash = hashlib.sha256()
        self.dsp_counts = Counter()
        warnings = carrier_plan_warnings(profile, self.book.m.roster if plan_roster is None else plan_roster)
        self.summary["carrier_plan_warnings"] = warnings
        write_json(self.path/"summary.json", self.summary)
        if warnings and plan_warning:
            plan_warning(warnings)

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
        self.offsets.add(row)
        branch = self.branches[f"{row['carrier_hz']}/SF{row['sf']}"]
        branch["packets"] += 1
        branch["suspect_packets"] += int(row.get("timing_check")=="suspect")
        branch["phy_pass"] += int(row["phy_crc_ok"] is True)
        branch["phy_crc_pass"] += int(row["phy_crc_ok"] is True)
        branch["phy_crc_fail"] += int(row["phy_crc_ok"] is False)
        branch["app_pass"] += int(row["application_crc_ok"] is True)
        for key in ("phy_crc_ok", "application_crc_ok"):
            self.counts[f"{key}/{row[key]}"] += 1
        if row["phy_crc_ok"] and row["application_crc_ok"]:
            branch["trusted_packets"] += 1
            self.last_valid_wall=time.monotonic()
            self.book.hear(Packet.read(row))
            self.recent.append(row["chipsat_id"])
            self.valid_by_period[int(row["capture_s"]//30)].add(row["chipsat_id"])

    def header_event(self, reason, sample, **details):
        self.event_iq.trigger(reason, sample, pass_name=self.pass_name, **details)

    def block(self, start, raw, cancel=None):
        # Limit raw history and callback latency even for a caller's very large replay block.
        if len(raw) > 2*BLOCK_SAMPLES:
            for offset in range(0, len(raw), 2*BLOCK_SAMPLES):
                self.block(start+offset//2, raw[offset:offset+2*BLOCK_SAMPLES], cancel)
            return
        t0 = time.perf_counter()
        n = len(raw)//2
        if self.rx.next_sample is not None and self.rx.next_sample != start:
            self.rx.close_segment()
        self.event_iq.origin = self.origin
        self.event_iq.feed(start, raw)
        rail_at = rail_indices(raw)
        if cancel is None:
            if start>self.total and self.recorded_gaps is None:
                self.gaps.append((self.total/self.p.sample_rate_hz,start/self.p.sample_rate_hz))
            rails = len(rail_at)
            self.clips += rails
            self.latest_clip = rails/n
            self.clip_streak=(self.clip_streak+n/self.p.sample_rate_hz) if self.latest_clip>1e-4 else 0.
            self.max_clip = max(self.max_clip, self.latest_clip)
            self.total = start+n
            self.processed += n
            if rails:
                self.event_iq.trigger("rail", start+int(rail_at[0]), rail_samples_in_block=rails, pass_name="primary")
        iq = complex_iq(raw)
        if cancel is not None:
            cancel.apply(start, iq)
        self.rx.feed(start, iq, rail_at)
        if cancel is None:
            self.spectrum.feed(start, iq)
        self.event_iq.flush()
        self.busy += time.perf_counter()-t0

    def update_stages(self):
        counts = self.dsp_counts + self.rx.counts
        for channel in self.rx.channels:
            for b in channel.branches:
                counts.update({f"{channel.fc}/SF{b.sf}/{k}": v for k,v in b.counts.items()})
        for key, branch in self.branches.items():
            for stage in ("syncs", "headers", "header_rejects", "unstable_headers"):
                branch[stage] = counts[f"{key}/{stage}"]
        return counts

    def progress(self, lag=0., live=False, force=False):
        now = time.perf_counter()-self.started
        self.watermark = max(self.watermark, lag)
        if now-self.last_update >= .45 or force:
            self.update_stages()
            capture_s = max(self.rf_clock,self.total/self.p.sample_rate_hz)
            if self.recorded_dropouts is not None:
                self.dropout_count = sum(g["start_sample"] < capture_s*self.p.sample_rate_hz
                                         for g in self.recorded_dropouts)
            offsets = self.offsets.snapshot(capture_s)
            if live:
                self.backlogs.append((now, lag))
            if self.notify:
                self.notify(dict(path=str(self.path), capture_s=capture_s,cfo_margin=offsets,
                                 settled_s=self.total/self.p.sample_rate_hz if force else max(self.rf_clock,self.rx.settled_before()/self.p.sample_rate_hz),
                                 gaps=(list(self.recorded_gaps if self.recorded_gaps is not None else self.gaps)
                                       + ([(self.active_dropout["start_sample"]/self.p.sample_rate_hz,self.rf_clock)]
                                          if self.active_dropout else [])),
                                 reconnecting=bool(self.active_dropout),dropout_count=self.dropout_count,
                                 reconnect_attempts=(self.active_dropout or {}).get("reconnect_attempts",0),
                                 mission=self.book.m.dump(),last_iq_wall=self.last_iq_wall,
                                 last_valid_wall=self.last_valid_wall,clip_streak=self.clip_streak,pass_fraction=self.pass_fraction,
                                 journal=self.journal, committed=self.committed,
                                 evidence=self.summary["evidence"], dropped_blocks=self.dropped_blocks,
                                 dsp_realtime=self.processed/(self.p.sample_rate_hz*self.busy) if self.busy else None,
                                 gain_db=self.summary.get("gain_db", self.summary.get("sidecar", {}).get("gain_db")),
                                 gain_warning=(self.summary.get("gain_setup", {}).get("warning")
                                               or self.summary.get("front_end_gain_reminder")),
                                 front_end=self.summary.get("front_end"),
                                 peak_clip_fraction=self.max_clip,
                                 rf_diagnostics=self.spectrum.snapshot(),
                                 branches={k:dict(v) for k,v in self.branches.items()}, recent_ids=list(self.recent),
                                 counts=dict(self.counts), clipped_fraction=self.latest_clip,
                                 dropped_samples=self.dropped, recorder_dropped_samples=self.recorder_dropped,
                                 recorder_warning=self.recorder_warning, backlog_s=lag, pass_name=self.pass_name))
            self.last_update = now

    def close(self, status, error=None):
        self.rx.close_segment()
        totals = self.update_stages()
        self.summary["event_iq"] = self.event_iq.close()
        self.spectrum.file.close()
        self.summary["rf_diagnostics"] = self.spectrum.snapshot()
        self.file.close()
        if self.journal == "packets_primary.csv":
            shutil.copyfile(self.path/self.journal, self.path/"packets.csv")
        duration = self.total/self.p.sample_rate_hz
        self.summary["cfo_margin"] = self.offsets.snapshot(duration)
        if self.recorded_dropouts is not None:
            seen = [g for g in self.recorded_dropouts if g["start_sample"]<self.total]
            self.summary.update(source_dropouts=seen,dropout_count=len(seen),
                                source_gap_samples=sum(min(g["end_sample"],self.total)-g["start_sample"] for g in seen))
        if self.recorded_gaps is not None:
            self.gaps=[(a,min(b,duration)) for a,b in self.recorded_gaps if a<duration]
            self.recorded_gaps=self.gaps
        elapsed = time.perf_counter()-self.started
        growth = slope = None
        values = list(self.backlogs)
        if len(values) >= 8 and values[-1][0]-values[0][0] >= 5:
            t, lag = np.asarray(values).T
            slope = float(np.polyfit(t, lag, 1)[0])
            growth = bool(slope > .01 and np.mean(lag[-3:])-np.mean(lag[:3]) > .13)
        ids = ((period*30, node) for period,nodes in self.valid_by_period.items() for node in nodes)
        self.summary.update(status=status, error=error, capture_seconds=duration,
            mission=self.book.m.dump(),mission_periods=self.book.report(duration,self.gaps),gap_ranges_s=self.gaps,
            processed_samples=self.processed, dropped_samples=self.dropped, dropped_blocks=self.dropped_blocks,
            processing_seconds=self.busy,
            processing_x_realtime=duration/self.busy if self.busy else None,
            elapsed_seconds=elapsed, elapsed_x_realtime=duration/elapsed if elapsed else None,
            clipped_samples=self.clips, clipped_sample_fraction=self.clips/max(1,self.processed),
            peak_block_clip_fraction=self.max_clip, peak_backlog_s=self.watermark,
            backlog_slope_s_per_s=slope, backlog_growing=growth, backlog_history_s=values,
            branches=self.branches, crc_counts=dict(self.counts), decoder_counters=dict(totals),
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
         gain_progress=print, folder=None, name="", mission=None,iq_heartbeat=None, plan_warning=None,
         front_end=None, rf_warning=None, event_limits=EventIQLimits(), front_choice=None, front_preferences=None):
    profile.checked()
    if seconds is not None and (not math.isfinite(seconds) or seconds<=0):
        raise ValueError("Live duration must be positive")
    gain = manual_gain_db(gain)
    stop = stop or threading.Event()
    remember_hardware = evidence == "live hardware"
    reminder = None
    if remember_hardware:
        front_preferences = front_preferences or FrontEndPreferences()
        if front_end is None:
            front_choice,front_end = front_preferences.selected()
        front_choice = front_choice or preset_for(front_end)
        if not auto_gain:
            reminder = gain_reminder(front_preferences.last_run(parent),front_end,auto_gain)
        if reminder and rf_warning:
            rf_warning(reminder)
    run = Run(profile, parent, "live", evidence, notify, folder=folder, name=name,mission=mission,
              plan_warning=plan_warning, front_end=front_end, rf_warning=rf_warning, event_limits=event_limits)
    radio = Radio(profile,gain,ppm,connect,device)
    run.offsets.ppm = ppm
    status, error, source = "complete", None, None
    run.summary.update(requested_gain_db=gain, auto_gain_requested=auto_gain, bias_tee_enable_requested=False,
                       front_end_gain_reminder=reminder)
    def prepare():
        radio.open(run.path, stop)
        if auto_gain and "gain_setup" not in run.summary:
            scan = calibration(radio, stop, gain_progress)
            radio.gain = scan["gain_db"]
            run.summary["gain_setup"] = scan
            gain_progress((scan.get("warning") or f"Gain set to {radio.gain:g} dB.") + " Starting capture…")
        # External rtl_tcp connections may contain pre-command buffered samples.
        # Start capture after the tuning/gain settling interval.
        read_exact(radio.sock, 2*round(.25*profile.sample_rate_hz), stop)
        if stop.is_set():
            raise InterruptedError("Stopped before capture")
        run.summary.update(gain_db=radio.gain, ppm=ppm, device=device,
                           rtl0_header_hex=radio.header.hex(), rtl_tcp_endpoint=connect or "local child")
    try:
        origin = (time.monotonic(),datetime.now(timezone.utc))
        initial_error = None
        try:
            prepare()
            origin = None  # The normal capture clock starts after tuning settles.
        except (FileNotFoundError,InterruptedError):
            raise
        except OSError as exc:
            initial_error = exc
            radio.close()
        run.event_iq.metadata.update(gain_db=radio.gain, ppm=ppm, source="rtl_tcp", bias_tee_enable_requested=False)
        source = LiveInput(radio,run.path,stop,seconds,record,synthetic=evidence.startswith("synthetic"),
                           mission=run.book.m.dump(), front_end=front_end,origin=origin,prepare=prepare,
                           initial_error=initial_error,log=rf_warning)
        source.heartbeat=iq_heartbeat
        run.origin = source.origin_utc
        run.clock_source = "live_capture_utc"
        run.summary["capture_start_utc"] = run.origin.isoformat()
        def idle():
            gap = source.active_gap
            if gap:
                run.active_dropout = gap
                run.dropout_count = len(source.dropouts)+1
                if run.rx.next_sample is not None and run.total >= gap["start_sample"]:
                    run.rx.close_segment()
                run.rf_clock = max(run.total/profile.sample_rate_hz,time.monotonic()-source.origin_mono)
                run.progress()
        source.idle = idle
        for start, raw in source.blocks():
            run.active_dropout = None
            run.dropout_count = len(source.dropouts)
            run.rf_clock = 0.
            if radio.gain != run.spectrum.gain_db:
                run.spectrum.set_gain(radio.gain)
                run.summary["gain_db"] = run.event_iq.metadata["gain_db"] = radio.gain
            if remember_hardware:
                remember_hardware = False
                try:
                    front_preferences.save(front_choice,front_end,received=True)
                except OSError as exc:
                    run.summary["front_end_settings_error"] = str(exc)
                    if rf_warning:
                        rf_warning(str(exc))
            run.last_iq_wall=source.last_iq_wall
            run.hash.update(raw)
            run.block(start,raw)
            run.dropped = source.dropped
            run.dropped_blocks = source.dropped_blocks
            if source.recorder:
                run.recorder_dropped = (source.recorder.dropped-source.recorder.source_gap_bytes+1)//2
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
            run.active_dropout = None
            run.dropout_count = len(source.dropouts)
            run.rf_clock = 0.
            run.summary.update(dropout_count=run.dropout_count,source_dropouts=source.dropouts,
                               source_gap_samples=sum(g["end_sample"]-g["start_sample"] for g in source.dropouts))
            if source.produced>run.total:
                run.gaps.append((run.total/profile.sample_rate_hz,source.produced/profile.sample_rate_hz))
            run.dropped, run.total = source.dropped, source.produced
            run.dropped_blocks = source.dropped_blocks
            if source.record_stats:
                run.recorder_warning = source.record_stats.get("stopped_reason")
                run.recorder_dropped = (source.record_stats["bytes_dropped"]-source.record_stats["source_gap_bytes"]+1)//2
                if source.record_stats.get("gaps"):
                    try:
                        recorded=Layout.read(run.path/"capture.iq")
                        run.summary["recording_gap_ranges_s"]=recorded.gap_ranges(profile.sample_rate_hz)
                    except (OSError,ValueError):
                        run.summary["recording_gap_ranges_s"]=None
            run.summary.update(recording=source.record_stats, queue_highwater_blocks=source.highwater,
                               capture_sha256=(source.record_stats or {}).get("sha256"),
                               trailing_unpaired_bytes=source.odd_bytes,
                               processed_iq_sha256=run.hash.hexdigest())
        run.close(status,error)
    return run.path


def replay(profile, path, parent, *, sic=False, paced=False, stop=None, notify=None,
           block_samples=BLOCK_SAMPLES, folder=None, name="",mission=None,pause=None,end_sample=None,plan_warning=None,
           rf_warning=None, event_limits=EventIQLimits()):
    path = Path(path)
    layout = Layout.read(path)
    profile = profile.tuning(layout.metadata.get("center_hz", profile.center_hz))
    stop = stop or threading.Event()
    evidence = "synthetic IQ replay" if layout.metadata.get("synthetic") else "recorded IQ replay"
    recorded_mission = layout.metadata.get("mission") or {}
    mission=(mission or Mission.read(recorded_mission)).aligned(layout.metadata)
    guard_roster = mission.roster or recorded_mission.get("roster", {})
    run = Run(profile,parent,"replay",evidence,notify,folder=folder,name=name,sic=sic,mission=mission,
              plan_warning=plan_warning, front_end=layout.metadata.get("front_end"), rf_warning=rf_warning,
              plan_roster=guard_roster, event_limits=event_limits)
    run.event_iq.metadata.update({k:layout.metadata[k] for k in ("gain_db","ppm","source","bias_tee_enable_requested")
                                  if k in layout.metadata})
    run.spectrum.set_gain(layout.metadata.get("gain_db"))
    run.offsets.ppm = layout.metadata.get("ppm")
    samples=layout.samples if end_sample is None else min(layout.samples,max(0,int(end_sample)))
    def blocks():
        for start,raw in layout.blocks(path,block_samples):
            if start>=samples:
                break
            yield start,raw[:2*(samples-start)]
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
        run.recorded_gaps=layout.gap_ranges(profile.sample_rate_hz)
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
        run.recorded_dropouts = layout.metadata.get("source_dropouts",[])
        t0 = time.perf_counter()
        def wait_until(sample):
            nonlocal t0
            target=sample/profile.sample_rate_hz
            while not stop.is_set():
                if pause is not None and pause.is_set():
                    before=time.perf_counter()
                    stop.wait(.04)
                    t0+=time.perf_counter()-before
                    continue
                elapsed=time.perf_counter()-t0
                if not paced or elapsed>=target:
                    return True
                if sample>run.total:
                    run.rf_clock=min(target,max(0.,elapsed))
                    run.progress()
                stop.wait(min(.04,target-elapsed))
            return False

        for start, raw in blocks():
            if start>run.total:
                run.rx.close_segment()
            if not wait_until(start):
                break
            if sic:
                run.hash.update(raw)
            run.block(start,raw)
            run.pass_fraction=run.total/max(samples,1)
            run.dropped = run.total-run.processed
            lag = max(0., time.perf_counter()-t0-run.total/profile.sample_rate_hz) if paced else 0.
            run.progress(lag, live=paced)
        if samples>run.total and not stop.is_set():
            run.rx.close_segment()
            wait_until(samples)
        if not stop.is_set():
            run.total = samples
            run.dropped = samples-run.processed
        elif run.rf_clock*profile.sample_rate_hz>run.total:
            run.total=round(run.rf_clock*profile.sample_rate_hz)
            run.dropped=run.total-run.processed
        if sic and not stop.is_set():
            run.rx.close_segment()
            run.dsp_counts.update(run.rx.counts)
            primary = run.rows
            cancel = Cancellation(profile,primary,cancelled=stop.is_set)
            run.pass_name = "sic"
            run.rx = Receiver(profile,run.packet,run.header_event)
            run.rows = []
            residual_input = hashlib.sha256()
            for start, raw in blocks():
                if stop.is_set():
                    break
                while pause is not None and pause.is_set() and not stop.is_set():
                    stop.wait(.04)
                if stop.is_set():
                    break
                residual_input.update(raw)
                run.block(start,raw,cancel)
                run.pass_fraction=(start+len(raw)//2)/max(samples,1)
                run.progress()
            run.rx.close_segment()
            identical = residual_input.digest() == run.hash.digest()
            if not stop.is_set() and not identical:
                raise ValueError("IQ changed between the primary and cancellation passes")
            write_packets(run.path/"packets_residual.csv",run.rows)
            run.rows, repeated = merge_passes(primary,run.rows,cancelled=stop.is_set,sample_rate_hz=profile.sample_rate_hz)
            run.summary["sic"] = dict(donors=len(cancel.donors), fit_segments=cancel.fitted,
                    identical_input_bytes=identical, primary_input_sha256=run.hash.hexdigest(),
                    residual_input_sha256=residual_input.hexdigest(),
                    rejected_segments=cancel.skipped, repeated_detections=repeated,
                    removed_energy_adc_normalized=cancel.removed_energy, primary_packets=len(primary))
            run.counts.clear()
            run.offsets.clear_packets()
            run.recent.clear()
            run.valid_by_period.clear()
            run.book=PeriodBook(mission)
            for branch in run.branches.values():
                branch.update(packets=0,phy_pass=0,app_pass=0,phy_crc_pass=0,phy_crc_fail=0,trusted_packets=0,
                              suspect_packets=0)
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
    except InterruptedError as exc:
        if not stop.is_set():
            status,error="failed",str(exc)
            raise
        status="stopped"
    except Exception as exc:
        status, error = "failed", str(exc)
        raise
    finally:
        run.close(status,error)
    return run.path
