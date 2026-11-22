# Windows bench validation

## 14 November 2026 · Eight back-to-back frames

The 23.330816 s RTL-SDR run detected eight SF10 frames at 915.000 MHz and 125 kHz bandwidth. Seven passed both CRCs live. The first frame of each pair started about 5.00004 s after the previous pair, and its partner started 854.023–854.024 ms later. Nominal frame airtime was 854.016 ms. The capture recorded zero DSP or recorder drops and no gaps. All 2,368 rail hits fell inside CSID 1, counter 1, which passed both CRCs live. [Original live run](runs/20261114_141301_0jmal1ml/summary.json)

The rejected frame started at 4.340082 s and contained zero rail hits. From 4.964722 to 4.981106 s, two successive symbol decisions selected the wrong FFT bins. After independent CRC verification, projecting those symbols back onto the original IQ gave levels of −60.1 and −63.1 dBFS. The median for data symbols 8–47 was −43.2 dBFS, and the local median FFT-bin floor changed by about 1 dB. Signal strength recovered before the next preamble at 5.194106 s. The physical cause of the fade is unresolved. The original SIC replay also scored seven valid frames. [Symbol levels over time](validation/sf10_back_to_back_corrected/20261114_142055_l0y4hucu/first_packet_fade.png) · [Baseline SIC summary](validation/sf10_back_to_back_baseline/20261114_141512_xsxzpfgk/summary.json)

The primary decoder's soft-decision FEC retry recovered CSID 1, counter 0 using the symbol spectra and transmitted parity. Both CRCs passed for counters 0–3 from both IDs. All seven earlier valid payloads and all eight preamble timestamps matched the original journal. Feeding the same IQ through the live reader at 1 MS/s produced the identical eight frames with no dropped samples or backlog growth. [Recovered packet journal](validation/sf10_back_to_back_corrected/20261114_142055_l0y4hucu/packets.csv) · [Paced live-reader replay](validation/sf10_back_to_back_paced/20261114_143140_rvie05_m/summary.json)

All 37 tests passed, including a synthetic zero-guard pair, both LDRO modes, all four payload coding rates and a deliberately invalid transmitted CRC. The independent PHY fixture scored 5/5 and the concurrent-branch fixture scored 9/9. Four earlier hardware recordings retained their 24, 4, 4 and 11 valid frames. The synthetic random-phase collision fixture retained all 179 previous matches and recovered nine more, with zero false or duplicate matches. [Acceptance](validation/sf10_back_to_back_tests/20261114_142200_nv7hppqj/acceptance.json) · [Fleet stress score](validation/sf10_back_to_back_regressions/fleet_stress/20261114_142227_8hu1vy_e/score.json)

## 13 November 2026 · Burst interference during a 915 MHz SF10 packet

The 31.981568 s RTL-SDR recording contained four non-overlapping 55-byte frames at 915 MHz. The original decoder synchronized to all four. The frame that started at 5.349306 s failed both CRCs. Between 6.058 and 6.077 s the ADC hit a rail on 17,114 samples of its payload. The other three frames had zero clipped samples. DSP and recorder drops were zero. Replaying the original primary and SIC passes reproduced three valid frames. [Live summary](runs/20261113_164721_dg7essg7/summary.json) · [Baseline SIC replay](validation/noisy_sf10_baseline/20261113_165009_qupe6h5k/summary.json)

With the ChipSats off, a 30 s recording at the same 42.1 dB gain contained five burst groups spaced about 5.76 s apart, with strong apparent peaks near 915.234 and 915.362 MHz. A separate 24 s recording at 28 dB also clipped during the bursts. A later back-to-back tuning check no longer showed this pattern at its original tuning, so the emitter and the peak frequencies in an undistorted capture remain unconfirmed. [Clipping and spectra](validation/noisy_sf10_corrected/20261113_165542_mmuj4c1h/interference.png) · [Capture measurements and run paths](validation/noisy_sf10_corrected/20261113_165542_mmuj4c1h/interference_measurements.json)

After the existing CRC retries fail, the primary decoder checks for brief amplitude outliers within the packet. When present, it blanks those samples in private symbol windows and retries the validated header and transmitted PHY CRC once. Full-profile replay recovered CSID 2, counter 0 on this retry with both CRCs passing. Both IDs sent counters 0 and 1. The three earlier valid payloads and all four start samples were identical. [Corrected packet journal](validation/noisy_sf10_corrected/20261113_165542_mmuj4c1h/packets.csv)

