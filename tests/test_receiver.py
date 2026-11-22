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

from config import MONITOR_PROFILES, carrier_preset, carrier_plan_warnings, read_profile
from fixtures import NINE_BRANCH_PROFILE,Event,GOLD,generate,payload
from gain import AutoSetCancelled, supported_gains, run_autoset
from phy import Codec, encode, waveform
from receiver import Branch, Receiver, preamble_levels
from rf_diagnostics import FrequencyOffsets
from reports import periods, score, write_json, write_packets, telemetry
from run import replay
from sic import Cancellation
from sources import Layout, clipped, complex_iq
from telemetry import decode_descent_packet
from tx_settings import check


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.p=read_profile(NINE_BRANCH_PROFILE)
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
        self.assertEqual(read_profile().carriers_hz,(914875000,915125000))
        self.assertEqual(self.p.carriers_hz,(914875000,915000000,915125000))
        self.assertEqual(self.p.fleet_carriers_hz,(914875000,915125000))
        self.assertEqual([self.p.de(sf) for sf in (10,11,12)],[False,True,True])
        with self.assertRaises(ValueError):
            self.p.tuning(915400000)
        with self.assertRaises(ValueError):
            replace(self.p,bandwidth_hz=112000).checked()

    def test_named_plans_match_rosters(self):
        for name,carriers in MONITOR_PROFILES.items():
            p = carrier_preset(read_profile(),name)
            roster = {i:dict(carrier_hz=fc,sf=sf) for i,(fc,sf) in enumerate(
                (fc,sf) for fc in carriers for sf in p.spreading_factors)}
            self.assertEqual(carrier_plan_warnings(p,roster),[])
            self.assertEqual(p.center_hz,915000000)
        self.assertEqual(len(carrier_plan_warnings(p,{1:dict(carrier_hz=914875000),2:dict(carrier_hz=915125000)})),2)

    def test_cfo_margin_signed_tails_capture_expiry_and_validity(self):
        notices=[]
        monitor=FrequencyOffsets(self.p,notices.append)
        monitor.ppm=3
        fc=self.p.carriers_hz[0]
        def add(t,offset,crc=True):
            monitor.add(dict(capture_s=t,carrier_hz=fc,cfo_hz=offset,phy_crc_ok=crc,application_crc_ok=False))
        def read(t):
            return monitor.snapshot(t)["carriers"][str(fc)]
        add(2,10000)
        add(1,-30000,False)
        add(2,30000,None)
        self.assertEqual((read(2)["count"],read(2)["warnings"]),(1,[]))
        add(1,-18000)
        row=read(3)
        np.testing.assert_allclose((row["median_hz"],row["p05_hz"],row["p95_hz"],row["p95_abs_hz"]),
                                   (-4000.,-16600.,8600.,17600.))
        self.assertEqual(row["warnings"],["search_edge"])
        self.assertEqual(row["suggested_ppm"],3+4000e6/fc)
        self.assertEqual(read(601)["count"],2)
        self.assertEqual(read(602)["count"],1)
        self.assertEqual(read(603)["count"],0)
        add(0,18000)
        add(603,14000)
        self.assertEqual(read(603)["warnings"],["median","search_edge"])
        self.assertEqual(read(603)["suggested_delta_ppm"],-14000e6/fc)
        self.assertEqual(len(notices),2)
        monitor.clear_packets()
        add(0,16000)
        add(603,14000)
        self.assertEqual(read(603)["count"],1)
        self.assertEqual(len(notices),2)

    def test_carrier_guard_roster_and_cfo_margin(self):
        p = replace(self.p, carriers_hz=(915000000,), spreading_factors=(11,))
        roster = {1: {"carrier_hz": 915016000, "sf": 11}, 2: {"carrier_hz": 914983999},
                  3: {"sf": 10}, 4: {}, 5: {"carrier_hz": "914983999"}, 6: {"sf": "11"}}
        warnings = carrier_plan_warnings(p, roster)
        self.assertEqual(len(warnings), 2)
        self.assertIn("2, 5", warnings[0])
        self.assertIn("16.001 kHz", warnings[0])
        self.assertIn("SF10", warnings[1])

    def test_carrier_guard_spacing(self):
        p = replace(self.p, carriers_hz=(915200000,915000000,915125000))
        warnings = carrier_plan_warnings(p, {})
        self.assertEqual(len(warnings), 2)
        self.assertIn("125 kHz apart", warnings[0])
        self.assertIn("75 kHz apart", warnings[1])
        self.assertFalse(carrier_plan_warnings(replace(p, carriers_hz=(915200000,915000000)), {}))

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

    def test_weak_half_chip_and_half_bin_acquisition(self):
        p = replace(self.p, carriers_hz=(915000000,), spreading_factors=(11,))
        n, fs, bw = 2048, p.sample_rate_hz, p.bandwidth_hz
        payload = np.random.default_rng(7).integers(0,256,55,dtype=np.uint8).tobytes()
        wave = waveform(p,11,payload,3,True,fs)
        wave /= np.sqrt(np.mean(abs(wave)**2))
        amplitude = (2*.1**2 * bw/fs * 10**(-17.5/10))**.5
        for sub, bins in ((4,24),(4,24.5),(0,24.5)):
            with self.subTest(eighth_chips=sub,cfo_bins=bins):
                start = 32*n+1024+sub
                rng = np.random.default_rng(2000+100*sub)
                iq = rng.standard_normal(2*(start+len(wave)+65536),dtype=np.float32).view(np.complex64)*.1
                phase = rng.uniform(0,2*np.pi) + 2*np.pi*(bins*bw/n)/fs*np.arange(len(wave))
                iq[start:start+len(wave)] += (amplitude*wave*np.exp(1j*phase)).astype(np.complex64)
                raw = np.clip(np.rint(iq.view(np.float32)*128+127.5),0,255).astype(np.uint8)
                rows = []
                rx = Receiver(p,rows.append)
                for offset in range(0,len(raw),2*131072):
                    rx.feed(offset//2,complex_iq(raw[offset:offset+2*131072]))
                rx.close_segment()
                self.assertEqual([(r["payload_hex"],r["phy_crc_ok"]) for r in rows],[(payload.hex(),True)])

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

    def test_preamble_rssi_and_snr_have_distinct_units(self):
        rng = np.random.default_rng(72)
        n = 4096
        for snr_db in (-10., 0., 12.):
            with self.subTest(snr_db=snr_db):
                noise_power = .01
                signal = np.sqrt(noise_power * 10**(snr_db/10))
                noise = np.sqrt(noise_power/2) * (rng.normal(size=(7,n)) + 1j*rng.normal(size=(7,n)))
                chirps = signal * np.exp(1j*np.arange(7))[:, None] + noise
                rssi, snr = preamble_levels(chirps)
                self.assertAlmostEqual(rssi, 10*np.log10(signal**2 + noise_power), delta=.3)
                self.assertAlmostEqual(snr, snr_db, delta=.6)
                louder, same_snr = preamble_levels(2*chirps)
                self.assertAlmostEqual(louder-rssi, 20*np.log10(2))
                self.assertAlmostEqual(same_snr, snr)
        self.assertEqual(preamble_levels(np.zeros((3, n))), (None, None))
        self.assertEqual(preamble_levels(np.ones((3, n))), (0., None))

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

    def test_short_adc_overload_during_sf10_payload(self):
        p = replace(self.p, carriers_hz=(915000000,), spreading_factors=(10,))
        data = b"CRC retry with bursts " + bytes(range(32))
        symbol = (1 << 10) * p.sample_rate_hz // p.bandwidth_hz
        first = 75137
        burst_time = np.arange(4 * symbol)
        blocker = 1.65 * np.exp(2j * np.pi * burst_time / 32) * (burst_time % 1024 < 410)
        data_start = first + (p.preamble_symbols + 4) * symbol + symbol//4
        at = data_start + 25 * symbol

        for valid_crc in (True, False):
            with self.subTest(valid_crc=valid_crc):
                if valid_crc:
                    tx = waveform(p, 10, data, 3, True, p.sample_rate_hz)
                else:
                    with patch("lora_phy.tx.calc_crc", return_value=(0x13, 0xa7)):
                        tx = waveform(p, 10, data, 3, True, p.sample_rate_hz)
                x = np.pad(.015 * tx, (first, 30000))
                noise = np.random.default_rng(72).standard_normal(len(x)*2, dtype=np.float32)
                x += .008 * noise.view(np.complex64)
                x[at:at+len(blocker)] += blocker
                adc = np.clip(np.rint(x.view(np.float32)*128 + 127.5), 0, 255).astype("u1")
                self.assertGreater(clipped(adc), 0)
                rows = self.demod(complex_iq(adc), p)
                self.assertEqual([r["phy_crc_ok"] for r in rows], [valid_crc])
                if valid_crc:
                    self.assertEqual(rows[0]["payload_hex"], data.hex())

    def test_zero_guard_after_a_faded_sf10_payload(self):
        p = replace(self.p, carriers_hz=(915000000,), spreading_factors=(10,))
        first, second = bytes(range(91, 146)), b"following " + bytes(range(48))
        symbol = (1 << 10) * p.sample_rate_hz // p.bandwidth_hz
        leading = 72113
        a = waveform(p, 10, first, 3, True, p.sample_rate_hz)
        b = waveform(p, 10, second, 3, True, p.sample_rate_hz)
        x = np.pad(.04 * np.concatenate((a, b)), (leading, 40000))
        data_start = leading + (p.preamble_symbols + 4) * symbol + symbol//4
        fade = data_start + 37 * symbol
        x[fade:fade+2*symbol] *= .001
        noise = np.random.default_rng(9).standard_normal(len(x)*2, dtype=np.float32)
        x += .045 * noise.view(np.complex64)
        iq = complex_iq(np.clip(np.rint(x.view(np.float32)*128 + 127.5), 0, 255).astype("u1"))
        with patch.object(Codec, "soft_payload", return_value=None):
            self.assertEqual([r["phy_crc_ok"] for r in self.demod(iq, p)], [False, True])
        rows = self.demod(iq, p)
        self.assertEqual([(r["phy_crc_ok"], r["payload_hex"]) for r in rows],
                         [(True, first.hex()), (True, second.hex())])
        self.assertAlmostEqual(rows[1]["start_sample"] - rows[0]["start_sample"], len(a), delta=2)

    def test_soft_fec_bin_mapping_and_crc(self):
        data = bytes(range(40))
        # One ambiguous symbol in a payload block. CR 4/5 has a single parity bit.
        for sf, cr, explicit, valid_crc in ((7, 1, True, True), (9, 2, False, True),
                                            (10, 3, True, False), (12, 4, True, True)):
            with self.subTest(sf=sf, cr=cr, valid_crc=valid_crc):
                p = replace(self.p, explicit_header=explicit, implicit_length=len(data),
                            coding_rate_denominator=cr+4)
                codec = Codec(p, 915000000, sf)
                if valid_crc:
                    bins = encode(p, sf, data, cr, True)
                else:
                    with patch("lora_phy.tx.calc_crc", return_value=(0x13, 0xa7)):
                        bins = encode(p, sf, data, cr, True)
                header = codec.header(bins[:8])
                spectra = np.zeros((len(bins), 1 << sf), dtype=np.float32)
                spectra[np.arange(len(bins)), bins] = 10.
                rival = (int(bins[10]) + (4 if p.de(sf) else 1)) % (1 << sf)
                spectra[10, rival] = 10.1
                hard = spectra.argmax(axis=1).astype(np.uint16)
                self.assertEqual(codec.soft_payload(hard, spectra, header), data if valid_crc else None)

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

    def test_sf10_overestimated_preamble_clock(self):
        p=replace(self.p,carriers_hz=(915000000,),spreading_factors=(10,))
        data=payload(1,3)
        ppm=10.
        y=waveform(p,10,data,3,True,1_000_000)
        y=resample(y,round(len(y)*(1+ppm*1e-6)))
        y*=np.exp(2j*np.pi*650*np.arange(len(y))/1_000_000)
        x=np.pad(y,(76513,35000))
        rng=np.random.default_rng(0)
        x+=.5*rng.standard_normal(len(x)*2,dtype=np.float32).view(np.complex64)
        synchronize=Branch.synchronize
        def noisy_estimate(branch,*args):
            burst=synchronize(branch,*args)
            if burst:
                # Inject the short-preamble estimation error observed on hardware.
                # The waveform has constant RF offset and a 10 ppm sample clock.
                burst.drift_bins_per_symbol=.002
                burst.clock_bins_per_symbol=-20e-6*branch.n
            return burst
        with patch.object(Branch,"synchronize",noisy_estimate):
            rows=self.demod(x,p)
        self.assertEqual([(r["phy_crc_ok"],r["payload_hex"]) for r in rows],[(True,data.hex())])
        self.assertAlmostEqual(rows[0]["clock_error_ppm"],ppm)
        self.assertTrue(telemetry(rows[0],"primary")["application_crc_ok"])

    def test_sf10_clock_recovery_when_preamble_reports_zero(self):
        p = replace(self.p, carriers_hz=(915000000,), spreading_factors=(10,))
        data = bytes(range(19, 74))
        template = waveform(p, 10, data, 3, True, p.sample_rate_hz)
        synchronize = Branch.synchronize

        def short_preamble_estimate(branch, *args):
            burst = synchronize(branch, *args)
            if burst:
                # Model the missed clock slip and apparent RF drift seen on the bench.
                burst.clock_bins_per_symbol = 0.
                burst.drift_bins_per_symbol = -.006
            return burst

        for ppm in (-10., 10.):
            with self.subTest(ppm=ppm):
                y = resample(template, round(len(template) * (1 + ppm * 1e-6)))
                y *= np.exp(2j*np.pi*225*np.arange(len(y))/p.sample_rate_hz)
                x = np.pad(y, (76513, 35000))
                noise = np.random.default_rng(40).standard_normal(len(x)*2, dtype=np.float32)
                x += .4 * noise.view(np.complex64)
                with patch.object(Branch, "synchronize", short_preamble_estimate):
                    rows = self.demod(x, p)
                self.assertEqual([(r["phy_crc_ok"], r["payload_hex"]) for r in rows],
                                 [(True, data.hex())])
                self.assertAlmostEqual(rows[0]["clock_error_ppm"], ppm, delta=2.)

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

    def test_autoset_rechecks_changing_headroom_with_a_bound(self):
        for amplitude,issue in ((96,"limited ADC headroom"),(128,"clipping")):
            with self.subTest(issue=issue):
                applied=[]
                def capture(seconds):
                    a=4 if seconds<1 else amplitude
                    return bytes([128-a,128-a,min(255,128+a),min(255,128+a)])*3000
                scan=run_autoset((0,5,10,15,20),20,set_gain=applied.append,capture=capture,progress=lambda _:None)
                checks=[v["gain_db"] for v in scan["headroom_checks"]]
                self.assertEqual(checks,[20,10,0])
                self.assertEqual((scan["gain_db"],applied[-1]),(0,0))
                self.assertIn(issue,scan["warning"])
                self.assertEqual(scan["level"]["clip_fraction"],float(amplitude==128))
        saturated=bytes([0,0,255,255])*3000
        scan=run_autoset((0,10),10,set_gain=lambda _:None,capture=lambda _:saturated,progress=lambda _:None)
        self.assertEqual(scan["gain_db"],0)  # Even the short scan saw clipping at every setting.
        self.assertIn("clipping",scan["warning"])

    def test_autoset_preserves_stop_and_input_errors_during_recheck(self):
        stable=bytes([124,124,132,132])*3000
        loud=bytes([16,16,240,240])*3000
        for failure in ("disconnect","flat","stop"):
            with self.subTest(failure=failure):
                applied=[]
                stopped=False
                def capture(seconds):
                    nonlocal stopped
                    if seconds<1:
                        return stable
                    if applied[-1]==10:
                        return loud
                    if failure=="disconnect":
                        raise OSError("rtl_tcp closed the stream")
                    if failure=="flat":
                        return bytes([128])*len(stable)
                    stopped=True
                    return b""
                expected={"disconnect":OSError,"flat":ValueError,"stop":AutoSetCancelled}
                with self.assertRaises(expected[failure]):
                    run_autoset((0,5,10),10,set_gain=applied.append,capture=capture,cancelled=lambda:stopped,progress=lambda _:None)
                self.assertLess(applied[-1],10)

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
