# Windows bench validation · 12 November 2026

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
