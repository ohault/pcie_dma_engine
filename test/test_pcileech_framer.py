#!/usr/bin/env python3

# License: BSD

# Simulation tests for the PCILeech return-path framer and the command-word codec.
# Run: python3 test/test_pcileech_framer.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migen import *

from pcie_dma_engine.gateware.pcileech.common import (
    GROUP_DATA_DWORDS, FILLER_DWORD, FILLER_NIBBLE,
    encode_command, decode_command, status_dword,
    FPGA_REG_CORE, FPGA_REG_PCIE, FPGA_REG_READWRITE, FPGA_REG_SHADOWCFGSPACE, CMD_MAGIC,
)
from pcie_dma_engine.gateware.pcileech.framer import PCILeechFramer


def run_framer(beats, timeout=16, max_cycles=400):
    """Feed (data, nibble) beats into the framer and return the emitted dword list."""
    dut = PCILeechFramer(timeout=timeout)
    out = []

    def gen():
        yield dut.source.ready.eq(1)
        i = 0
        idle = 0
        for _ in range(max_cycles):
            # Drive the next beat (one at a time).
            if i < len(beats):
                yield dut.sink.data.eq(beats[i][0])
                yield dut.sink.nibble.eq(beats[i][1])
                yield dut.sink.valid.eq(1)
            else:
                yield dut.sink.valid.eq(0)
            yield
            if i < len(beats) and (yield dut.sink.ready) and (yield dut.sink.valid):
                i += 1
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            # Stop once all beats are consumed and the output has drained.
            if i >= len(beats):
                idle += 1
                if idle > timeout + GROUP_DATA_DWORDS + 8:
                    break

    run_simulation(dut, gen())
    return out


def test_full_group():
    beats = [(0x11111111 * 0 + (0x10 + k), (k % 4)) for k in range(GROUP_DATA_DWORDS)]
    # distinct data + nibbles
    beats = [(0xA0000000 | k, k & 0x3) for k in range(GROUP_DATA_DWORDS)]
    out = run_framer(beats)
    exp_status = status_dword([b[1] for b in beats])
    expected = [exp_status] + [b[0] for b in beats]
    assert out[:8] == expected, f"\n got={[hex(x) for x in out[:8]]}\n exp={[hex(x) for x in expected]}"
    assert (out[0] & 0xf0000000) == 0xe0000000, "status marker nibble must be 0xE"
    print("[OK] full group: status + 7 data, correct nibbles/marker")


def test_partial_group_filler():
    beats = [(0xB0000000 | k, 1) for k in range(3)]  # only 3 of 7 -> timeout flush
    out = run_framer(beats)
    nibbles = [1, 1, 1] + [FILLER_NIBBLE] * (GROUP_DATA_DWORDS - 3)
    exp_status = status_dword([1, 1, 1])  # unused slots default to filler nibble in status_dword
    expected = [exp_status] + [b[0] for b in beats] + [FILLER_DWORD] * (GROUP_DATA_DWORDS - 3)
    assert out[:8] == expected, f"\n got={[hex(x) for x in out[:8]]}\n exp={[hex(x) for x in expected]}"
    print("[OK] partial group: padded with filler words/nibbles after timeout")


def test_command_codec():
    # Round-trip a few commands and check known byte patterns from the design doc.
    cases = [
        dict(addr=0x0008, value=0x0000, write=False, bank=FPGA_REG_CORE),
        dict(addr=0x0002, value=0x0001, write=True,  bank=FPGA_REG_CORE | FPGA_REG_READWRITE),
        dict(addr=0x1234, value=0xbeef, write=True,  bank=FPGA_REG_PCIE | FPGA_REG_SHADOWCFGSPACE),
    ]
    for c in cases:
        b = encode_command(**c)
        assert b[7] == CMD_MAGIC
        d = decode_command(b)
        assert d["addr"]  == c["addr"],  (hex(d["addr"]),  hex(c["addr"]))
        assert d["value"] == c["value"], (hex(d["value"]), hex(c["value"]))
        assert d["write"] == c["write"], (d["write"], c["write"])

    # Known keepalive from the doc: write CORE RW 0x02 value 0x0001 -> 01 00 01 00 80 02 23 77
    ka = encode_command(addr=0x02, value=0x0001, mask=0x0001, write=True,
                        bank=FPGA_REG_CORE | FPGA_REG_READWRITE)
    assert ka == bytes([0x01, 0x00, 0x01, 0x00, 0x80, 0x02, 0x23, 0x77]), ka.hex()
    print("[OK] command codec: round-trip + doc keepalive byte pattern 01 00 01 00 80 02 23 77")


def main():
    test_full_group()
    test_partial_group_filler()
    test_command_codec()
    print("All PCILeech framer/codec tests passed.")


if __name__ == "__main__":
    main()
