# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# USB (FT601) host transport.
#
# Wraps the FT601 synchronous FIFO PHY and the USB packet core, and exposes the
# board-independent transport interface used by ScreamerSoC:
#   - wishbone_master : control-plane Wishbone master (None in loopback mode).
#   - tlp             : TLP sender/receiver endpoint (None in loopback mode).

from migen import *

from litex.soc.interconnect import stream
from litex.soc.cores.usb_fifo import phy_description

from gateware.usb import USBCore
from gateware.etherbone import Etherbone
from gateware.tlp import TLP
from gateware.ft601 import FT601Sync


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
