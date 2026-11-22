from pathlib import Path
import tempfile
import unittest

from tools.splitter_bench import compare, wilson


class SplitterTests(unittest.TestCase):
    def test_join_rejects_duplicates_and_counts_neither(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tx,sdr,beam = [root/name for name in ("tx.csv","packets.csv","beam.log")]
            tx.write_text("attenuation_db,payload_hex\n90,01\n90,02\n90,03\n90,04\n91,05\n")
            sdr.write_text("phy_crc_ok,payload_hex\nTrue,01\nTrue,01\nTrue,02\nFalse,03\nTrue,ff\n")
            beam.write_text("# Boot\nRX phy_crc_ok=1 payload_hex=01\nRX phy_crc_ok=true payload_hex=03\n")
            report, joined = compare(tx,sdr,beam)
            self.assertEqual([row["outcome"] for row in joined],["both_heard","sdr_only","tbeam_only","neither_heard","neither_heard"])
            self.assertEqual(report["sdr_log"], dict(duplicates=1,unmatched_valid=1,crc_failed_or_unverified=1))
            self.assertEqual(report["steps"][1]["neither_heard"]["count"],1)
            self.assertAlmostEqual(wilson(0,4)["wilson95"][1],.48989,places=4)
            self.assertAlmostEqual(wilson(4,4)["wilson95"][0],.51011,places=4)

    def test_clock_matching_for_reused_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tx,sdr,beam = [root/name for name in ("tx.csv","packets.csv","beam.log")]
            tx.write_text("attenuation_db,payload_hex,start_s\n10,ab,1\n11,ab,3\n")
            sdr.write_text("phy_crc_ok,payload_hex,capture_s\nTrue,ab,4.01\n")
            beam.write_text('{"phy_crc_ok":true,"payload_hex":"ab","rx_start_s":1.5}\n')
            with self.assertRaisesRegex(ValueError,"Ambiguous"):
                compare(tx,sdr,beam)
            _, joined = compare(tx,sdr,beam,sdr_offset=1.,tbeam_offset=.5)
            self.assertEqual([r["outcome"] for r in joined],["tbeam_only","sdr_only"])