All 35 tests passed, including valid and deliberately incorrect transmitted CRCs under synthetic ADC overload. The independent PHY fixture scored 5/5 and the concurrent branches scored 9/9. Earlier hardware recordings retained 4/4 and 11/11 valid frames. The synthetic random-phase stress fixture retained all 179 prior matches out of 300 with zero false or duplicate matches. [Acceptance](validation/noisy_sf10_tests/20261113_165708_i4fadiej/acceptance.json) · [Stress replay](validation/noisy_sf10_regressions/random_stress/20261113_165717_q8qxdpcr/score.json)

## 13 November 2026 · Two ChipSats at 915 MHz and SF10

The RTL-SDR captured four separate 55-byte frames in 23.986176 s at 125 kHz bandwidth. The original live journal passed both CRCs for three frames. CSID 1, counter 1 started at 11.658226 s, had a valid LoRa header and failed both CRCs on the original decode. Its preamble measured −41.45 dBFS. The capture recorded zero clipped samples, DSP drops and recorder drops. [Original live summary](runs/20261113_161217_bejjek0c/summary.json)

The short preamble gave a zero sample-clock correction for that frame. When cheaper retries fail, the primary decoder now measures residual timing and frequency change across the received data symbols. It retries the same header and checks the transmitted PHY CRC. On the identical IQ, this recovered CSID 1, counter 1 with an effective clock correction of +8.55 ppm and a passing DeSCENT CRC. The nine-branch replay recovered counters 0 and 1 from both IDs on the primary pass. The other three payloads and all four preamble timestamps matched the live journal. The saved replay opens in the GUI with two ChipSats reporting valid data and two packets per ChipSat. [Corrected replay journal](validation/sf10_pair_corrected/20261113_162523_cze52v_e/packets.csv)

Feeding the recorded IQ through the live socket reader at 1 MS/s produced the same four payloads at the same timestamps, with zero dropped samples and no backlog growth. The separate 90 s hardware recording retained all 11 previously recovered packets. The synthetic random-phase stress run retained all 176 previous matches and recovered three more, with zero false or duplicate matches. All 34 tests passed, along with the 5/5 independent PHY fixture and the 9/9 concurrent-branch fixture. [Paced replay through the live reader](validation/sf10_pair_paced/20261113_163123_0nzdd4xu/summary.json) · [Stress score](validation/sf10_pair_stress/20261113_162824_tht0a5wr/score.json) · [Acceptance results](validation/sf10_pair_tests/20261113_162912_bexjk_4w/acceptance.json)

## 13 November 2026 · Auto Set and DeSCENT packet checks

**915 MHz SF10 bench capture.** At 125 kHz bandwidth and 29.7 dB manual gain, the RTL-SDR recorded 90 seconds with zero rail hits, DSP drops or recorder drops. The original live decoder found eleven frames and seven passed both CRCs. Replaying identical IQ with the revised primary decoder recovered all eleven. The extra retries keep RF drift independent of sample-clock slip and test a smaller clock correction if the first estimates fail. Recovered counters were 3, 9, 11 and 12. All eleven frames came from ChipSat 1 with validity byte `0x7F`. Counter 10 was absent. No transmit record was supplied for a packet reception rate. [Original live run](validation/sf10_on_air/20261113_155616_zocvntb3/summary.json) · [Recovered packet journal](validation/sf10_clock_verified/20261113_160015_2hmnhrfl/packets_primary.csv)

The full nine-branch profile produced the same eleven payloads at the same sample indices. Offline SIC added zero trusted frames. All 32 tests passed, and the synthetic random-phase fleet stress replay matched 176 of 300 offered transmissions with zero false matches and duplicates in 17.1 seconds of processing for 93.2 seconds of IQ. [Nine-branch replay](validation/sf10_clock_multicarrier/20261113_160220_ecw4mcpp/summary.json) · [Acceptance tests](validation/sf10_recovery_tests/20261113_160122_be5155qn/acceptance.json) · [Stress replay score](validation/sf10_clock_fleet/20261113_160245_jb844nvo/score.json)

