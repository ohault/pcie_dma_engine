#!/usr/bin/env python3

# License: BSD

# Simulation test for the PCILeech control plane (command parser + register file + response framer).
# Feeds host command words as 32-bit dwords (big-endian within a dword, first dword -> high half of
# the 64-bit command word) and checks the framed register-read responses.
# Run: python3 test/test_pcileech_control.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migen import *

from pcie_dma_engine.gateware.pcileech.common import RO_MAGIC, FRAME_MARKER, PORT_COMMAND, PORT_TLP, CMD_MAGIC
from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine

VERSION_MAJOR = 0x02
VERSION_MINOR = 0x05
DEVICE_ID     = 0x42


def cmd_dwords(cmd8):
    """Split an 8-byte command word into two little-endian wire dwords, exactly as the host puts them
    on the wire and the opaque transport delivers them to the engine's sink (which normalizes byte
    order internally). First 4 bytes -> first dword."""
    assert len(cmd8) == 8
    dw0 = cmd8[0] | (cmd8[1] << 8) | (cmd8[2] << 16) | (cmd8[3] << 24)
    dw1 = cmd8[4] | (cmd8[5] << 8) | (cmd8[6] << 16) | (cmd8[7] << 24)
    return [dw0, dw1]


def read_cmd(addr, target=0x03):
    """Build an 8-byte CORE read command for a byte address (RO file)."""
    # byte layout: [0:1]=value, [2:3]=mask, [4]=addr_hi|flags, [5]=addr_lo, [6]=type|target, [7]=0x77
    return bytes([0x00, 0x00, 0x00, 0x00, (addr >> 8) & 0x3f, addr & 0xff, 0x10 | target, 0x77])


def run(cmds):
    dut = PCILeechEngine(version_major=VERSION_MAJOR, version_minor=VERSION_MINOR, device_id=DEVICE_ID)
    out = []

    # Flatten all command words into a dword stream.
    dwords = []
    for c in cmds:
        dwords += cmd_dwords(c)

    def gen():
        yield dut.source.ready.eq(1)
        # Feed the command dwords.
        for dw in dwords:
            yield dut.sink.data.eq(dw)
            yield dut.sink.valid.eq(1)
            yield
        yield dut.sink.valid.eq(0)
        # Let the framer flush (its idle timeout) and collect the output.
        for _ in range(4000):
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, gen())
    return out


def parse_groups(dwords):
    """Yield (nibble_i, data_i) for each data word in each valid 0xE group."""
    i = 0
    while i + 8 <= len(dwords):
        status = dwords[i]
        if (status & 0xf0000000) == (FRAME_MARKER << 28):
            for k in range(7):
                nib = (status >> (4 * k)) & 0xf
                yield nib, dwords[i + 1 + k]
        i += 8


def test_identity_reads():
    # Read magic (offset 0x00), version (0x08), device id (0x0A).
    out = run([read_cmd(0x00), read_cmd(0x08), read_cmd(0x0a)])
    # Collect command-channel responses: {data[31:16], byteswap(addr)[15:0]} (LeechCore layout).
    resp = {}
    for nib, data in parse_groups(out):
        if (nib & 0x3) == PORT_COMMAND:
            lo   = data & 0xffff
            addr = ((lo & 0xff) << 8) | ((lo >> 8) & 0xff)  # addr = byteswap(low 16)
            resp[addr] = (data >> 16) & 0xffff              # data = high 16

    assert resp.get(0x00) == RO_MAGIC, f"magic: got {resp.get(0x00):#06x} exp {RO_MAGIC:#06x}"
    # version word at byte 0x08 = {minor[15:8], major[7:0]}
    exp_ver = (VERSION_MINOR << 8) | VERSION_MAJOR
    assert resp.get(0x08) == exp_ver, f"version: got {resp.get(0x08):#06x} exp {exp_ver:#06x}"
    # device id at byte 0x0A (low byte)
    assert (resp.get(0x0a, 0) & 0xff) == DEVICE_ID, f"devid: got {resp.get(0x0a):#06x} exp {DEVICE_ID:#04x}"
    print(f"[OK] identity reads: magic=0x{resp[0x00]:04x}, version=0x{resp[0x08]:04x}, devid=0x{resp[0x0a]:04x}")


