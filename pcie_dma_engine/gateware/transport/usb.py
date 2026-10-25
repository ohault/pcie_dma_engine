# License: BSD

# FT601 / USB3 host transport.
#
# Opaque dword mover (unified architecture seam ❶): wraps the FT601 synchronous FIFO and exposes only
# the 32-bit dword seam (self.sink/self.source) -- no Wishbone, no TLP framing, no payload knowledge.
# The LeechCore payload (PCILeechEngine) runs over it exactly as EthTransport carries it over UDP.

from migen import *

from pcie_dma_engine.gateware.ft601 import FT601Sync
from pcie_dma_engine.gateware.transport.base import HostTransport


class Ft601Transport(HostTransport):
    """Opaque FT601/USB3 host transport.

    Moves raw 32-bit dwords between the monitoring host and the FPGA over the FT601 synchronous FIFO.
    Byte-order normalization is the payload's job, so this transport is byte-order-agnostic. Needs the
    'usb' clock domain (the FT601 FIFO clock) set up by the caller's CRG.
    """
    def __init__(self, platform, timeout=1024):
        HostTransport.__init__(self)
        self.submodules.phy = phy = FT601Sync(platform.request("usb_fifo"), dw=32, timeout=timeout)
        self.comb += [
            # host -> FPGA (RX): FT601 FIFO -> dword seam.
            self.source.valid.eq(phy.source.valid),
            self.source.data.eq(phy.source.data),
            self.source.last.eq(phy.source.last),
            phy.source.ready.eq(self.source.ready),
            # FPGA -> host (TX): dword seam -> FT601 FIFO.
            phy.sink.valid.eq(self.sink.valid),
            phy.sink.data.eq(self.sink.data),
            phy.sink.last.eq(self.sink.last),
            self.sink.ready.eq(phy.sink.ready),
        ]