- **Physical RTL-SDR.** The verification window at 48 dB had 1.2368% rail hits. Auto Set lowered the tuner to 38.6 dB, verified it, and recorded 40 seconds (80,000,000 bytes) with zero DSP or recorder drops. The nine configured branches found zero matching syncs. Offline SIC found zero packets on the same IQ. The peak capture block had 30.56% rail hits. There was no transmit record for this test. [Hardware run](validation/autoset_hardware/20261113_153613_okd6g36r/summary.json) · [SIC replay](validation/autoset_hardware_sic/20261113_153746_rbnwi0ej/summary.json)
- **Operator live journals.** On 915.000 MHz at SF10, ChipSat 1 transmitted counter 1 at capture time 82.430049 s with both CRCs passing and validity byte `0x7F`. In a separate 100.27-second run, four candidate frames failed both CRCs. Two preamble starts were 30 ms apart on that same carrier and SF. The corresponding 55-byte frames take 854 ms at the decoded PHY settings. Those runs saved no IQ for per-packet diagnosis. [Trusted packet journal](runs/20261113_152258_j4skb8p2/packets.csv) · [CRC failures](runs/20261113_153110_du4w3eci/packets.csv)
- **Replay of recorded bench RF.** Blind replay of the 915 MHz two-radio capture reproduced the original payload bytes and start samples for all ten trusted frames. Seven came from ID 249 at SF11 and three from ID 250 at SF12. The application CRCs were also recomputed independently over the first 53 payload bytes. [Replay journal](validation/autoset_packet_check/20261113_153420_p5g28fu4/packets.csv)
- **Synthetic IQ.** The GUI recorded IQ when a burst first appeared during Auto Set verification. Tests also covered fluctuating levels, clipping at minimum gain, Stop, flat IQ and socket failure. All 31 tests passed. In a separate SF10 experiment, both isolated frames passed. Of two equal-power frames starting 30 ms apart, primary recovered one and replay with SIC recovered both. [Acceptance](validation/autoset_startup/20261113_153020_73tjwj3l/acceptance.json) · [Primary score](validation/autoset_sf10_comparison/20261113_153955_k7_po1bm/primary/20261113_153956_c79k395y/score.json) · [SIC score](validation/autoset_sf10_comparison/20261113_153955_k7_po1bm/sic/20261113_153956_oi37u6iv/score.json)

## Field operator and blind coverage

The Windows client was exercised at 1366 × 768 with both a two-radio recording and a populated fleet. The receiver used its IQ preamble search for every decode. The synthetic load tests used generated starts independent of flight timings. The scorer read the transmit records after decoding.

| Evidence | Primary distinct trusted IDs per complete 30 s period | With offline SIC | Periods reaching 80 |
|---|---|---|---|
| Synthetic staggered load, 100 radios, two carriers, 30 SF10 / 12 SF11 / 8 SF12 on each | 100, 100, 100 | 100, 100, 100 | 3 of 3 in both passes |
| Existing random-phase collision stress IQ, same 100-radio allocation | 61, 56, 59 | 91, 83, 82 | 0 of 3 primary, 3 of 3 with SIC |

The staggered campaigns matched 300/300 transmitted payloads on identical IQ, with zero false matches, duplicates or dropped samples. Primary DSP took 18.44 seconds for 93 seconds of IQ. Both SIC passes together took 46.73 seconds of DSP. A regression test covers the weak residual that initially reacquired the same valid payload 1.18 ms from the primary start. The merge checks exact payload, carrier, SF and symbol-duration proximity. [Staggered load report](validation/field_final_load/20261112_191057_o_xgkn87/field_acceptance.json)

The random-phase recording yielded 176 matched transmissions on primary and 256 after cancellation. All 80 additional recoveries passed both CRCs and exported with the SIC decoder pass. Primary and merged results each had zero false or duplicate matches. SHA-256 checks verified preservation of the input IQ, original summary, journal and original score across reprocessing. [Per-period comparison](validation/field_stress_final/20261112_185925_u_c4g0id/analyses/20261112_191057_0fj9xgga/comparison.json) · [SIC-only export](validation/field_stress_final/20261112_185925_u_c4g0id/analyses/20261112_191057_0fj9xgga/exports/sic-recoveries.csv)

