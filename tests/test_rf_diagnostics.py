from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np

from config import EventIQLimits, read_profile
from rf_diagnostics import EventIQ, SpectrumMonitor
from sources import Layout, Radio
from gain import run_autoset
from config import front_end_settings
from front_end import FrontEndPreferences, gain_reminder, preset


class DiagnosticTests(unittest.TestCase):
    def test_fixed_gain_compares_with_a_pre_upgrade_live_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prefs = FrontEndPreferences(root/"settings.sqlite3")
            for i,(evidence,front) in enumerate((("live hardware","none"),("synthetic loopback","lana"))):
                folder = root/str(i)
                folder.mkdir()
                (folder/"summary.json").write_text(json.dumps(dict(mode="live",evidence=evidence,
                    processed_samples=12,front_end=preset(front),started_utc=f"2026-11-{12+i:02d}T00:00:00+00:00")))
            self.assertEqual(prefs.last_run(root),preset("none"))
            self.assertIn("Auto Set",gain_reminder(prefs.last_run(root),preset("lana"),False))
            self.assertIsNone(gain_reminder(prefs.last_run(root),preset("lana"),True))

    def test_external_lna_metadata_and_bias_tee_guard(self):
        front = front_end_settings(usb_supply="battery bank")
        self.assertEqual((front["lna_model"],front["nominal_gain_db"],front["power_source"]),
                         ("Nooelec LaNA",20.,"USB"))
        self.assertEqual(front_end_settings("None")["nominal_gain_db"],0.)
        with self.assertRaises(ValueError):
            Radio(read_profile(),10,0).command(0x0e,1)

    def test_autoset_logs_safe_minimum(self):
        messages = []
        raw = bytes([120,120,136,136])*3000
        scan = run_autoset((0.,10.), 0., set_gain=lambda _:None, capture=lambda _:raw, progress=messages.append)
        self.assertEqual(scan["gain_db"],0.)
        self.assertIn("minimum tuner gain",scan["warning"])
        self.assertIn("Auto Set selected 0 dB.", messages)

    def test_clip_bounds_cooldown_and_gap(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = EventIQ(tmp, read_profile())
            # Use a short sample coordinate system for this ring-only test.
            event.before, event.after = 12, 8
            raw = bytes(range(80))
            event.feed(0, raw[:40])
            event.trigger("rail", 16)
            event.trigger("rail", 17)
            event.flush()
            event.feed(20, raw[40:60])
            event.flush()
            event.trigger("header_reject", 28, sf=11)
            event.feed(50, raw[60:])
            event.trigger("rail", 51)
            info = event.close()
            self.assertEqual(len(info["files"]), 2)
            self.assertEqual(info["skipped_by_reason"], {"cooldown":2})
            for i,(begin,end) in enumerate(((4,24),(16,30))):
                path=Path(tmp)/info["files"][i]
                layout=Layout.read(path)
                self.assertEqual((layout.metadata["source_start_sample"],layout.metadata["source_end_sample"]),(begin,end))
                self.assertEqual(path.read_bytes(),raw[begin*2:end*2])

    def test_rolling_budget_in_both_capture_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = replace(read_profile(),sample_rate_hz=10)
            event = EventIQ(tmp,p,EventIQLimits(2,1))
            for second in (100,3650,2000,3700,3710):
                sample = second*p.sample_rate_hz
                event.feed(sample-20,bytes(60))
                event.trigger("rail",sample)
                event.flush()
                event.collect(wait=True)
            info = event.close()
            self.assertEqual(len(info["files"]),3)
            self.assertEqual(info["skipped_by_reason"],{"hourly_limit":1,"cooldown":1})
            self.assertEqual(info["saved_bytes"], sum(p.stat().st_size for p in (Path(tmp)/"events").iterdir()))

    def test_failed_captures_release_quota_and_preserve_existing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = EventIQ(tmp,read_profile(),EventIQLimits(1,1))
            event.feed(0,bytes(1_000_000))
            event.trigger("rail",499_999)
            event.flush(partial=True)
            event.collect(wait=True)
            event.feed(2_000_000,bytes(800))
            def attempt(sample):
                event.trigger("rail",sample)
                event.flush(partial=True)
                event.collect(wait=True)
            with patch("rf_diagnostics.shutil.disk_usage") as disk:
                disk.return_value.free = 0
                attempt(2_000_100)
            folder = Path(tmp)/"events"
            old = folder/"rail_2000100.iq.json"
            old.write_bytes(b"previous sidecar")
            attempt(2_000_100)
            self.assertEqual(old.read_bytes(),b"previous sidecar")
            self.assertFalse((folder/"rail_2000100.iq").exists())
            attempt(2_000_200)
            info = event.close()
            self.assertEqual(len(info["files"]),1)
            self.assertEqual(info["skipped_by_reason"],{"size_cap":1,"low_disk":1,"file_exists":1})

    def test_noise_step_with_zero_rails(self):
        p = replace(read_profile(), carriers_hz=(915000000,))
        rng = np.random.default_rng(543)
        with tempfile.TemporaryDirectory() as tmp:
            rf = SpectrumMonitor(tmp, p)
            try:
                # Fragment FFT windows across caller block boundaries.
                start = 0
                for scale in [.015]*5 + [.045]*20:
                    iq = rng.standard_normal(1_000_000, dtype=np.float32).view(np.complex64)*scale
                    for pos in range(0,len(iq),15_677):
                        rf.feed(start+pos,iq[pos:pos+15_677])
                    start += len(iq)
                    if start == 4_500_000:
                        early = rf.snapshot()
                        self.assertTrue(early["desense"]["raised"])
                        first_floor = early["floors"]["915000000"]
                        self.assertAlmostEqual(first_floor["dbfs"], first_floor["reference_dbfs"], delta=.8)
                snap=rf.snapshot()
                floor=snap["floors"]["915000000"]
                self.assertTrue(floor["raised"])
                self.assertAlmostEqual(floor["dbfs"]-floor["reference_dbfs"], 9.54, delta=.8)
                self.assertAlmostEqual(floor["reference_dbfs"],10*np.log10(2*.015**2*.125),delta=.8)
                self.assertEqual(len(snap["waterfall"]),25)
                self.assertEqual(snap["desense"]["rises"],1)
                json.dumps(snap,allow_nan=False)
            finally:
                rf.file.close()

    def test_mask_fallback_and_gain_change(self):
        p = read_profile()
        rng = np.random.default_rng(1205)
        with tempfile.TemporaryDirectory() as tmp:
            p = replace(p, carriers_hz=tuple(range(p.center_hz-350_000,p.center_hz+350_001,100_000)))
            rf = SpectrumMonitor(tmp,p)
            with rf.file:
                rf.set_gain(10.)
                for i in range(8):
                    iq = rng.standard_normal(1_000_000,dtype=np.float32).view(np.complex64) * (.015 if i<4 else .045)
                    if i == 4:
                        rf.set_gain(20.)
                    rf.feed(i*len(iq),iq)
                alarm = rf.snapshot()["desense"]
                self.assertEqual((alarm["source"],alarm["guard_bins"]),("in_channel_fallback",0))
                self.assertEqual((alarm["gain_db"],alarm["gain_changes"],alarm["rises"]),(20.,1,0))
                self.assertAlmostEqual(alarm["reference_dbfs"],10*np.log10(2*.045**2*.125),delta=.8)
                for i in range(8,28):
                    iq = rng.standard_normal(1_000_000,dtype=np.float32).view(np.complex64)*.09
                    rf.feed(i*len(iq),iq)
                self.assertTrue(rf.snapshot()["desense"]["raised"])
                self.assertEqual(rf.snapshot()["desense"]["rises"],1)

    def test_clipped_startup_cannot_depress_the_guard_reference(self):
        p = read_profile()
        rng = np.random.default_rng(1741)
        warnings = []
        with tempfile.TemporaryDirectory() as tmp:
            rf = SpectrumMonitor(tmp,p,warnings.append)
            with rf.file:
                for i in range(24):
                    noise = rng.standard_normal(1_000_000,dtype=np.float32)*(.1 if i<14 else .14125)
                    data = np.clip(np.rint(noise*128+127.5),0,255).astype(np.uint8)
                    if i < 6:
                        data[::2] = 255
                    iq = ((data.astype(np.float32)-127.5)/128).view(np.complex64)
                    rf.feed(i*len(iq),iq)
                    if i == 5:
                        self.assertFalse(rf.alarm["reference_ready"])
                self.assertTrue(rf.alarm["reference_ready"])
                self.assertEqual(warnings,[])
