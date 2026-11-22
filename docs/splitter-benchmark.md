# SDR and T-Beam splitter benchmark

Measure recovery against the same offered transmissions at both receiver inputs. Use a separate run directory for each hardware condition, port assignment and PHY setting. Save the TX truth, SDR CU8 and sidecar, primary journal, raw T-Beam serial log and all settings together.

## RF setup

1. Feed a cabled packet source through calibrated attenuation into a two-way splitter. Put the splitter **before the LaNA**. One arm goes through the USB-powered LaNA and any field filter into the SDR. The other feeds the T-Beam. At the test frequency measure each cable, adapter and splitter port's insertion loss. Record loss, measured source power and attenuation per step so input dBm can be referred to each arm. Record LaNA gain separately. Swap the splitter ports, remeasure and repeat.
2. Verify the programmed carrier, SF, BW, sync word, explicit/implicit header and length, CR, PHY CRC, preamble length, IQ polarity and low-data-rate optimization on both ends. Record each device/firmware, antenna-port configuration, SDR sample rate, tuner gain and ppm. With one T-Beam compare one SF at a time. For simultaneous SF11/SF12 use two matched reference receivers or separate repeatable runs for each reference SF.
3. With TX active, disconnect and terminate the receiver feeds. Require **zero decodes** on both arms before accepting the shielding check. With TX quiet and the SDR input terminated through the intended front end, compare channel-floor traces at the same tuner gain with the LaNA on laptop USB and on a battery bank. Use the quieter supply and record it in Receiver Setup. Repeat the quiet trace after reconnecting the field chain.
4. Use widely spaced isolated packets for the sensitivity sweep. Establish a strong all-heard control, then step attenuation by **1 dB near the limit**. Record every actual TX, including those heard by neither receiver. Give each transmission distinct on-air bytes, for example a run ID, node ID and nonrepeating counter. Keep the tuner gain fixed across steps after any initial Auto Set. Record CU8 throughout and retain the original IQ hash so later decoder builds can be compared on identical samples. For estimating 1% PER use at least 300 transmissions per point and inspect the interval width.
5. Repeat without the LaNA, with a controlled blocker about 1 MHz away, with a stronger overlapping same-SF LoRa signal 125 kHz away, and with an equal-power overlapping SF11/SF12 pair. Measure and record wanted and interferer powers independently, their frequency and overlap. Include isolated controls at those powers. Keep the hardware variants, port swaps, interference levels and primary/SIC journal results in separate reports.

## Logs and scoring

TX CSV needs `attenuation_db,payload_hex`. It also accepts the existing `scheduled_payload_hex` column. Include `start_s,carrier_hz,sf,tx_id` when available. Use the SDR's native `packets.csv`. Configure the T-Beam firmware to log every valid packet's **full payload and PHY CRC status** to serial. This example records one SF11 packet.

```text
12.561 RX phy_crc_ok=true carrier_hz=914875000 sf=11 payload_hex=08000023
```

Serial CSV with a `phy_crc_ok,payload_hex` header and JSON Lines with the same keys also work. An empty serial capture represents zero receptions after you have verified its logger with the strong control. Prefix non-RX text with `#`. The leading serial timestamp above is informational. Clock matching uses an explicit `rx_start_s` or `capture_s` measured at preamble start. Account for packet airtime and UART delay if your firmware timestamps RX-done.

```powershell
.\.venv\Scripts\python.exe tools\splitter_bench.py tx.csv sdr\packets.csv tbeam.log --out steps.json --joined paired.csv
```

A match requires a positive PHY CRC and exact bytes. Logged carrier/SF values must agree when present in both logs. This compares generic PHY recovery, including payloads without DeSCENT framing. The scorer counts duplicate receipts once and reports extra valid receptions. When bytes repeat, provide a matching `tx_id` or enable aligned-clock matching for that receiver, for example `--sdr-offset-s 1.25 --tbeam-offset-s 0 --tolerance-s 0.05`. The offset is the receiver-clock time at TX time zero. Ambiguous matches stop scoring.

At each attenuation the four counts sum to the number offered. Each count and each receiver's marginal reception rate has a pointwise, two-sided 95% Wilson score interval using all offered transmissions as its denominator. These intervals describe proportions at that step. The per-TX join preserves the pairing for analysis of disagreements. [NIST formula for Wilson score limits](https://www.itl.nist.gov/div898/handbook/prc/section2/prc241.htm)
