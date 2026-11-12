"""SPDX-License-Identifier: GPL-3.0-only
Synthetic interoperability fixture from EPFL gr-lora_sdr a8143cb. No RF sink.
Adapted flowgraph wiring from lora_sdr_lora_tx.py (Joachim Tapparel, EPFL).
Uses hexadecimal PMT input for arbitrary binary payloads and a bounded file sink.
Run using radioconda Python, separately from the receiver environment.
"""
import argparse
import time
from pathlib import Path
from gnuradio import blocks, gr, lora_sdr
import pmt


def generate(path, sf, payload_hex, cr):
    tb = gr.top_block()
    src = lora_sdr.whitening(True, False, ",", "packet_len")
    mod = lora_sdr.modulate(sf, 250000, 125000, [0x12], 0, 8)
    sink = blocks.file_sink(gr.sizeof_gr_complex, str(path), False)
    chain = [src, lora_sdr.header(False, True, cr), lora_sdr.add_crc(True),
             lora_sdr.hamming_enc(cr, sf), lora_sdr.interleaver(cr, sf, 2, 125000),
             lora_sdr.gray_demap(sf), mod, sink]
    tb.connect(*chain)
    # SF12 at 2 samples/chip needs space for a complete 8192-item output symbol.
    mod.set_min_output_buffer(131072)
    tb.start()
    try:
        src.to_basic_block()._post(pmt.intern("msg"), pmt.intern(payload_hex))
        payload_length = len(bytes.fromhex(payload_hex))
        from math import ceil
        de = int((1 << sf)/125000 > .016)
        nsym = 8 + max(0, ceil((8*payload_length - 4*sf + 28 + 16)/(4*(sf-2*de))))*(4+cr)
        goal = round((12.25+nsym)*(2 << sf))
        deadline = time.monotonic()+12
        while mod.nitems_written(0) < goal:
            if time.monotonic() > deadline:
                raise RuntimeError(f"EPFL output incomplete: {mod.nitems_written(0)} of {goal}")
            time.sleep(.02)
        while sink.nitems_read(0) < goal:
            if time.monotonic() > deadline:
                raise RuntimeError("File sink did not drain")
            time.sleep(.01)
    finally:
        tb.stop()
        tb.wait()
        sink.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("sf", type=int)
    parser.add_argument("hex")
    parser.add_argument("--cr", type=int, default=3)
    args = parser.parse_args()
    generate(args.output, args.sf, args.hex, args.cr)