def tlp_dwords(d, last):
    """A host->FPGA TLP inject word (type 00, magic 0x77, data in [63:32], last in [10]) as two
    little-endian wire dwords (first dword = high half), as the opaque transport delivers them."""
    w = (d << 32) | (last << 10) | CMD_MAGIC  # type 00 (TLP), magic 0x77
    return [bswap32((w >> 32) & 0xffffffff), bswap32(w & 0xffffffff)]


class _TlpLoopbackDUT(Module):
    # PCILeechEngine with the PCIe side looped (TLPs sent to the bus come back as received).
    def __init__(self):
        from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine
        self.submodules.core = core = PCILeechEngine()
        self.sink   = core.sink
        self.source = core.source
        self.comb += core.tlp_sink.connect(core.tlp_source)


def bswap32(x):
    return ((x & 0xff) << 24) | ((x & 0xff00) << 8) | ((x >> 8) & 0xff00) | ((x >> 24) & 0xff)


def test_tlp_loopback():
    # A returned completion carries source nibbles LeechCore decodes as {FIRST=bit3, LAST=bit2,
    # port=bits[1:0]=PORT_TLP}: the first dword must be FIRST-marked (v4.13+ ignores TLPs otherwise)
    # and the last LAST-marked. Every dword (header and payload) goes out byte-swapped so both the
    # header parses and the payload lands in host memory order. Loop a 4-dword TLP and check all of it.
    from pcie_dma_engine.gateware.pcileech.common import PORT_TLP
    dut = _TlpLoopbackDUT()
    tlp = [0xaabbccdd, 0x11223344, 0x55667788, 0x99aabbcc]  # [h0, h1, h2, payload]
    dwords = []
    for i, d in enumerate(tlp):
        dwords += tlp_dwords(d, 1 if i == len(tlp) - 1 else 0)
    out = []

    def gen():
        yield dut.source.ready.eq(1)
        for dw in dwords:
            yield dut.sink.data.eq(dw)
            yield dut.sink.valid.eq(1)
            yield
        yield dut.sink.valid.eq(0)
        for _ in range(4000):
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, gen())

    got = [(nib, data) for nib, data in parse_groups(out) if (nib & 0x3) == PORT_TLP]
    assert len(got) == 4, f"expected 4 TLP dwords back, got {len(got)}: {[(n, hex(d)) for n, d in got]}"
    (n0, d0), (n1, d1), (n2, d2), (n3, d3) = got
    assert d0 == bswap32(tlp[0]) and (n0 & 0x8), f"h0: {hex(d0)} nibble {n0:#x} (want FIRST+bswap)"
    assert d1 == bswap32(tlp[1]) and n1 == 0,    f"h1: {hex(d1)} nibble {n1:#x}"
    assert d2 == bswap32(tlp[2]) and n2 == 0,    f"h2: {hex(d2)} nibble {n2:#x}"
    assert d3 == bswap32(tlp[3]) and (n3 & 0x4), f"payload: {hex(d3)} nibble {n3:#x} (want LAST+bswap)"
    print("[OK] TLP loopback: FIRST/LAST markers, all dwords byte-swapped")


def test_tlp_loopback_odd():
    # A completion with an odd dword count (3-DW header + odd payload) ends on a 64-bit beat with one
    # valid dword. The return path must emit exactly that many dwords -- no stale trailing dword -- or
    # LeechCore rejects the TLP (received length != header Length). Loop a 5-dword TLP and require
    # exactly 5 dwords back, FIRST on the first and LAST on the fifth.
    from pcie_dma_engine.gateware.pcileech.common import PORT_TLP
    dut = _TlpLoopbackDUT()
    tlp = [0x11111111, 0x22222222, 0x33333333, 0x44444444, 0x55555555]  # 5 dwords (odd)
    dwords = []
    for i, d in enumerate(tlp):
        dwords += tlp_dwords(d, 1 if i == len(tlp) - 1 else 0)
    out = []

    def gen():
        yield dut.source.ready.eq(1)
        for dw in dwords:
            yield dut.sink.data.eq(dw)
            yield dut.sink.valid.eq(1)
            yield
        yield dut.sink.valid.eq(0)
        for _ in range(4000):
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, gen())
    got = [(nib, data) for nib, data in parse_groups(out) if (nib & 0x3) == PORT_TLP]
    assert len(got) == 5, f"expected exactly 5 TLP dwords (no stale), got {len(got)}: {[hex(d) for _, d in got]}"
    assert got[0][1] == bswap32(tlp[0]) and (got[0][0] & 0x8), f"first: {got[0]}"
    assert got[4][1] == bswap32(tlp[4]) and (got[4][0] & 0x4), f"last: {got[4]}"
    print("[OK] TLP loopback (odd): 5-dword TLP returns exactly 5 dwords, no stale tail")