**Recorded physical bench IQ.** A 14.942208-second RTL-SDR recording at 915.000 MHz yielded seven trusted SF11 packets from ID 249 and three trusted SF12 packets from ID 250. Both radios were heard in the partial bench period. Neither reported valid GPS. SIC added zero packets. These measurements came from replay of the saved physical samples. [Original capture](runs/20261112_162107_mc3rlsbi/capture.iq) · [Bench comparison](validation/field_recorded/20261112_185916_rr7q71y5/analyses/20261112_191145_kz7h8pt2/comparison.json)

**Generic LoRa and telemetry.** A separate synthetic CU8 bench fixture produced 25 diagnostic rows, 23 PHY-valid Generic LoRa lines and 20 trusted science packets from two IDs. The text message `hello`, binary `00 FF 00 41 7F`, valid telemetry and an unrelated 55-byte payload matched their original bytes. One deliberately corrupted PHY CRC and one absent PHY CRC appeared only in the diagnostic packet log. GPS tests included a stationary radio and a moving radio losing and regaining fix. [Synthetic bench summary](validation/field_final_load/20261112_191057_o_xgkn87/bench/20261112_191101_39akmokx/summary.json)

**Workflow.** Tests cover a real demodulated preamble spanning a period boundary, a leading partial period, empty periods, a paused recording clock, gaps, sorted selection during traffic, fixed zoom, combined altitude references, repeated dated fixes, archive inspection without IQ, filtered exports after Clear View, cancel/restart SIC and New Run during outstanding work. The quick checks also recovered all five packets from the independent EPFL fixture and all nine simultaneous branch transmissions. [Windows test log](validation/field_operator/20261112_191427_7o3b899b/unit_tests.txt) · [Quick acceptance](validation/field_operator/20261112_191427_7o3b899b/acceptance.json)

**Field checks still open.** The RTL-SDR driver enumerated zero devices on this host during this pass. Fresh physical auto-gain, USB disconnect indications, sustained recording to the field drive and real moving GPS tracks remain to be exercised. The synthetic power and motion cases and the archived two-radio recording cover different conditions from an on-air fleet acquisition.

The screenshots show the final Windows client. The time history uses recorded hardware IQ. The fleet overview, moving track and generic terminal use labeled synthetic IQ. [Fleet overview](docs/field-fleet.png) · [Recorded two-radio bench](docs/field-bench.png) · [Time history](docs/field-trends.png) · [Ground track](docs/field-track.png) · [Generic LoRa](docs/field-generic.png)

## Fleet operator on Windows

The 1366 × 768 screenshot shows the native Tk application after an unpaced replay of the 93.2-second synthetic fleet recording. The journal held 224 packet rows. The fleet table contained 70 distinct trusted ChipSats. Post-run scoring matched 176 of 300 offered transmissions with zero false or duplicate matches. Primary DSP took 16.90 seconds, or 5.51× real time, with zero dropped samples. [Screenshot](docs/fleet-receiver.png) · [Run summary](validation/operator_preview/20261112_161127_f059wfjv/summary.json) · [TX comparison](validation/operator_preview/20261112_161127_f059wfjv/score.json)

| Operator check | Measured result |
|---|---|
| Run exports from the screenshot replay | 224 full packet rows, 2 rows for ChipSat 041 at 914.875 MHz / SF11, 70 fleet rows. Export counts and run IDs matched the packet journal. [Full](validation/operator_preview/20261112_161127_f059wfjv/exports/all-packets.csv) · [Filtered](validation/operator_preview/20261112_161127_f059wfjv/exports/chipsat-041-sf11.csv) · [Fleet](validation/operator_preview/20261112_161127_f059wfjv/exports/fleet-summary.csv) |
| Selection during paced IQ replay | ChipSat 002 stayed selected during incoming packets and last-heard sorting. Telemetry, raw bytes and two history rows matched the independent fixture. Full, filtered and fleet exports contained 5, 2 and 3 rows. PRR appeared as 5/5 after scoring against the transmit record. |
| Repeated runs | The next IQ replay produced one packet and one ChipSat under a new ID. The first journal and exports remained readable. Three resets during active replay finished without remaining receiver, recorder or export threads. A reset during a pending export waited for its file. |
| Display bounds and telemetry | More than 300 injected packet rows exercised the rolling log and 12-packet history. Full export retained the aged-out rows. A packet losing GPS fix displayed the previous valid coordinates and their age. A valid zero and an unavailable reading produced distinct CSV values. |
| Receiver setup and decoder | A gain-dependent simulated RTL stream exercised one-shot gain selection, both 915.000 MHz SF11/SF12 branches and a drained 200,000-byte IQ recording. Independent replay matched 5/5 known transmissions. Synthetic simultaneous replay matched 9/9 across all branches. Physical gain calibration in the redesigned GUI remains unverified. |

