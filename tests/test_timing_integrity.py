from dataclasses import replace
import unittest
from unittest.mock import Mock

import numpy as np

from config import read_profile
from phy import upchirp,waveform
from receiver import Branch,Burst,RailHistory,Receiver,TimingCandidate,TimingEvidence
from rf_diagnostics import FrequencyOffsets
from reports import FIELDS
from sources import complex_iq, rail_indices


class TimingIntegrityTests(unittest.TestCase):
    def test_refinement_can_disprove_a_coarse_chip_alias(self):
        p=read_profile()
        br=Branch(p,p.center_hz,10)
        length,n=br.length,br.n
        freq=3.2
        start=length
        sfd=start+(p.preamble_symbols+2)*length
        wave=waveform(p,10,bytes(55),3,True,2*p.bandwidth_hz)
        samples=wave[:(p.preamble_symbols+5)*length]*np.exp(1j*np.pi*freq*np.arange((p.preamble_symbols+5)*length)/n)
        buf=np.r_[np.zeros(start),samples].astype(np.complex64)
        coarse=freq-165
        original=br.preamble_fit(buf,0,sfd,0.,freq,coarse)
        candidate=TimingCandidate(sfd-330,start-330,coarse,original)
        verified=br.resolve_timing(buf,0,candidate)
        self.assertIs(verified,original)
        self.assertEqual(verified.timing_check,"ok")
        self.assertAlmostEqual(br.start_cfo(verified),freq,places=4)
        self.assertTrue(br.cfo_consistent(verified))
        self.assertEqual(verified.coarse_bins,coarse)

    def test_clean_phase_uses_only_consecutive_rail_free_symbols(self):
        p=read_profile()
        rails=RailHistory(1,1)
        br=Branch(p,p.center_hz,10,rails)
        n,length=br.n,br.length
        freq=.17
        sfd=(p.preamble_symbols+2)*length
        buf=np.tile(upchirp(10,2),p.preamble_symbols+2)*np.exp(1j*np.pi*freq*np.arange(sfd)/n)
        for j in (5,6,7):
            rails.append(j*length+10,np.array([0]))
            buf[j*length:(j+1)*length]*=np.exp(1j*j*j)
        b=Burst(sfd,0.,freq,freq,0.,0.,1.)
        self.assertAlmostEqual(br.clean_start_cfo(buf,0,b,freq),freq,places=5)
        # A third usable pair is no longer available.
        rails.blocks.clear()
        rails.append(2*length,np.arange(4*length))
        self.assertIsNone(br.clean_start_cfo(buf,0,b,freq))

    def test_ambiguous_solutions_retain_frozen_estimate(self):
        p=read_profile()
        br=Branch(p,p.center_hz,10)
        original=Burst(5000,0.,0.,0.,0.,0.,1.)
        rival=Burst(5012,12.,6.,6.,0.,0.,1.,timing_check="fallback")
        br.timing_alternatives=Mock(return_value=[rival])
        positive=TimingEvidence(True,(.6,.5),(.5,.6))
        missing=TimingEvidence(None,(None,None),(None,None))
        for evidence,agreed,status in ((positive,False,"suspect"),(missing,False,"suspect"),(missing,True,"ok")):
            br.timing_evidence=Mock(return_value=evidence)
            chosen=br.resolve_timing(np.empty(0),0,TimingCandidate(5012,12.,6.,original,agreed))
            self.assertIs(chosen,original)
            self.assertEqual(chosen.timing_check,status)

    def test_agreeing_fit_with_four_strong_chirps_defers_masked_retries(self):
        p=read_profile()
        br=Branch(p,p.center_hz,10)
        b=Burst(40000,0.,0.,0.,0.,0.,1.)
        c=TimingCandidate(b.sfd,b.start,0.,b,agreed=True)
        br.timing_evidence=Mock(return_value=TimingEvidence(None,(.3,.3),(.3,.3)))
        br.resolve_timing=Mock()
        br.read_burst=Mock(side_effect=[None,(b"good",True),(b"damaged",False)])
        buf=np.zeros(b.sfd+br.n+2*br.length,np.complex64)
        self.assertIsNone(br.prepare_timing(buf,0,c,None,False))
        self.assertIs(br.prepare_timing(buf,0,c,None,False),b)
        br.resolve_timing.assert_not_called()
        self.assertIs(br.prepare_timing(buf,0,c,None,False),br.resolve_timing.return_value)
        br.resolve_timing.assert_called_once_with(buf,0,c)
        br.timing_evidence.assert_called_once_with(buf,0,b)

    def test_settled_clock_holds_earliest_pending_refinement(self):
        p=replace(read_profile(),carriers_hz=(915000000,),spreading_factors=(10,))
        rx=Receiver(p,lambda _:None)
        rx.feed(0,np.zeros(32,np.complex64))
        channel=rx.channels[0]
        br=channel.branches[0]
        start=100_000
        br.n_grid=start+100*br.length
        rx.next_sample=br.n_grid*channel.d
        b=Burst(start+(p.preamble_symbols+2)*br.length,start,0.,0.,0.,0.,1.)
        br.pending=[TimingCandidate(b.sfd,start,0.,b,agreed=True)]
        delay=(len(channel.taps)-1)//2
        self.assertLessEqual(rx.settled_before(),(start-br.n)*channel.d-delay)
        br.pending.clear()
        self.assertGreater(rx.settled_before(),start*channel.d)

    def test_journal_compatibility_by_prefix(self):
        # This is the published pre-integrity journal schema. Future columns
        # may follow it without changing any existing column positions.
        prior = ("start_sample capture_s carrier_hz sf bandwidth_hz sync_word explicit_header "
                 "cr_denominator ldro preamble_symbols phy_crc_ok application_crc_ok chipsat_id "
                 "packet_counter cfo_hz drift_hz_s clock_error_ppm preamble_power_dbfs pass_name "
                 "payload_hex telemetry_json run_id run_name receive_utc timestamp_source rssi_dbfs snr_db").split()
        self.assertEqual(FIELDS[:len(prior)], prior)
        self.assertEqual(FIELDS[len(prior):len(prior)+3], ["timing_check","sync_rail_samples","coarse_cfo_hz"])

    def test_only_original_crc_pass_preserves_payload_as_suspect(self):
        p=read_profile()
        branch=Branch(p,p.center_hz,10)
        preferred=Burst(5000,0.,0.,0.,0.,0.,1.,timing_check="fallback")
        backup=Burst(4990,-10.,0.,0.,0.,0.,1.,timing_check="suspect")
        preferred.alternate=backup
        branch.pending=[preferred]
        emit=Mock()
        # First block only has the preferred result. The original header and
        # payload may require more samples than the preferred hypothesis.
        preferred.result=(b"damaged",False)
        branch.decode(np.zeros(1,np.complex64),0,emit)
        emit.assert_not_called()
        backup.result=(b"verified",True)
        branch.decode(np.zeros(1,np.complex64),0,emit)
        emit.assert_called_once_with(branch,backup,b"verified",True)
        self.assertEqual(branch.pending,[])

    def test_other_full_downchirp_and_preamble_only_fallback(self):
        p = read_profile()
        rail = RailHistory(1,1)
        branch = Branch(p,p.center_hz,10,rail)
        length = branch.length
        sfd = length*(p.preamble_symbols+2)
        buf = np.zeros(sfd+2*length,np.complex64)
        up = upchirp(10,2)
        buf[:p.preamble_symbols*length] = np.tile(up,p.preamble_symbols)
        buf[sfd+length:] = up.conj()
        original = Burst(sfd-200,-200.,-100.,-100.,0.,0.,1.,timing_check="suspect")
        candidate = TimingCandidate(sfd,0.,0.,original)
        rail.append(sfd,np.arange(length))
        refit = branch.resolve_timing(buf,0,candidate)
        self.assertEqual((refit.timing_check,refit.sfd),("refit",sfd))
        self.assertIs(refit.alternate,original)
        rail.append(sfd+length,np.arange(length))
        fallback = branch.resolve_timing(buf,0,candidate)
        self.assertEqual((fallback.timing_check,fallback.sfd),("fallback",sfd))
        self.assertAlmostEqual(fallback.freq_bins,0.)
        branch.wrap_check = Mock(return_value=None)
        uncertain = branch.resolve_timing(buf,0,candidate)
        self.assertIs(uncertain,original)
        self.assertEqual(uncertain.timing_check,"suspect")
        self.assertEqual(uncertain.alternate.sfd,sfd)

    def test_suspect_timing_excluded_from_cfo_with_legacy_rows_accepted(self):
        p=read_profile()
        fc=p.carriers_hz[0]
        monitor=FrequencyOffsets(p)
        def add(hz,**extra):
            monitor.add(dict(phy_crc_ok=True,capture_s=2.,cfo_hz=hz,carrier_hz=fc,**extra))
        add(-30000,timing_check="suspect")
        add(-400)
        add(-410,timing_check="refit")
        row=monitor.snapshot(3.)["carriers"][str(fc)]
        self.assertEqual((row["count"],row["median_hz"],row["warnings"]),(2,-405,[]))

    def test_original_rails_follow_fir_across_partial_blocks_and_gaps(self):
        p = replace(read_profile(),carriers_hz=(915000000,915125000),spreading_factors=(10,),invert_iq=True)
        adc = np.full((256,2),127,np.uint8)
        adc[2,0],adc[13,1],adc[88],adc[224,0] = 0,255,(0,255),255
        raw = adc.tobytes()
        np.testing.assert_array_equal(rail_indices(raw),[2,13,88,224])
        rx = Receiver(p,lambda _:None)
        for lo,hi in zip((0,3,14,71,95,221),(3,14,71,95,221,256)):
            block = raw[2*lo:2*hi]
            rx.feed(1100+lo,complex_iq(block),rail_indices(block))
        for c in rx.channels:
            expected = np.convolve(((adc==0)|(adc==255)).any(axis=1).astype(int),
                                   np.ones(len(c.taps),int))[:len(adc):c.d]>0
            np.testing.assert_array_equal(c.branches[0].rails.mask(0,len(expected)),expected)
            np.testing.assert_array_equal(c.rails.mask(1,len(expected),2),expected[1::2])
            self.assertEqual(c.rails.count(2,224),3)
        # Disjoint segments have distinct FIR histories. A residual IQ value
        # at the rail is clean when the original CU8 mask contains no hits.
        rx.feed(9000,np.full(256,127.5/128,dtype=np.complex64),np.empty(0,dtype=int))
        self.assertFalse(rx.channels[0].rails.mask(0,64).any())
