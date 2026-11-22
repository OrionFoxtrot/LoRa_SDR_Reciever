"""RTL-SDR U8 source and reference-compatible capture timeline."""
import json
import os
from pathlib import Path
import queue
import shutil
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import numpy as np
from iq_recorder import IQRecorder

BLOCK_SAMPLES = 131072
IQ_TIMEOUT_S = 3.
RECONNECT_DELAYS = (1., 2., 5., 10.)


@dataclass
class Layout:
    spans: list
    samples: int
    unavailable: int
    metadata: dict

    @classmethod
    def read(cls, path):
        path = Path(path)
        size = path.stat().st_size
        sidecar = Path(str(path)+".json")
        meta = json.loads(sidecar.read_text(encoding="utf-8-sig")) if sidecar.is_file() else {}
        if meta.get("sample_rate_hz", 1000000) != 1000000:
            raise ValueError("Expected 1 MS/s in the IQ sidecar.")
        if "format" in meta and meta["format"] not in ("rtl_sdr unsigned 8-bit interleaved I/Q", "cu8", "u8"):
            raise ValueError(f"Unsupported IQ format: {meta['format']}")
        if meta.get("finished_utc") and meta.get("bytes_written", size) != size:
            raise ValueError("IQ size disagrees with the finalized sidecar.")
        gaps = {}
        for item in meta.get("gaps", []):
            offset, missing = item["file_offset"], item["dropped_bytes"]
            if not isinstance(offset, int) or not isinstance(missing, int) or not 0 <= offset <= size or missing < 1:
                raise ValueError("Invalid gap in recording sidecar")
            gaps[offset] = gaps.get(offset, 0) + missing
        if meta.get("bytes_dropped", sum(gaps.values())) != sum(gaps.values()):
            raise ValueError("Dropped byte count disagrees with sidecar gap list")
        file_pos = logical = 0
        spans = []
        for offset in sorted(set(gaps) | {size}):
            if offset < file_pos:
                raise ValueError("Overlapping recorder gap/padding offsets")
            length = offset-file_pos
            skip_q = logical & 1
            count = max(0, (length-skip_q)//2)
            if count:
                spans.append((file_pos+skip_q, (logical+skip_q)//2, count))
            logical += length + gaps.get(offset, 0)
            # The LR-FHSS recorder inserts one 0x7f byte after an odd net
            # drop, only if it accepts more data. It is never an RF sample.
            file_pos = offset + ((gaps.get(offset, 0) & 1) if offset < size else 0)
        total = max(logical, int(meta.get("bytes_received", 0)))//2
        return cls(spans, total, total-sum(s[2] for s in spans), meta)

    def gap_ranges(self,fs):
        first=0
        gaps=[]
        for _,start,count in self.spans:
            if start>first:
                gaps.append((first/fs,start/fs))
            first=start+count
        if first<self.samples:
            gaps.append((first/fs,self.samples/fs))
        return gaps

    def recorded_profile(self, path):
        from config import profile_from_settings
        settings = self.metadata.get("receiver_profile")
        path = Path(path)
        if not settings and path.name.lower()=="capture.iq" and (path.parent/"summary.json").is_file():
            settings = json.loads((path.parent/"summary.json").read_text(encoding="utf-8-sig")).get("profile")
        return profile_from_settings(settings) if settings else None

    def blocks(self, path, block_samples=BLOCK_SAMPLES):
        with open(path, "rb") as f:
            for offset, start, count in self.spans:
                f.seek(offset)
                while count:
                    n = min(count, block_samples)
                    raw = f.read(2*n)
                    if len(raw) != 2*n:
                        raise IOError("IQ file changed during replay")
                    yield start, raw
                    start += n
                    count -= n


def complex_iq(raw):
    v = np.frombuffer(raw, np.uint8).astype(np.float32)
    v -= 127.5
    v *= 1/128
    return v.view(np.complex64)


def rail_indices(raw):
    """Offsets of CU8 sample pairs with at least one ADC component on a rail."""
    a = np.frombuffer(raw, np.uint8)
    rails = (a == 0) | (a == 255)
    return np.flatnonzero(rails[::2] | rails[1::2])


def clipped(raw):
    return len(rail_indices(raw))


def find_rtl():
    paths = [os.environ.get("RTL_TCP_EXE"), shutil.which("rtl_tcp"),
             Path(os.environ.get("ProgramData", "C:/ProgramData"))/"radioconda/Library/bin/rtl_tcp.exe",
             Path.home()/"radioconda/Library/bin/rtl_tcp.exe"]
    return next((str(p) for p in paths if p and Path(p).is_file()), None)


class StreamEnded(OSError):
    def __init__(self,message,partial,reason="stream_closed",last_byte=None):
        super().__init__(message)
        self.partial = bytes(partial)
        self.reason, self.last_byte = reason,last_byte


def read_exact(sock, length, stop):
    buf = bytearray()
    last_byte = None
    deadline = time.monotonic()+IQ_TIMEOUT_S
    while len(buf) < length and not stop.is_set():
        try:
            part = sock.recv(length-len(buf))
        except socket.timeout:
            if time.monotonic() >= deadline:
                raise StreamEnded("rtl_tcp supplied no IQ for 3 s",buf,"iq_timeout",last_byte)
            continue
        except OSError as exc:
            raise StreamEnded(str(exc),buf,"socket_error",last_byte) from exc
        if not part:
            raise StreamEnded("rtl_tcp closed the stream",buf,"stream_closed",last_byte)
        buf.extend(part)
        last_byte = time.monotonic()
        deadline = last_byte+IQ_TIMEOUT_S
    return bytes(buf)


class Radio:
    def __init__(self, profile, gain, ppm, connect=None, device=0):
        self.p, self.gain, self.ppm = profile, gain, ppm
        self.connect, self.device = connect, device
        self.process = self.sock = self.log = None

    def open(self, directory, stop):
        if self.connect:
            host, ps = self.connect.rsplit(":", 1)
            addr = (host, int(ps))
        else:
            exe = find_rtl()
            if not exe:
                raise FileNotFoundError("rtl_tcp.exe missing. Set RTL_TCP_EXE or install radioconda.")
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                addr = probe.getsockname()
            self.log = open(Path(directory)/"rtl_tcp.log", "ab")
            self.process = subprocess.Popen(
                [exe, "-a", addr[0], "-p", str(addr[1]), "-d", str(self.device),
                 "-s", str(self.p.sample_rate_hz), "-f", str(self.p.center_hz),
                 "-g", str(self.gain), "-P", str(self.ppm)],
                stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic()+8
        while not stop.is_set():
            if self.process and self.process.poll() is not None:
                raise OSError("rtl_tcp exited during startup. See rtl_tcp.log; check USB, driver and device ownership.")
            try:
                self.sock = socket.create_connection(addr, timeout=.5)
                break
            except OSError:
                if self.connect or time.monotonic() >= deadline:
                    raise
                stop.wait(.12)
        if self.sock is None:
            raise InterruptedError("Stopped before connection")
        self.sock.settimeout(.5)
        self.header = read_exact(self.sock, 12, stop)
        if stop.is_set():
            raise InterruptedError("Stopped during radio setup")
        if len(self.header) != 12 or self.header[:4] != b"RTL0":
            raise ValueError("Invalid rtl_tcp RTL0 handshake")
        self.command(2, self.p.sample_rate_hz)
        self.command(1, self.p.center_hz)
        self.command(5, self.ppm)
        self.command(8, 0)  # disable RTL2832 digital AGC
        self.command(3, 1)  # manual tuner gain, including 0 dB
        self.command(4, round(self.gain*10))

    def command(self, key, value):
        if key == 0x0e and value:
            raise ValueError("Antenna-port power is disabled for this receiver.")
        self.sock.sendall(struct.pack("!BI", key, value & 0xffffffff))

    def close(self):
        if self.sock:
            self.sock.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        if self.log:
            self.log.close()
        self.sock = self.process = self.log = None


class LiveInput:
    """One socket reader. The recorder receives original bytes before the DSP queue."""
    def __init__(self, radio, directory, stop, seconds, record=True, block_samples=BLOCK_SAMPLES,
                 synthetic=False,mission=None,front_end=None,origin=None,prepare=None,initial_error=None,log=None):
        self.radio, self.stop, self.seconds = radio, stop, seconds
        self.block = block_samples
        self.queue = queue.Queue(maxsize=16)
        self.produced = self.dropped = self.highwater = self.odd_bytes = 0
        self.dropped_blocks = 0
        self.last_iq_wall = None
        self.heartbeat = None
        self.finished = threading.Event()
        self.error = None
        self.fs = radio.p.sample_rate_hz
        self.origin_mono, self.origin_utc = origin or (time.monotonic(),datetime.now(timezone.utc))
        def reopen():
            radio.open(directory,stop)
            read_exact(radio.sock,2*round(.25*self.fs),stop)
        self.prepare, self.log, self.idle = prepare or reopen,log,None
        self.initial_error = initial_error
        self.dropouts = []
        self.active_gap = None
        self.recorder = None
        self.record_stats = None
        if record:
            self.recorder = IQRecorder(Path(directory)/"capture.iq",started_utc=self.origin_utc.isoformat(),
                metadata=dict(center_hz=radio.p.center_hz, gain_db=radio.gain, ppm=radio.ppm,
                              source="rtl_tcp", synthetic=synthetic, receiver_profile=radio.p.report(),
                              mission=mission or {}, front_end=front_end, bias_tee_enable_requested=False))
        self.thread = threading.Thread(target=self._produce, name="rtl-reader", daemon=True)

    def wall(self, sample):
        return (self.origin_utc+timedelta(seconds=sample/self.fs)).isoformat()

    def begin_gap(self, exc):
        self.active_gap = dict(start_sample=self.produced,wall_start_utc=self.wall(self.produced),
                               reason=getattr(exc,"reason","socket_error"),detail=str(exc),reconnect_attempts=0)
        if self.log:
            self.log(f"Reconnecting… dropout {len(self.dropouts)+1}: {exc}")

    def end_gap(self, when, limit, resumed):
        gap = self.active_gap
        first = gap["start_sample"]
        end = max(first+1,round((when-self.origin_mono)*self.fs))
        if limit is not None:
            end = min(limit,end)
        gap = dict(gap,end_sample=end,wall_end_utc=self.wall(end),duration_s=(end-first)/self.fs,
                   resumed=resumed)
        if self.recorder:
            self.recorder.source_gap(end-first,gap)
        self.produced = end
        self.dropouts.append(gap)
        self.active_gap = None
        if self.log:
            self.log(f"Dropout {len(self.dropouts)} {'reconnected' if resumed else 'ended'} after "
                     f"{gap['duration_s']:.3f} s and {gap['reconnect_attempts']} attempt(s).")

    def reconnect(self, limit):
        attempts = self.active_gap["reconnect_attempts"]
        delay = RECONNECT_DELAYS[min(attempts,len(RECONNECT_DELAYS)-1)]
        remaining = float("inf") if limit is None else self.origin_mono+limit/self.fs-time.monotonic()
        if remaining <= 0 or self.stop.wait(min(delay,remaining)) or remaining <= delay:
            return False
        self.active_gap = dict(self.active_gap,reconnect_attempts=attempts+1)
        if self.log:
            self.log(f"Reconnecting… dropout {len(self.dropouts)+1}, attempt {attempts+1}.")
        self.prepare()
        return True

    def _produce(self):
        limit = None if self.seconds is None else round(self.seconds*self.fs)
        try:
            if self.initial_error is not None:
                self.begin_gap(self.initial_error)
            while not self.stop.is_set() and (limit is None or self.produced < limit):
                want = self.block if limit is None else min(self.block, limit-self.produced)
                if self.active_gap:
                    try:
                        if not self.reconnect(limit):
                            break
                    except (OSError,ValueError) as exc:
                        # Handshake and settling bytes belong to radio setup.
                        self.active_gap = dict(self.active_gap,last_attempt_error=str(exc))
                        self.radio.close()
                        continue
                failure = None
                try:
                    raw = read_exact(self.radio.sock, want*2, self.stop)
                    arrived = time.monotonic()
                except StreamEnded as exc:
                    raw, failure, arrived = exc.partial, exc, exc.last_byte or time.monotonic()
                except InterruptedError:
                    if self.stop.is_set():
                        break
                    raise
                except OSError as exc:
                    raw, failure, arrived = b"", exc, time.monotonic()
                if len(raw) & 1:
                    self.odd_bytes += 1
                    raw = raw[:-1]
                if raw and self.active_gap:
                    self.end_gap(arrived-len(raw)/(2*self.fs),limit,True)
                    if limit is not None:
                        raw = raw[:2*(limit-self.produced)]
                if raw:
                    self.publish(raw,arrived)
                if failure is not None:
                    if not self.active_gap:
                        self.begin_gap(failure)
                    self.radio.close()
                elif not raw:
                    break
        except Exception as exc:
            self.error = exc
        finally:
            if self.active_gap:
                self.end_gap(time.monotonic(),limit,False)
            self.finished.set()

    def publish(self, raw, arrived):
        start = self.produced
        self.last_iq_wall = arrived
        if self.heartbeat:
            self.heartbeat(arrived)
        self.produced += len(raw)//2
        if self.recorder:
            self.recorder.metadata["gain_db"] = self.radio.gain
            self.recorder.write(raw)
        try:
            self.queue.put_nowait((start, raw))
            self.highwater = max(self.highwater, self.queue.qsize())
        except queue.Full:
            self.dropped += len(raw)//2
            self.dropped_blocks += 1

    def blocks(self):
        self.thread.start()
        try:
            while not self.finished.is_set() or not self.queue.empty():
                try:
                    yield self.queue.get(timeout=.12)
                except queue.Empty:
                    if self.idle:
                        self.idle()
            if self.error:
                raise self.error
        finally:
            self.stop.set()
            self.thread.join()
            if self.recorder:
                self.recorder.metadata["source_dropouts"] = self.dropouts
                # The run can retire only after its final IQ and sidecar are durable.
                self.record_stats = self.recorder.close(timeout=None)
