import csv
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import tkinter as tk
from tkinter import ttk

from config import ROOT
from fixtures import NINE_BRANCH_PROFILE
from front_end import FrontEndPreferences
from sources import Layout, LiveInput, StreamEnded, read_exact
from types import SimpleNamespace
from config import read_profile


class OperatorTests(unittest.TestCase):
    def test_terminal_connection_gap_preserves_empty_iq_timeline(self):
        skin=SimpleNamespace(p=read_profile(),gain=10.,ppm=0)
        with tempfile.TemporaryDirectory() as tmp:
            source=LiveInput(skin,tmp,threading.Event(),seconds=.05,initial_error=ConnectionRefusedError())
            self.assertEqual(list(source.blocks()),[])
            layout=Layout.read(Path(tmp)/"capture.iq")
            self.assertEqual((layout.samples,layout.unavailable),(50_000,50_000))
            self.assertEqual(layout.gap_ranges(1_000_000),[(0,.05)])
            gap,=layout.metadata["source_dropouts"]
            self.assertEqual((gap["reason"],gap["resumed"],gap["reconnect_attempts"]),("socket_error",False,0))

    def test_stall_timeout_keeps_only_received_bytes(self):
        import socket
        from unittest.mock import Mock
        conn=Mock()
        conn.recv.side_effect=[b"abc",socket.timeout(),socket.timeout()]
        with patch("sources.time.monotonic",side_effect=[0.,.5,3.49,3.5]):
            with self.assertRaises(StreamEnded) as caught:
                read_exact(conn,16,threading.Event())
        self.assertEqual((caught.exception.reason,caught.exception.partial),("iq_timeout",b"abc"))

    def test_queue_overrun_keeps_original_recording(self):
        skin=SimpleNamespace(p=read_profile(),gain=10.,ppm=0,sock=object())
        with tempfile.TemporaryDirectory() as tmp:
            source=LiveInput(skin,tmp,threading.Event(),seconds=.024,block_samples=1000)
            raw=bytes([5,250])*1000
            with patch("sources.read_exact",return_value=raw):
                source.thread.start()
                source.thread.join(5)
            self.assertFalse(source.thread.is_alive())
            stats=source.recorder.close()
            self.assertEqual((source.produced,source.dropped),(24000,8000))
            self.assertEqual(source.dropped_blocks,8)
            self.assertEqual(stats["bytes_dropped"],0)
            self.assertEqual(hashlib.sha256((raw*24)).hexdigest(),stats["sha256"])

    def test_gui_replay_button(self):
        summary=self.gui_replay()
        self.assertEqual(summary["packets"],5)
        self.assertEqual(len(summary["branches"]),9)

    def test_gui_monitor_selection(self):
        def select(widgets):
            for w in widgets:
                if isinstance(w,ttk.Checkbutton) and w.cget("text") in ("914.875","915.125"):
                    w.invoke()
                if w.winfo_name()=="additional":
                    w.insert(0,"914.875125")
        summary=self.gui_replay(select)
        self.assertEqual(summary["profile"]["carriers_hz"],[915000000,914875125])
        self.assertEqual(len(summary["branches"]),6)
        self.assertEqual(summary["packets"],4)

    def gui_replay(self,select=None):
        from operator_gui import open_gui
        with tempfile.TemporaryDirectory() as tmp:
            real_tk=tk.Tk
            finished=[]
            def window():
                root=real_tk()
                root.withdraw()
                def dispose():
                    for timer in root.tk.call("after","info"):
                        root.after_cancel(timer)
                    root.destroy()
                def controls(widget):
                    yield widget
                    for child in widget.winfo_children():
                        yield from controls(child)
                def start():
                    widgets=list(controls(root))
                    if select:
                        select(widgets)
                    button=next(w for w in widgets if isinstance(w,ttk.Button)
                                and w.cget("text")=="Start receiver")
                    button.invoke()
                def inspect():
                    if any(w.cget("text")=="Run saved" for w in controls(root) if isinstance(w,ttk.Label)):
                        finished.append(True)
                        dispose()
                    else:
                        root.after(100,inspect)
                root.after(30,start)
                root.after(150,inspect)
                root.after(20000,dispose)
                return root
            with patch("operator_gui.ROOT",Path(tmp)),patch("operator_gui.tk.Tk",window), \
                    patch("receiver_setup.DEFAULT_PROFILE",NINE_BRANCH_PROFILE), \
                    patch("receiver_setup.FrontEndPreferences",return_value=FrontEndPreferences(Path(tmp)/"settings.db")):
                open_gui("Replay",str(ROOT/"examples/epfl_bench.iq"))
            self.assertTrue(finished,"GUI did not finish replay")
            summary=next(Path(tmp).glob("runs/*/summary.json"))
            return json.loads(summary.read_text())