def write_cmd(addr, value, mask=0xffff):
    """Build an 8-byte CORE RW write command for a byte address (RW file, addr bit15 set)."""
    # [0:1]=value LE, [2:3]=mask LE, [4]=addr_hi|0x80 (RW), [5]=addr_lo, [6]=0x20|target, [7]=0x77
    return bytes([value & 0xff, (value >> 8) & 0xff, mask & 0xff, (mask >> 8) & 0xff,
                  ((addr >> 8) & 0x3f) | 0x80, addr & 0xff, 0x20 | 0x03, 0x77])


def read_rw_cmd(addr):
    """Read a CORE RW register (addr bit15 set selects the RW file)."""
    return bytes([0x00, 0x00, 0x00, 0x00, ((addr >> 8) & 0x3f) | 0x80, addr & 0xff, 0x10 | 0x03, 0x77])


def test_register_write_readback():
    # Write 0xBEEF to RW byte offset 0x10, then read it back.
    out = run([write_cmd(0x10, 0xbeef), read_rw_cmd(0x10)])
    resp = {}
    for nib, data in parse_groups(out):
        if (nib & 0x3) == PORT_COMMAND:
            lo   = data & 0xffff
            addr = ((lo & 0xff) << 8) | ((lo >> 8) & 0xff)  # addr = byteswap(low 16)
            resp[addr & 0x7fff] = (data >> 16) & 0xffff     # mask off the RW-select bit
    assert resp.get(0x10) == 0xbeef, f"RW readback: got {resp.get(0x10)} exp 0xbeef"
    print("[OK] register write/readback: RW[0x10] = 0xbeef")


def shadow_write(dword, addr_lo, value):
    # Shadow write: address bit15 (RW) + bit14 (shadow); dword at [11:2], half-select at [1].
    addr16 = 0xc000 | (dword << 2) | (addr_lo << 1)
    return bytes([value & 0xff, (value >> 8) & 0xff, 0xff, 0xff,
                  (addr16 >> 8) & 0xff, addr16 & 0xff, 0x20 | 0x03, 0x77])


def shadow_read(dword, addr_lo):
    addr16 = 0x4000 | (dword << 2) | (addr_lo << 1)  # shadow (bit14), read
    return bytes([0, 0, 0, 0, (addr16 >> 8) & 0xff, addr16 & 0xff, 0x10 | 0x03, 0x77])


def collect_shadow(out):
    sh = {}
    for nib, data in parse_groups(out):
        if (nib & 0x3) == PORT_COMMAND and ((data >> 28) & 0xf) == 0xc:  # shadow tag 0xC...
            sh[data >> 16] = data & 0xffff  # {tag: half} (no byteswap)
    return sh


def test_shadow_write_readback():
    out = collect_shadow(run([
        shadow_write(5, 1, 0x1234), shadow_write(5, 0, 0x5678),
        shadow_read(5, 1),          shadow_read(5, 0),
    ]))
    tag_lo = 0xc000 | (5 << 2) | (1 << 1)  # 0xC016
    tag_hi = 0xc000 | (5 << 2) | (0 << 1)  # 0xC014
    assert out.get(tag_lo) == 0x1234, f"shadow[5].lo: {out.get(tag_lo)}"
    assert out.get(tag_hi) == 0x5678, f"shadow[5].hi: {out.get(tag_hi)}"
    print("[OK] shadow config write/readback: dword5 = 0x5678_1234")


def pcie_read_cmd(addr):
    """8-byte PCIe (target 01) config read command for a byte address."""
    return bytes([0x00, 0x00, 0x00, 0x00, (addr >> 8) & 0x3f, addr & 0xff, 0x10 | 0x01, 0x77])


class _PcieIdDUT(Module):
    # PCILeechEngine with the PCIe register bank enabled and a fixed enumerated requester id.
    def __init__(self, pcie_id=0x0600):
        from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine
        self.submodules.core = core = PCILeechEngine(with_cfg_mgmt=True)
        self.sink   = core.sink
        self.source = core.source
        self.comb += [
            core.tlp_source.valid.eq(0), core.tlp_sink.ready.eq(1),
            core.pcie_id.eq(pcie_id),  # bus/device/function = Cat(function, device, bus)
        ]


