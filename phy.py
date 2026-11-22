"""Pinned pyLoRaPHY codec boundary and sample-indexed LoRa chirps."""
from functools import lru_cache
import numpy as np
from lora_phy.common import calc_sym_num
from lora_phy.rx import LoRaReceiver, diag_deinterleave
from lora_phy.tx import LoRaTransmitter, TxProcessing, diag_interleave


class Codec:
    def __init__(self, profile, carrier, sf):
        self.profile, self.sf = profile, sf
        self.rx = LoRaReceiver(carrier, sf, profile.bandwidth_hz, 2*profile.bandwidth_hz,
                               has_header=profile.explicit_header,
                               implicit_header_payload_len=profile.implicit_length,
                               implicit_header_coding_rate=profile.coding_rate_denominator-4,
                               implicit_header_enable_crc=profile.phy_crc)
        # The package has no public forced-LDRO or header-only API in 0.3.0.
        self.rx._proc.low_data_rate_optimization = profile.de(sf)

    def header(self, symbols):
        p = self.profile
        if p.explicit_header:
            # decode() expects already tracked symbols. _parse_header() in
            # 0.3.0 applies a second LDRO tracking pass and can disagree with
            # decode() under collisions. Use its exact header codeword path.
            gray = self.rx._proc.gray_coding(symbols.astype(np.float64))
            words = diag_deinterleave(gray, self.sf-2)
            nibbles = self.rx._proc.hamming_decode(words, 8)
            valid, length, cr, crc = self.rx._parse_header_inner(nibbles[:5])
            if not valid or not 1 <= cr <= 4 or not 1 <= length <= 255:
                return None
        else:
            length, cr, crc = p.implicit_length, p.coding_rate_denominator-4, p.phy_crc
        count = calc_sym_num(p.explicit_header, length, cr, crc, self.sf, p.de(self.sf))
        return int(length), int(cr), bool(crc), int(count)

    def payload(self, symbols, header):
        length, _, has_crc, _ = header
        data, expected = self.rx.decode(symbols)
        data = data.tobytes()
        if len(data) < length + 2*has_crc:
            raise ValueError("Incomplete decoded codeword block")
        return data[:length], data[length:length+2] == bytes(expected) if has_crc else None

    def soft_payload(self, symbols, magnitudes, header):
        """Retry the payload using FFT evidence for each bit of a LoRa codeword."""
        cr = header[1]
        block_size = 4 + cr
        if len(symbols) <= 8:
            return None
        ldro = self.profile.de(self.sf)
        width = self.sf - 2*int(ldro)
        codec = TxProcessing(self.sf, ldro)

        # Ask the same encoder for all valid codewords and symbol-bin labels.
        # Its leading header block uses a different coding rate and bin spacing.
        nibbles = np.r_[np.zeros(self.sf-2, dtype=np.uint8), np.arange(16, dtype=np.uint8)]
        words = codec.hamming_encode(nibbles, cr)[-16:]
        columns = np.arange(block_size)
        signs = 2 * ((words[:, None] >> columns) & 1).astype(np.float32) - 1
        labels = np.arange(1 << width, dtype=np.uint16)
        prefix = np.zeros(8, dtype=np.uint16)
        bins = codec.gray_decoding(np.r_[prefix, labels])[8:]
        bit_masks = (labels[:, None] >> np.arange(width) & 1).astype(bool)

        corrected = symbols.copy()
        for start in range(8, len(symbols), block_size):
            block = slice(start, start + block_size)
            peaks = magnitudes[block][:, bins]
            # Max-log bit evidence. Close competing peaks contribute less.
            evidence = np.stack([peaks[:, ones].max(axis=1) - peaks[:, ~ones].max(axis=1)
                                 for ones in bit_masks.T], axis=1)
            # Undo the diagonal rotation before scoring the 16 legal codewords.
            evidence = np.stack([np.roll(row, col) for col, row in enumerate(evidence)], axis=1)
            chosen = words[(evidence @ signs.T).argmax(axis=1)]
            interleaved = diag_interleave(chosen, block_size)
            corrected[block] = codec.gray_decoding(np.r_[prefix, interleaved])[8:]

        if not np.array_equal(corrected, symbols):
            data, ok = self.payload(corrected, header)
            if ok:
                return data
        return None


def encode(profile, sf, data, cr, crc):
    p = profile
    tx = LoRaTransmitter(sf, p.bandwidth_hz, 2*p.bandwidth_hz, has_header=p.explicit_header,
                         coding_rate=cr, enable_crc=crc, preamble_len=p.preamble_symbols)
    tx._proc.low_data_rate_optimization = p.de(sf)
    count = calc_sym_num(p.explicit_header, len(data), cr, crc, sf, p.de(sf))
    # The upstream encoder pads to a CR4/8 upper bound. Only on-air symbols belong here.
    return tx.encode(np.frombuffer(data, dtype=np.uint8).copy())[:count]


@lru_cache(maxsize=24)
def upchirp(sf, oversampling):
    n = 1 << sf
    chips = np.arange(n*oversampling, dtype=float) / oversampling
    return np.exp(1j*np.pi*chips*(chips/n-1)).astype(np.complex64)


def symbol_plan(profile, sf, symbols):
    """(length in chips, direction, cyclic shift), in on-air order."""
    n = 1 << sf
    sync = profile.sync_word
    return ([(n, 1, 0)] * profile.preamble_symbols +
            [(n, 1, (sync >> 4)*8), (n, 1, (sync & 15)*8),
             (n, -1, 0), (n, -1, 0), (n//4, -1, 0)] +
            [(n, 1, int(k)) for k in symbols])


def waveform(profile, sf, data, cr, crc, rate):
    """Clean complex baseband used by synthetic tests. RF is never transmitted."""
    os = rate // profile.bandwidth_hz
    ref = upchirp(sf, os)
    symbols = encode(profile, sf, data, cr, crc)
    plan = symbol_plan(profile, sf, symbols)
    result = np.empty(sum(length*os for length, _, _ in plan), dtype=np.complex64)
    pos = 0
    for length, direction, shift in plan:
        chirp = np.roll(ref, -shift*os) / ref[shift*os]
        if direction < 0:
            chirp = chirp.conj()
        end = pos + length*os
        result[pos:end] = chirp[:end-pos]
        pos = end
    return result
