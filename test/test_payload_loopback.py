#!/usr/bin/env python3

# License: BSD

# Conformance harness for the unified transport/payload seam (doc/unified_tlp_architecture.md).
# Pairs a bare HostTransport (the ❶ dword seam, no link logic) with the LoopbackPayload via
# connect_payload() and checks dwords driven into the transport's RX come back out its TX unchanged.
# This is the pattern a hardware-less transport (usb3_pipe Usb3Transport, FT601 USBTransport) uses to
# sim-validate its seam: swap the bare HostTransport for the real one and drive its host-facing side.
# Run: python3 test/test_payload_loopback.py

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from migen import *

from pcie_dma_engine.gateware.transport.base import HostTransport, connect_payload
from pcie_dma_engine.gateware.payload import LoopbackPayload


class _Rig(Module):
    # A bare transport seam + the loopback payload, wired with the production connect_payload() helper.
    def __init__(self):
        self.submodules.tr = tr = HostTransport()
        self.submodules.pl = pl = LoopbackPayload()
        self.comb += connect_payload(tr, pl)


def test_loopback_round_trip():
    dut = _Rig()
    data = [(0x11111111, 0), (0x22222222, 0), (0xdeadbeef, 0), (0x44444444, 1)]
    out = []

    def gen():
        yield dut.tr.sink.ready.eq(1)  # host side always ready for the echoed dwords.
        for d, last in data:
            yield dut.tr.source.valid.eq(1)
            yield dut.tr.source.data.eq(d)
            yield dut.tr.source.last.eq(last)
            yield
            if (yield dut.tr.source.valid) and (yield dut.tr.source.ready):
                assert (yield dut.tr.sink.valid), "loopback did not present an echoed dword"
                out.append(((yield dut.tr.sink.data), (yield dut.tr.sink.last)))
        yield dut.tr.source.valid.eq(0)
        yield

    run_simulation(dut, gen())
    assert out == data, f"round-trip mismatch: {[(hex(d), l) for d, l in out]} != {[(hex(d), l) for d, l in data]}"
    print("[OK] seam round-trip: dwords (data + last) echo through connect_payload(transport, loopback)")


def test_contract_shapes():
    # The transport and payload expose matching dword endpoints so connect_payload() joins them.
    tr = HostTransport()
    pl = LoopbackPayload()
    for ep in (tr.sink, tr.source, pl.sink, pl.source):
        assert len(ep.data) == 32, f"seam endpoint must carry 32-bit dwords, got {len(ep.data)}"
    print("[OK] seam contract: transport and payload expose 32-bit dword sink/source")


def main():
    test_contract_shapes()
    test_loopback_round_trip()
    print("All payload/transport seam tests passed.")


if __name__ == "__main__":
    main()
