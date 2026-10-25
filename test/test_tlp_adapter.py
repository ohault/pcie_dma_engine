#!/usr/bin/env python3

# License: BSD

# Unit tests for the shared TLP width adapter (gateware/tlp_adapter.py): the 32<->64 conversion with
# correct byte-enables on odd-length injects, stale-tail masking on odd-length completions, and
# per-TLP first/last. Byte order and framing are the payload's job and are NOT exercised here.
# Run: python3 test/test_tlp_adapter.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migen import *

from pcie_dma_engine.gateware.tlp_adapter import TlpAdapter


def run_inject(ndw):
    """Feed ndw 32-bit dwords into inj; capture the (dat, be, last) beats emitted on phy_tx."""
    dut = TlpAdapter()
    beats = []

    def cap():
        if (yield dut.phy_tx.valid) and (yield dut.phy_tx.ready):
            beats.append(((yield dut.phy_tx.dat), (yield dut.phy_tx.be), (yield dut.phy_tx.last)))

    def tb():
        yield dut.phy_tx.ready.eq(1)
        for k in range(ndw):
            yield dut.inj.valid.eq(1)
            yield dut.inj.dat.eq(0x1000 + k)
            yield dut.inj.last.eq(1 if k == ndw - 1 else 0)
            yield
            yield from cap()
            while not (yield dut.inj.ready):
                yield
                yield from cap()
        yield dut.inj.valid.eq(0)
        for _ in range(20):
            yield
            yield from cap()

    run_simulation(dut, tb())
    return beats


def test_inject_byte_enables():
    # Odd-length TLP: last 64-bit beat has one valid dword -> be=0x0f. Even: be=0xff.
    beats = run_inject(3)
    assert beats[-1][1] == 0x0f, f"3-dword inject: last be={beats[-1][1]:#04x}, want 0x0f"
    assert (beats[-1][0] & 0xffffffff) == 0x1002, f"3-dword inject: last low dword {beats[-1][0]:#x}"
    beats = run_inject(4)
    assert beats[-1][1] == 0xff, f"4-dword inject: last be={beats[-1][1]:#04x}, want 0xff"
    print("[OK] adapter inject: odd TLP last beat be=0x0f, even be=0xff")


def run_return(ndw):
    """Pack ndw dwords into 64-bit beats (last beat partial if odd) and feed phy_rx; capture ret."""
    beats = []
    i = 0
    while i < ndw:
        low = 0xAA00 + i
        if i + 1 < ndw:
            dat = ((0xAA00 + i + 1) << 32) | low
            be = 0xff
            i += 2
        else:
            dat = low
            be = 0x0f
            i += 1
        beats.append((dat, be, 1 if i >= ndw else 0))

    dut = TlpAdapter()
    out = []

    def cap():
        if (yield dut.ret.valid) and (yield dut.ret.ready):
            out.append(((yield dut.ret.dat), (yield dut.ret.first), (yield dut.ret.last)))

    def tb():
        yield dut.ret.ready.eq(1)
        for dat, be, last in beats:
            yield dut.phy_rx.valid.eq(1)
            yield dut.phy_rx.dat.eq(dat)
            yield dut.phy_rx.be.eq(be)
            yield dut.phy_rx.last.eq(last)
            yield
            yield from cap()
            while not (yield dut.phy_rx.ready):
                yield
                yield from cap()
        yield dut.phy_rx.valid.eq(0)
        for _ in range(20):
            yield
            yield from cap()

    run_simulation(dut, tb())
    return out


def test_return_stale_tail_and_first_last():
    for ndw in (4, 5):  # even and odd (odd exercises the stale-tail drop)
        out = run_return(ndw)
        assert len(out) == ndw, f"{ndw}-dword completion returned {len(out)} dwords (stale tail?)"
        dats = [d for d, _, _ in out]
        assert dats == [0xAA00 + k for k in range(ndw)], f"return dwords (raw, no bswap): {[hex(x) for x in dats]}"
        firsts = [f for _, f, _ in out]
        lasts = [l for _, _, l in out]
        assert firsts[0] == 1 and sum(firsts) == 1, f"FIRST markers: {firsts}"
        assert lasts[-1] == 1 and sum(lasts) == 1, f"LAST markers: {lasts}"
    print("[OK] adapter return: exact dword count, no stale tail, first on first + last on last, raw bytes")


def main():
    test_inject_byte_enables()
    test_return_stale_tail_and_first_last()
    print("All TLP adapter tests passed.")


if __name__ == "__main__":
    main()
