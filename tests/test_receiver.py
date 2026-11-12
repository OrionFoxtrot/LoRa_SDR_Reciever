import binascii
import copy
import csv
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import numpy as np
from scipy.signal import resample

from config import read_profile
from fixtures import Event,GOLD,generate,payload
from gain import supported_gains, run_autoset
from phy import waveform
from receiver import Receiver
from reports import periods, score, write_json, write_packets, telemetry
from run import replay
from sic import Cancellation
from sources import Layout, clipped
from telemetry import decode_descent_packet
from tx_settings import check


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.p=read_profile()
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir=Path(self.temp.name)

    def test_golden_telemetry(self):
        for i,data in enumerate(GOLD,1):
            d=decode_descent_packet(data)
            self.assertEqual((d["csid"],d["packet_counter"]),(i,0))
            self.assertEqual(d["application_crc16"],int.from_bytes(data[-2:],"little"))
        self.assertAlmostEqual(decode_descent_packet(GOLD[0])["latitude_deg"],42.4351455)
        with self.assertRaises(ValueError):
            decode_descent_packet(bytes(55))

    def test_occupied_band_and_ldro(self):
        self.assertEqual(self.p.carriers_hz,(914875000,915000000,915125000))
        self.assertEqual(self.p.fleet_carriers_hz,(914875000,915125000))
        self.assertEqual([self.p.de(sf) for sf in (10,11,12)],[False,True,True])
        with self.assertRaises(ValueError):
            self.p.tuning(915400000)
        with self.assertRaises(ValueError):
            replace(self.p,bandwidth_hz=112000).checked()

    def test_odd_recorder_gaps(self):
        # At file byte 3 an I has been received. Two successive drops total 3
        # bytes; the recorder inserts a parity pad before source byte 6.
        path=self.dir/"odd.iq"
        path.write_bytes(bytes([0,1,2,127,6,7,8,9]))
        write_json(str(path)+".json",{"gaps":[{"file_offset":3,"dropped_bytes":1},
                                             {"file_offset":3,"dropped_bytes":2},
                                             {"file_offset":8,"dropped_bytes":4}]})
        layout=Layout.read(path)
        self.assertEqual(list(layout.blocks(path,20)),[(0,bytes([0,1])),(3,bytes([6,7,8,9]))])
        self.assertEqual((layout.samples,layout.unavailable),(7,4))

    def test_sidecar_integrity(self):
        path=self.dir/"bad.iq"
        path.write_bytes(bytes(100))
        for metadata in ({"sample_rate_hz":8000000},{"gaps":[{"file_offset":120,"dropped_bytes":4}]},
                         {"finished_utc":"now","bytes_written":102},{"format":"CF32"}):
            write_json(str(path)+".json",metadata)
            with self.assertRaises(ValueError):
                Layout.read(path)

    def test_gap_resets_burst_and_preserves_next_start(self):
        path=self.dir/"gap.iq"
        a=self.p.carriers_hz[0]
        events=[Event(.092345,a,10,GOLD[0]),Event(1.212347,a,10,GOLD[1])]
        generate(path,self.p,events,2.23)
        raw=path.read_bytes()
        cut=405721*2
        path.write_bytes(raw[:cut]+raw[cut+32006:])
        m=json.loads(Path(str(path)+".json").read_text())
        m.update(bytes_dropped=32006,bytes_written=len(raw)-32006,
                 gaps=[{"file_offset":cut,"dropped_bytes":32006}])
        write_json(str(path)+".json",m)
        r=replay(self.p,path,self.dir/"runs",block_samples=5017)
        with (r/"packets.csv").open() as f:
            rows=list(csv.DictReader(f))
        self.assertEqual([v["payload_hex"] for v in rows],[GOLD[1].hex()])
        self.assertLessEqual(abs(int(rows[0]["start_sample"])-1212347),2)
        self.assertEqual(json.loads((r/"summary.json").read_text())["dropped_samples"],16003)

    def demod(self,x,p=None):
        out=[]
        rx=Receiver(p or self.p,out.append)
        for i in range(0,len(x),63379):
            rx.feed(i,x[i:i+63379])
        rx.close_segment()
        return out

    def test_generic_crc_and_header_modes(self):
        for explicit,crc,bw,sf in [(True,False,250000,9),(False,True,125000,10)]:
            data=b"generic binary\x00\xffSDR"
            p=replace(self.p,carriers_hz=(915000000,),spreading_factors=(sf,),explicit_header=explicit,
                      bandwidth_hz=bw,implicit_length=len(data),coding_rate_denominator=6)
            x=waveform(p,sf,data,2,crc,1000000)
            out=self.demod(np.pad(x,(71237,15000)),p)
            self.assertEqual([r["payload_hex"] for r in out],[data.hex()])
            self.assertIs(out[0]["phy_crc_ok"],True if crc else None)
            self.assertIsNone(telemetry(out[0],"primary")["application_crc_ok"])

    def test_independent_crc_gates(self):
        p=replace(self.p,carriers_hz=(915000000,),spreading_factors=(10,))
        for bad_phy in (True,False):
            data=GOLD[0] if bad_phy else GOLD[0][:-1]+bytes([GOLD[0][-1]^1])
            if bad_phy:
                # Corrupt only the transmitter's embedded PHY CRC.
                with patch("lora_phy.tx.calc_crc",return_value=(0x13,0xa7)):
                    x=waveform(p,10,data,3,True,1000000)
            else:
                x=waveform(p,10,data,3,True,1000000)
            r=telemetry(self.demod(np.pad(x,(75313,12000)),p)[0],"primary")
            self.assertIs(r["phy_crc_ok"],not bad_phy)
            self.assertIs(r["application_crc_ok"],bad_phy)
            self.assertFalse(Cancellation(p,[r]).donors)

    def test_sync_word_and_noise_rejection(self):
        p=replace(self.p,carriers_hz=(915000000,),spreading_factors=(10,))
        x=waveform(replace(p,sync_word=0x34),10,GOLD[0],3,True,1000000)
        rng=np.random.default_rng(902)
        noise=rng.standard_normal(2000000,dtype=np.float32).view(np.complex64)
        self.assertEqual(self.demod(np.r_[noise,.23*x,noise],p),[])

    def test_motion_and_sample_clock(self):
        p=replace(self.p,carriers_hz=(915000000,))
        rng=np.random.default_rng(78)
        for sf in p.spreading_factors:
            for ppm in (-19.,24.):
                y=waveform(p,sf,GOLD[0],3,True,1000000)
                y=resample(y,round(len(y)*(1+ppm*1e-6)))
                t=np.arange(len(y))*1e-6
                y*=np.exp(2j*np.pi*t*(11511+.5*118*t))
                x=np.pad(y,(89311,70000))
                x+=.13*rng.standard_normal(len(x)*2,dtype=np.float32).view(np.complex64)
                with self.subTest(sf=sf,ppm=ppm):
                    out=self.demod(x,p)
                    self.assertEqual([(r["phy_crc_ok"],r["payload_hex"]) for r in out],[(True,GOLD[0].hex())])

    def test_adc_and_gain(self):
        self.assertEqual(clipped(bytes([0,0,127,255,2,127,120,120])),2)
        gains=supported_gains(b"RTL0"+(5).to_bytes(4,"big")+(29).to_bytes(4,"big"))
        rng=np.random.default_rng(33)
        chosen=[]
        def capture(sec):
            return np.clip(np.rint(127.5+4*rng.standard_normal(round(sec*2000000))),0,255).astype("u1").tobytes()
        result=run_autoset(gains,10.,set_gain=chosen.append,capture=capture,progress=lambda _:None)
        self.assertIn(result["gain_db"],gains)
        self.assertEqual(result["gain_db"],chosen[-1])

    def test_scoring_duplicate_false_missing_and_periods(self):
        e={"start_sample":300000,"capture_s":.3,"carrier_hz":915125000,"sf":11,
           "phy_crc_ok":True,"payload_hex":GOLD[0].hex()}
        row=telemetry(e,"primary")
        extra=telemetry(dict(e,payload_hex=GOLD[1].hex()),"primary")
        write_packets(self.dir/"packets.csv",[row,dict(row,capture_s=.31),extra])
        write_json(self.dir/"summary.json",dict(capture_seconds=60.2))
        tx=self.dir/"tx.csv"
        tx.write_text("start_s,payload_hex\n0.2,"+GOLD[0].hex()+"\n30.0,"+GOLD[2].hex())
        result=score(self.dir,tx,.1)
        self.assertEqual([result[k] for k in ("matched","missed","duplicate_decodes","false_decodes")],[1,1,1,1])
        self.assertEqual([v["complete"] for v in result["periods"]],[True,True,False])
        self.assertEqual(periods([(1,i) for i in range(80)],60)["fraction_complete_periods_at_least_80"],.5)

    def test_check_transmitter_readback(self):
        path=self.dir/"readback.csv"
        header="carrier_hz,sf,bandwidth_hz,sync_word,explicit_header,phy_crc,cr_denominator,preamble_symbols,ldro,invert_iq\n"
        data="".join(f"{fc},{sf},125000,0x12,1,1,7,8,{int(self.p.de(sf))},0\n"
                     for fc in self.p.fleet_carriers_hz for sf in self.p.spreading_factors)
        path.write_text(header+data)
        self.assertTrue(check(self.p,path)["confirmed"])
        path.write_text(header+data.replace("0x12","0x34",1))
        self.assertFalse(check(self.p,path)["confirmed"])
