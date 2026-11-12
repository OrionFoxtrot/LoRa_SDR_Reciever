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
import numpy as np
from iq_recorder import IQRecorder

BLOCK_SAMPLES = 131072


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


def clipped(raw):
    a = np.frombuffer(raw, np.uint8)
    rails = (a == 0) | (a == 255)
    return int(np.count_nonzero(rails[::2] | rails[1::2]))


def find_rtl():
    paths = [os.environ.get("RTL_TCP_EXE"), shutil.which("rtl_tcp"),
             Path(os.environ.get("ProgramData", "C:/ProgramData"))/"radioconda/Library/bin/rtl_tcp.exe",
             Path.home()/"radioconda/Library/bin/rtl_tcp.exe"]
    return next((str(p) for p in paths if p and Path(p).is_file()), None)


class StreamEnded(OSError):
    def __init__(self,message,partial):
        super().__init__(message)
        self.partial = bytes(partial)


def read_exact(sock, length, stop):
    buf = bytearray()
    deadline = time.monotonic()+5
    while len(buf) < length and not stop.is_set():
        try:
            part = sock.recv(length-len(buf))
        except socket.timeout:
            if time.monotonic() >= deadline:
                raise StreamEnded("rtl_tcp stopped supplying IQ",buf)
            continue
        except OSError as exc:
            raise StreamEnded(str(exc),buf) from exc
        if not part:
            raise StreamEnded("rtl_tcp closed the stream",buf)
        buf.extend(part)
        deadline = time.monotonic()+5
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
            self.log = open(Path(directory)/"rtl_tcp.log", "wb")
            self.process = subprocess.Popen(
                [exe, "-a", addr[0], "-p", str(addr[1]), "-d", str(self.device),
                 "-s", str(self.p.sample_rate_hz), "-f", str(self.p.center_hz),
                 "-g", str(self.gain), "-P", str(self.ppm)],
                stdout=self.log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic()+8
        while not stop.is_set():
            if self.process and self.process.poll() is not None:
                raise RuntimeError("rtl_tcp exited during startup. See rtl_tcp.log; check USB, driver and device ownership.")
            try:
                self.sock = socket.create_connection(addr, timeout=.5)
                break
            except OSError:
                if time.monotonic() >= deadline:
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


class LiveInput:
    """One socket reader. The recorder receives original bytes before the DSP queue."""
    def __init__(self, radio, directory, stop, seconds, record=True, block_samples=BLOCK_SAMPLES,
                 synthetic=False):
        self.radio, self.stop, self.seconds = radio, stop, seconds
        self.block = block_samples
        self.queue = queue.Queue(maxsize=16)
        self.produced = self.dropped = self.highwater = self.odd_bytes = 0
        self.dropped_blocks = 0
        self.finished = threading.Event()
        self.error = None
        self.recorder = None
        self.record_stats = None
        if record:
            self.recorder = IQRecorder(Path(directory)/"capture.iq",
                metadata=dict(center_hz=radio.p.center_hz, gain_db=radio.gain, ppm=radio.ppm,
                              source="rtl_tcp", synthetic=synthetic, receiver_profile=radio.p.report()))
        self.thread = threading.Thread(target=self._produce, name="rtl-reader", daemon=True)

    def _produce(self):
        try:
            limit = None if self.seconds is None else round(self.seconds*self.radio.p.sample_rate_hz)
            while not self.stop.is_set() and (limit is None or self.produced < limit):
                want = self.block if limit is None else min(self.block, limit-self.produced)
                try:
                    raw = read_exact(self.radio.sock, want*2, self.stop)
                except StreamEnded as exc:
                    raw, self.error = exc.partial, exc
                if len(raw) & 1:
                    self.odd_bytes += 1
                    raw = raw[:-1]
                if not raw:
                    break
                start = self.produced
                self.produced += len(raw)//2
                if self.recorder:
                    self.recorder.write(raw)
                try:
                    self.queue.put_nowait((start, raw))
                    self.highwater = max(self.highwater, self.queue.qsize())
                except queue.Full:
                    self.dropped += len(raw)//2
                    self.dropped_blocks += 1
                if self.error:
                    break
        except Exception as exc:
            self.error = exc
        finally:
            self.finished.set()

    def blocks(self):
        self.thread.start()
        try:
            while not self.finished.is_set() or not self.queue.empty():
                try:
                    yield self.queue.get(timeout=.12)
                except queue.Empty:
                    continue
            if self.error:
                raise self.error
        finally:
            self.stop.set()
            self.thread.join()
            if self.recorder:
                # The run can retire only after its final IQ and sidecar are durable.
                self.record_stats = self.recorder.close(timeout=None)
