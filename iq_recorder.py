# Copyright (c) 2026 Mark Lohatepanont. MIT; see licenses/DeSCENT-MIT.txt.
# LoRa adaptation: exclusive creation and streaming SHA-256 of recorded bytes.
"""Raw IQ recording for live reception (field readiness, 2026-10-28).

Every byte the RTL-SDR delivers is written, unchanged, to an rtl_sdr-format file
(unsigned 8-bit interleaved I/Q at 1 MS/s), so a flight can be decoded again later
by any future receiver version. A JSON sidecar records the tuning and any gaps.

The receiver never waits for the disk (lessons 4 and 5 in ENGINEERING.md): write()
only enqueues; one background thread does all file I/O. If the queue fills (the disk
cannot keep up for longer than the queue covers), blocks are dropped, counted and
listed in the sidecar as gaps (file offset and length), and I/Q pairing is preserved
with a pad byte when a dropped length is odd. Recording stops cleanly, never the
receiver, on a write error or when free disk space falls below ``min_free_bytes``.
"""
from __future__ import annotations

import json
import hashlib
import os
import queue
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

QUEUE_BYTES = 64_000_000          # about 32 s of IQ at 2 MB/s
MIN_FREE_BYTES = 2_000_000_000    # stop recording below 2 GB free
FREE_CHECK_BYTES = 200_000_000    # check free space every 200 MB written


def default_recording_path(directory, freq_hz, gain_db, now=None):
    now = now or datetime.now().astimezone()
    name = f"lrfhss_live_{now:%Y%m%d_%H%M%S}_{freq_hz/1e6:.3f}MHz_g{gain_db:g}dB.iq"
    return Path(directory) / name


class IQRecorder:
    def __init__(self, path, *, metadata=None, queue_bytes=QUEUE_BYTES,
                 min_free_bytes=MIN_FREE_BYTES, free_check_bytes=FREE_CHECK_BYTES):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.sidecar = self.path.with_suffix(self.path.suffix + ".json")
        self.metadata = dict(metadata or {})
        self.queue_bytes, self.min_free_bytes, self.free_check_bytes = queue_bytes, min_free_bytes, free_check_bytes
        self._q = queue.Queue()
        self._queued = 0
        self._lock = threading.Lock()
        self.received = 0               # bytes offered by the receiver
        self.written = 0                # bytes in the file
        self.dropped = 0
        self.gaps = []                  # dict(file_offset, dropped_bytes, wall_utc)
        self._pad = 0                   # pending parity pad after an odd-length drop
        self._file_end = 0              # file length once every accepted block is written
        self.stopped_reason = None      # set when recording stops early
        self.error = None
        self.closed = False
        self.started_utc = datetime.now(timezone.utc).isoformat()
        self.digest = hashlib.sha256()
        self._file = open(self.path, "xb")
        self._write_sidecar()
        self._thread = threading.Thread(target=self._run, name="iq-recorder", daemon=True)
        self._thread.start()

    # ---- receiver side: never blocks ------------------------------------------------
    def write(self, raw) -> None:
        if self.closed or not raw:
            return
        n = len(raw)
        self.received += n
        if self.stopped_reason is not None:
            return
        with self._lock:
            if self._queued + n > self.queue_bytes:
                self.dropped += n
                self._pad ^= n & 1
                self.gaps.append(dict(file_offset=self._file_end, dropped_bytes=n,
                                      wall_utc=datetime.now(timezone.utc).isoformat()))
                return
            # The pad (if any) travels with the first block accepted after the drop.
            pad, self._pad = self._pad, 0
            self._queued += n
            self._file_end += pad + n
            self._q.put((pad, bytes(raw)))

    # ---- writer thread --------------------------------------------------------------
    def _run(self):
        since_check = 0
        while True:
            item = self._q.get()
            if item is None:
                break
            pad, block = item
            with self._lock:
                self._queued -= len(block)
            if self.stopped_reason is not None:
                continue
            try:
                if pad:
                    self._file.write(b"\x7f")         # keep I/Q pairs aligned after an odd drop
                    self.digest.update(b"\x7f")
                    self.written += 1
                self._file.write(block)
                self.digest.update(block)
                self.written += len(block)
                since_check += len(block)
                if since_check >= self.free_check_bytes:
                    since_check = 0
                    self._file.flush()
                    if shutil.disk_usage(self.path.parent).free < self.min_free_bytes:
                        self.stopped_reason = "low_disk_space"
            except OSError as exc:
                self.error = repr(exc)
                self.stopped_reason = "write_error"

    def _write_sidecar(self):
        doc = dict(format="rtl_sdr unsigned 8-bit interleaved I/Q", sample_rate_hz=1_000_000,
                   file=self.path.name, started_utc=self.started_utc,
                   finished_utc=datetime.now(timezone.utc).isoformat() if self.closed else None,
                   bytes_received=self.received, bytes_written=self.written, bytes_dropped=self.dropped,
                   sha256=self.digest.hexdigest() if self.closed and not self.error else None,
                   gaps=self.gaps, stopped_reason=self.stopped_reason, error=self.error, **self.metadata)
        tmp = self.sidecar.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=2) + "\n")
        os.replace(tmp, self.sidecar)

    def close(self, timeout=30.) -> dict:
        """Flush the queue (bounded), close the file and finalize the sidecar."""
        if self.closed:
            return self.stats()
        self.closed = True
        self._q.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            self.stopped_reason = self.stopped_reason or "close_timeout"
        else:
            try:
                self._file.close()
            except OSError as exc:
                self.error = self.error or repr(exc)
        self._write_sidecar()
        return self.stats()

    def stats(self) -> dict:
        return dict(path=str(self.path), bytes_written=self.written, bytes_dropped=self.dropped,
                    sha256=self.digest.hexdigest() if self.closed and not self.error else None,
                    gaps=len(self.gaps), queued_bytes=self._queued, stopped_reason=self.stopped_reason,
                    error=self.error)