All 21 tests and the quick acceptance gates passed. The operator test exercised the three choices in the Export CSV dialog, verified its default selection and checked the Open Folder destination. The telemetry pane was also checked at 1336 × 692 and 1150 × 650 client sizes. [Test log](validation/operator_ui/20261112_161533__u3f3426/unit_tests.txt) · [Acceptance report](validation/operator_ui/20261112_161533__u3f3426/acceptance.json)

## Selectable carriers, including 915.000 MHz

The GUI and CLI now accept a subset of the three default carriers or custom frequencies. With all three selected, synthetic replay recovered **9/9 simultaneous packets** across SF10, SF11 and SF12 with zero false or duplicate matches. Selecting just 915.000 MHz on the same waveform recovered the three center-channel packets, one per SF. The five-packet independent EPFL fixture also passed with zero false or duplicate matches. All 15 software tests passed, including GUI replay with a subset and a custom carrier. [Three-carrier acceptance](validation/center_915/20261112_152040_dilfhrcp/acceptance.json)

Monitoring the center alongside A and B recovered the same 176 valid packets from the original 93.2 s fleet IQ in 17.29 s of primary processing, with zero local drops, false matches or duplicate matches. A subsequent 98.96-second live hardware run on the bench logged seven SF11 and 21 SF12 packets on 915 MHz passing both CRCs. Its fixed 10 dB setting produced 28.46% clipped samples. The full campaign below was measured with the original two-carrier profile. [Three-carrier fleet replay](runs/center_fleet_check/20261112_151636_uzx5qz_t/score.json) · [Historical hardware run](runs/20261112_152240_6l7w8vt8/summary.json)

## Original two-carrier campaign

The original acceptance suite passed all seven gates on Windows 11 with Python 3.13.17. Fourteen software tests passed, including a hidden-window GUI test that used **Start receiver** to replay the supplied recording and export the five packets. The positive RF waveforms in that campaign were synthetic. The scored comparisons used exact payload bytes, branch identity, positive CRCs and a ±15 ms capture-time window. [Acceptance JSON](validation/bench_runs/20261112_141208_zbo_w9ky/acceptance.json) · [Software test log](validation/bench_runs/20261112_141208_zbo_w9ky/unit_tests.txt)

## Acquisition and interoperability

| Input | Observed |
|---|---|
| EPFL-generated 55-byte frames with receiver-archive payloads | 3/3 isolated at SF10, SF11 and SF12. Both equal-power SF11 and SF12 packets starting together on A also passed. |
| Concurrent six-branch waveform | 6/6 on two carriers, three SFs per carrier, common preamble-start time |
| Motion and rate errors | Exact payload and PHY CRC at all three SFs with +11.511 kHz CFO, +118 Hz/s linear slew and separate −19 / +24 ppm time dilations |
| PHY versus application CRC | Correctly distinguished a corrupted on-air PHY CRC from a valid PHY frame carrying a bad application CRC. No cancellation donor admitted in either case. |
| Generic formats | Arbitrary binary bytes with an explicit header and absent PHY CRC, and a fixed-length implicit-header CRC packet |
| Discontinuities | Discarded a burst cut by a gap and recovered the next burst at its original capture time. Tested repeated odd-byte gaps, recorder pad bytes, an unpaired endpoint and a trailing gap. |
| Negative controls | Wrong sync word and seeded noise produced no packets. A 0.5 s LR-FHSS archive file with its original sidecar replayed successfully and produced zero LoRa packets. |
| Recorder under DSP overload | A deliberately full 16-block queue dropped 8,000 of 24,000 samples from the DSP path. The recorder retained all 24,000 with the expected hash. |

The largest absolute start-time error on the independent fixture and the six concurrent transmissions was one input sample (1 µs). This measures software timing against generated IQ. Physical TX-log alignment, radio filter delay and crystal offsets require a bench reference. [Independent example recording](examples/epfl_bench.iq) · [EPFL transmit truth](examples/epfl_bench.tx.csv) · [Archive replay summary](runs/reference_replay/20261112_140817_8louat_k/summary.json)

## Received-power sweep

