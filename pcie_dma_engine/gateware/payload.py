# License: BSD

# Host-link payload interface (unified architecture seam ❸ -- see doc/unified_tlp_architecture.md).
#
# A payload speaks a host protocol (LeechCore today, others later) over the transport dword seam, and
# -- for DMA payloads -- drives the PCIe TLP bus via the shared TlpAdapter. The transport-facing
# contract is exactly two dword stream endpoints, mirroring HostTransport so connect_payload() joins
# them:
#
#     sink   : dwords host -> FPGA (from transport.source)
#     source : dwords FPGA -> host (to transport.sink)
#
# A payload that moves TLPs additionally exposes tlp_sink/tlp_source (phy_layout) and any sideband
# (pcie_id, cfg_mgmt, msi); those are payload-specific and not part of this base.

from migen import *

from litex.soc.interconnect import stream

from pcie_dma_engine.gateware.pcileech.common import dword_layout


class HostPayload(Module):
    def __init__(self):
        self.sink   = stream.Endpoint(dword_layout())  # host -> FPGA.
        self.source = stream.Endpoint(dword_layout())  # FPGA -> host.


class LoopbackPayload(HostPayload):
    """A trivial payload that echoes every received dword back to the host and touches no PCIe bus.

    It lets a transport be exercised end to end -- host -> transport -> payload -> transport -> host --
    in simulation, with no PCIe PHY and no board. This is the conformance harness for transports whose
    hardware is not on hand yet (the usb3_pipe Usb3Transport, or the FT601 Ft601Transport): drive the
    transport's host-facing side, pair it with this payload, and check the dwords round-trip."""
    def __init__(self):
        HostPayload.__init__(self)
        self.comb += self.sink.connect(self.source)