def test_pcie_id_register():
    # LeechCore reads the "Device PCIe ID" (requester id) from FPGA_REG_PCIE 0x0008 on the CFG
    # channel. For bus=6, dev=0, fn=0 the id is 0x0600; the 16-bit read at 0x0008 is {bus, devfn}
    # = 0x0006, which LeechCore byte-swaps back to 0x0600.
    from pcie_dma_engine.gateware.pcileech.common import PORT_CFG
    dut = _PcieIdDUT(pcie_id=0x0600)
    dwords = cmd_dwords(pcie_read_cmd(0x0008))
    out = []

    def gen():
        yield dut.source.ready.eq(1)
        for dw in dwords:
            yield dut.sink.data.eq(dw)
            yield dut.sink.valid.eq(1)
            yield
        yield dut.sink.valid.eq(0)
        for _ in range(4000):
            if (yield dut.source.valid) and (yield dut.source.ready):
                out.append((yield dut.source.data))
            yield

    run_simulation(dut, gen())
    cfg = {}
    for nib, data in parse_groups(out):
        if (nib & 0x3) == PORT_CFG:
            lo   = data & 0xffff
            addr = ((lo & 0xff) << 8) | ((lo >> 8) & 0xff)  # addr = byteswap(low 16)
            cfg[addr] = (data >> 16) & 0xffff                # data = high 16
    assert cfg.get(0x0008) == 0x0006, f"pcie id reply: {cfg}"           # {bus=6, devfn=0}
    bdf = ((cfg[0x0008] & 0xff) << 8) | ((cfg[0x0008] >> 8) & 0xff)     # host byte-swaps -> 0x0600
    assert bdf == 0x0600, f"bdf: {bdf:#06x}"
    print("[OK] PCIe id register: PCIE 0x0008 -> bus:dev.fn 06:00.0 (0x0600, CFG channel)")


class _TlpInjectDUT(Module):
    # PCILeechEngine exposing the raw TLP bus sink so a test can observe the beats (dat/be/last)
    # that would reach the 7-series PCIe core.
    def __init__(self):
        from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine
        self.submodules.core = core = PCILeechEngine()
        self.sink = core.sink
        self.tlp  = core.tlp_sink
        self.comb += core.tlp_source.valid.eq(0)


def test_tlp_inject_odd_be():
    # A 3-dword MRd (odd dword count) must leave the engine as exactly 3 dwords: the last 64-bit beat
    # carries one valid dword, so its byte-enables must be 0x0f (not 0xff with a stale 4th dword, which
    # the PCIe root port rejects as a Malformed TLP). A 4-dword TLP's last beat must stay be=0xff.
    for tlp in ([0x00000000, 0x060080ff, 0x00001000], [0xa, 0xb, 0xc, 0xd]):
        odd = len(tlp) % 2 == 1
        dwords = []
        for i, d in enumerate(tlp):
            dwords += tlp_dwords(d, 1 if i == len(tlp) - 1 else 0)
        beats = []

        def gen():
            yield dut.tlp.ready.eq(1)
            for dw in dwords:
                yield dut.sink.data.eq(dw)
                yield dut.sink.valid.eq(1)
                yield
            yield dut.sink.valid.eq(0)
            for _ in range(200):
                if (yield dut.tlp.valid) and (yield dut.tlp.ready):
                    beats.append(((yield dut.tlp.dat), (yield dut.tlp.be), (yield dut.tlp.last)))
                yield

        dut = _TlpInjectDUT()
        run_simulation(dut, gen())
        # Reconstruct the dwords actually placed on the bus (per last-beat byte-enables).
        bus = []
        for dat, be, last in beats:
            bus.append(dat & 0xffffffff)
            if be & 0xf0:
                bus.append((dat >> 32) & 0xffffffff)
        assert bus == tlp, f"bus dwords {[hex(x) for x in bus]} != TLP {[hex(x) for x in tlp]}"
        last_be = beats[-1][1]
        assert last_be == (0x0f if odd else 0xff), f"last beat be={last_be:#04x} (odd={odd})"
    print("[OK] TLP inject byte-enables: 3-DW MRd -> 3 dwords (be=0x0f), 4-DW -> be=0xff")


def main():
    test_identity_reads()
    test_tlp_loopback()
    test_tlp_loopback_odd()
    test_tlp_inject_odd_be()
    test_register_write_readback()
    test_shadow_write_readback()
    test_pcie_id_register()
    print("All PCILeech engine tests passed.")


if __name__ == "__main__":
    main()