At 914.875 MHz, SF11 and SF12 transmitted 55-byte packets. Each signed difference had two overlap trials. Starts were identical in one trial and 731 µs apart in the other. SF11 had −1,823 Hz CFO and SF12 had +2,244 Hz CFO. The stronger amplitude was held at 0.34 of ADC full scale and the weaker amplitude was attenuated digitally. Noise standard deviation was 0.012 per I/Q component. All 24 weaker-alone/equal-power controls passed. The ADC had zero rail hits.

| SF11 minus SF12 power | Primary SF11 | Primary SF12 | SIC SF11 | SIC SF12 |
|---:|---:|---:|---:|---:|
| −30 dB | 0/2 | 2/2 | 2/2 | 2/2 |
| −24 dB | 0/2 | 2/2 | 2/2 | 2/2 |
| −18 dB | 1/2 | 2/2 | 2/2 | 2/2 |
| −12, −6, 0, +6, +12, +18 dB | 2/2 each | 2/2 each | 2/2 each | 2/2 each |
| +24 dB | 2/2 | 0/2 | 2/2 | 2/2 |
| +30 dB | 2/2 | 0/2 | 2/2 | 2/2 |

The primary decoder recovered 35/44 overlapping packets. A second blind search after CRC-gated cancellation raised this to 44/44. Including isolated controls, recovery was 59/68 and 68/68. Both runs had zero false and duplicate matches on the same 157.631813 s recording. These are two observations per signed difference, with digital amplitudes. The physical weak-packet recovery curve is open. [Signed sweep and run paths](validation/bench_runs/20261112_141208_zbo_w9ky/acceptance.json)

## Fleet recovery and timing

The 93.2 s fixture contained three 30 s periods and a 3.2 s drain interval. Each period offered all 100 CSIDs once. Each carrier had 30 SF10, 12 SF11 and eight SF12 radios. Each node had a seeded random starting phase repeated across periods with ±35 ms jitter, a fixed relative level, a CFO drawn within ±7.5 kHz, and a per-packet linear slew within ±55 Hz/s. All six searches saw the same quantized ADC stream. The transmit CSV was opened only by the scorer after each decode.

| Input and mode | Valid / 300 | Unique IDs by full 30 s period | Periods ≥80 | Processing | Wall time | Local dropped samples |
|---|---:|---|---:|---:|---:|---:|
| Synthetic IQ, primary replay | 176 | 61, 56, 59 | 0/3 | 11.81 s | 12.04 s | 0 |
| Identical IQ, primary + one SIC replay pass | 256 | 91, 83, 82 | 3/3 | 32.26 s | 32.90 s | 0 |
| Identical IQ, synthetic paced rtl_tcp through live reader | 176 | 61, 56, 59 | 0/3 | 14.89 s | 93.25 s | 0 |

All three runs scored zero false and zero duplicate valid decodes. The primary run also exported 48 rejected observations. After merging SIC results, 21 rejected observations remained. The fleet waveform had 573 rail-hit samples out of 93,200,000 (0.000615%). Absolute timing error for matched packets was at most one sample. The single generated fleet and seed give a repeatable bench baseline. The three periods share node phases and are correlated.

The paced socket delivered deliberately odd-length writes through the complete RTL0 handshake, tuning and gain commands, reader, queue, recorder and decoder. The record had zero recorder drops and exactly matched the source SHA-256, `f09970b6374db9a348718df16ab1466d8edb42c7ebdbd4ac43214f2943af58e9`. The reader queue peaked at two blocks, and the largest measured outstanding interval after processing a block was 7.808 ms. The sampled backlog slope was zero. The Windows sandbox initially denied the localhost connection. The paced stage passed when rerun with localhost access using the previously scored IQ. [Live-path summary](validation/bench_runs/20261112_141208_zbo_w9ky/paced_socket/20261112_143551_k48w8zdu/summary.json)

## Physical bench status

librtlsdr enumerated zero devices and the live launcher reported “No supported devices found.” The user's earlier equal-power SF11/SF12 hardware result is the first comparison to reproduce. The workspace supplied no positive LoRa bench capture or flight-radio settings export. Physical sensitivity, gain setup on the actual tuner, upstream USB losses, Doppler/fading under real motion and a hardware 80-of-100 result are still unverified. The receiver README gives exact capture, replay, paired SIC and transmit-scoring commands. [Bench procedure](README.md#bench-acceptance)
