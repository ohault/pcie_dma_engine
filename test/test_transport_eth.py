#!/usr/bin/env python3

# License: BSD

# Simulation test for the Ethernet (UDP) transport seam against the PCILeech engine.
#
# Regression guard for the endianness mismatch that made UDP port 28474 silent on hardware while
# ICMP still answered: LiteEth's dw=32 UDP port is little-endian (first wire byte -> dword LSB),
# but the PCILeech dword protocol (engine + host probe + LeechCore) is big-endian within a dword
# (first byte = MSB). EthTransport._bswap32 reconciles the two at the seam. The engine's own tests
# feed it dwords directly and never exercise this, so the seam needs its own test.
#
# This drives the *exact* host-side wire bytes through LiteEth's real StrideConverter (dw=8<->32,
# reverse=False) + _bswap32 into the engine and back, and checks the identity response the
# host probe / LeechCore expects (magic 0xab89).
# Run: python3 test/test_transport_eth.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migen import *

from litex.soc.interconnect import stream

from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine
from pcie_dma_engine.gateware.transport.eth import _bswap32

VERSION_MAJOR = 1
VERSION_MINOR = 0
DEVICE_ID     = 0x01


def read_cmd(addr, target=0x03):
    # Host-side 8-byte CORE read command (matches software/pcileech_udp_probe.py): magic at byte[7].
    return bytes([0x00, 0x00, 0x00, 0x00, (addr >> 8) & 0x3f, addr & 0xff, 0x10 | target, 0x77])


def parse_groups(data):
    # Parse 32-byte return groups as the host does (big-endian dwords); return {addr: value}.
    resp = {}
    for i in range(0, len(data) - 31, 32):
        status = int.from_bytes(data[i:i + 4], "big")
        if (status & 0xf0000000) != (0xe << 28):
            continue
        for k in range(7):
            nib = (status >> (4 * k)) & 0xf
            dw  = int.from_bytes(data[i + 4 + 4 * k:i + 8 + 4 * k], "big")
            if (nib & 0x3) == 3:  # PORT_COMMAND
                addr = (dw >> 16) & 0xffff
                bs   = dw & 0xffff
                resp[addr] = ((bs & 0xff) << 8) | ((bs >> 8) & 0xff)  # undo byteswap
    return resp


class _Seam(Module):
    # Host byte stream -> LiteEth RX converter -> _bswap32 -> engine -> _bswap32 -> TX converter.
    def __init__(self):
        self.submodules.rx  = rx  = stream.StrideConverter([("data", 8)], [("data", 32)], reverse=False)
        self.submodules.eng = eng = PCILeechEngine(VERSION_MAJOR, VERSION_MINOR, DEVICE_ID,
                                                   with_cfg_mgmt=False)
        self.submodules.tx  = tx  = stream.StrideConverter([("data", 32)], [("data", 8)], reverse=False)
        self.comb += [
            eng.sink.valid.eq(rx.source.valid),
            eng.sink.data.eq(_bswap32(rx.source.data)),
            rx.source.ready.eq(1),

            tx.sink.valid.eq(eng.source.valid),
            tx.sink.data.eq(_bswap32(eng.source.data)),
            tx.sink.last.eq(eng.source.last),
            eng.source.ready.eq(tx.sink.ready),
            tx.source.ready.eq(1),
        ]
        self.wire_in  = rx.sink
        self.wire_out = tx.source


def test_identity_over_liteeth_seam():
    dut  = _Seam()
    wire = b"".join(read_cmd(a) for a in (0x00, 0x08, 0x0a))
    out  = bytearray()

    def tb():
        yield dut.wire_in.valid.eq(1)
        for b in wire:
            yield dut.wire_in.data.eq(b)
            yield
            while not (yield dut.wire_in.ready):
                yield
        yield dut.wire_in.valid.eq(0)
        for _ in range(4000):
            if (yield dut.wire_out.valid):
                out.append((yield dut.wire_out.data))
            yield

    run_simulation(dut, tb())

    resp = parse_groups(bytes(out))
    assert resp.get(0x00) == 0xab89, f"magic {resp.get(0x00)} != 0xab89 (endianness seam broken)"
    assert (resp.get(0x08, 0) & 0xff) == VERSION_MAJOR
    assert ((resp.get(0x08, 0) >> 8) & 0xff) == VERSION_MINOR
    assert (resp.get(0x0a, 0) & 0xff) == DEVICE_ID
    print(f"[OK] UDP seam: magic=0xab89, version={VERSION_MAJOR}.{VERSION_MINOR}, "
          f"devid={DEVICE_ID:#04x} (big-endian protocol over little-endian LiteEth port)")


if __name__ == "__main__":
    test_identity_over_liteeth_seam()
    print("All Ethernet transport seam tests passed.")
