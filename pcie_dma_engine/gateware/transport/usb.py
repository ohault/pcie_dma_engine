# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# USB (FT601) host transport.
#
# Wraps the FT601 synchronous FIFO PHY and the USB packet core, and exposes the
# board-independent transport interface used by PCIeDMAEngineSoC:
#   - wishbone_master : control-plane Wishbone master (None in loopback mode).
#   - tlp             : TLP sender/receiver endpoint (None in loopback mode).

from migen import *

from litex.soc.interconnect import stream
from litex.soc.cores.usb_fifo import phy_description

from pcie_dma_engine.gateware.usb import USBCore
from pcie_dma_engine.gateware.etherbone import Etherbone
from pcie_dma_engine.gateware.tlp import TLP
from pcie_dma_engine.gateware.ft601 import FT601Sync
from pcie_dma_engine.gateware.transport.base import HostTransport


class Ft601Transport(HostTransport):
    """Opaque FT601/USB3 host transport (unified architecture seam ❶).

    Moves raw 32-bit dwords between the monitoring host and the FPGA over the FT601 synchronous FIFO,
    exposing ONLY the dword seam (self.sink/self.source) -- no Wishbone, no TLP framing, no knowledge
    of the payload. The LeechCore payload (PCILeechEngine) runs over it exactly as over EthTransport;
    byte-order normalization is the payload's job, so this transport is byte-order-agnostic. Needs the
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


class USBTransport(Module):
    # Logical ports multiplexed over the USB link.
    usb_map = {
        "wishbone": 0,
        "tlp":      1,
    }

    def __init__(self, platform, sys_clk_freq, with_loopback=False):
        self.wishbone_master = None
        self.tlp             = None

        # # #

        # FT601 USB3 PHY.
        self.submodules.phy = phy = FT601Sync(platform.request("usb_fifo"), dw=32, timeout=1024)

        # Loopback: wire the PHY back onto itself (no Wishbone/TLP).
        if with_loopback:
            self.submodules.loopback_fifo = loopback_fifo = stream.SyncFIFO(phy_description(32), 2048)
            self.comb += [
                phy.source.connect(loopback_fifo.sink),
                loopback_fifo.source.connect(phy.sink),
            ]
        # Normal: USB Core with Wishbone (Etherbone) and TLP ports.
        else:
            self.submodules.core = core = USBCore(phy, sys_clk_freq)

            # USB <--> Wishbone.
            self.submodules.etherbone = etherbone = Etherbone(core, self.usb_map["wishbone"])
            self.wishbone_master = etherbone.master.bus

            # USB <--> TLP.
            self.submodules.tlp = TLP(core, self.usb_map["tlp"])
