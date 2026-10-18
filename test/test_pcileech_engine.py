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
    """Split an 8-byte command word into two transport dwords (big-endian within each dword,
    first 4 bytes first). This matches pcileech_eth RX (first byte -> MSB) + com pairing."""
    assert len(cmd8) == 8
    dw0 = (cmd8[0] << 24) | (cmd8[1] << 16) | (cmd8[2] << 8) | cmd8[3]
    dw1 = (cmd8[4] << 24) | (cmd8[5] << 16) | (cmd8[6] << 8) | cmd8[7]
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
    # Collect command-channel responses: {addr[31:16], byteswap(data)[15:0]}.
    resp = {}
    for nib, data in parse_groups(out):
        if (nib & 0x3) == PORT_COMMAND:
            addr = (data >> 16) & 0xffff
            bs   = data & 0xffff
            val  = ((bs & 0xff) << 8) | ((bs >> 8) & 0xff)  # undo byte-swap
            resp[addr] = val

    assert resp.get(0x00) == RO_MAGIC, f"magic: got {resp.get(0x00):#06x} exp {RO_MAGIC:#06x}"
    # version word at byte 0x08 = {minor[15:8], major[7:0]}
    exp_ver = (VERSION_MINOR << 8) | VERSION_MAJOR
    assert resp.get(0x08) == exp_ver, f"version: got {resp.get(0x08):#06x} exp {exp_ver:#06x}"
    # device id at byte 0x0A (low byte)
    assert (resp.get(0x0a, 0) & 0xff) == DEVICE_ID, f"devid: got {resp.get(0x0a):#06x} exp {DEVICE_ID:#04x}"
    print(f"[OK] identity reads: magic=0x{resp[0x00]:04x}, version=0x{resp[0x08]:04x}, devid=0x{resp[0x0a]:04x}")


def tlp_dwords(d, last):
    """A host->FPGA TLP inject word (type 00, magic 0x77, data in [63:32], last in [10]) as two
    transport dwords (first dword = high half)."""
    w = (d << 32) | (last << 10) | CMD_MAGIC  # type 00 (TLP), magic 0x77
    return [(w >> 32) & 0xffffffff, w & 0xffffffff]


class _TlpLoopbackDUT(Module):
    # PCILeechEngine with the PCIe side looped (TLPs sent to the bus come back as received).
    def __init__(self):
        from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine
        self.submodules.core = core = PCILeechEngine()
        self.sink   = core.sink
        self.source = core.source
        self.comb += core.tlp_sink.connect(core.tlp_source)


def test_tlp_loopback():
    from pcie_dma_engine.gateware.pcileech.common import PORT_TLP
    dut = _TlpLoopbackDUT()
    tlp = [(0xaabbccdd, 0), (0x11223344, 1)]  # a 2-dword TLP: dword0 (not last), dword1 (last)
    dwords = []
    for d, last in tlp:
        dwords += tlp_dwords(d, last)
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
    assert len(got) == 2, f"expected 2 TLP dwords back, got {len(got)}: {[(n, hex(d)) for n, d in got]}"
    (n0, d0), (n1, d1) = got
    assert d0 == 0xaabbccdd and (n0 >> 2) == 0, f"dword0: {hex(d0)} nibble {n0:#x}"
    assert d1 == 0x11223344 and (n1 >> 2) == 1, f"dword1 (last): {hex(d1)} nibble {n1:#x}"
    print("[OK] TLP loopback: 2-dword TLP round-trips with PORT_TLP nibble + last on dword1")


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
            addr = (data >> 16) & 0xffff
            bs   = data & 0xffff
            val  = ((bs & 0xff) << 8) | ((bs >> 8) & 0xff)
            resp[addr & 0x7fff] = val  # mask off the RW-select bit in the echoed address
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


class _CfgMgmtDUT(Module):
    # PCILeechEngine with cfg_mgmt enabled and a 1-cycle fake cfg_mgmt responder.
    def __init__(self, canned=0xdead0666):
        from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine
        self.submodules.core = core = PCILeechEngine(with_cfg_mgmt=True)
        self.sink   = core.sink
        self.source = core.source
        self.comb += [core.tlp_source.valid.eq(0), core.tlp_sink.ready.eq(1)]
        done = Signal()
        self.sync += done.eq(core.cfg_mgmt_read & ~done)  # 1-cycle done pulse while read asserted
        self.comb += [
            core.cfg_mgmt_read_write_done.eq(done),
            core.cfg_mgmt_read_data.eq(canned),
        ]


def test_cfg_mgmt_bridge():
    from pcie_dma_engine.gateware.pcileech.common import PORT_CFG
    dut = _CfgMgmtDUT(canned=0xdead0666)
    dwords = []
    for dw in (cmd_dwords(pcie_read_cmd(0x0008)),):  # read PCIe device-id dword
        dwords += dw
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
            addr = (data >> 16) & 0xffff
            bs   = data & 0xffff
            cfg[addr] = ((bs & 0xff) << 8) | ((bs >> 8) & 0xff)  # undo byteswap
    # addr 0x0008, addr_lo=0 -> high half of 0xdead0666 = 0xdead
    assert cfg.get(0x0008) == 0xdead, f"cfg reply: {cfg}"
    print("[OK] cfg_mgmt bridge: PCIe config read 0x0008 -> 0xdead (via cfg_mgmt, CFG channel)")


def main():
    test_identity_reads()
    test_tlp_loopback()
    test_register_write_readback()
    test_shadow_write_readback()
    test_cfg_mgmt_bridge()
    print("All PCILeech engine tests passed.")


if __name__ == "__main__":
    main()
