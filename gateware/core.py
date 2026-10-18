# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# Board-independent PCIe Screamer SoC.
#
# Wires the PCIe PHY, the (pluggable) host transport, MSI and the optional
# analyzer together. Board-specific details (platform, clocking) are supplied by
# the caller; the transport (USB/Ethernet/...) is supplied as a ready-built
# object exposing the interface described in gateware/transport/usb.py.

from migen import *
from migen.genlib.resetsync import AsyncResetSynchronizer

from litex.soc.cores.clock import *
from litex.soc.integration.soc_core import *
from litex.soc.cores.uart import UARTWishboneBridge

from litepcie.phy.s7pciephy import S7PCIEPHY

from gateware.msi import MSI

from litescope import LiteScopeAnalyzer

# CRG ----------------------------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq):
        self.clock_domains.cd_sys = ClockDomain()
        self.clock_domains.cd_usb = ClockDomain()

        # # #

        # sys
        sys_clk_100 = platform.request("clk100")
        platform.add_period_constraint(sys_clk_100, 1e9/100e6)
        self.submodules.pll = pll = S7PLL(speedgrade=-1)
        pll.register_clkin(sys_clk_100, 100e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)

        # usb
        usb_clk100 = platform.request("usb_fifo_clock")
        platform.add_period_constraint(usb_clk100, 1e9/100e6)
        self.comb += self.cd_usb.clk.eq(usb_clk100)
        self.specials += AsyncResetSynchronizer(self.cd_usb, ResetSignal("pcie"))

# ScreamerSoC --------------------------------------------------------------------------------------

class ScreamerSoC(SoCMini):
    def __init__(self, platform, sys_clk_freq=int(100e6), transport=None, with_analyzer=True):
        assert transport is not None

        # SoCMini ----------------------------------------------------------------------------------
        SoCMini.__init__(self, platform, sys_clk_freq, ident="PCIe Screamer", ident_version=True)

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = _CRG(platform, sys_clk_freq)

        # Serial Wishbone Bridge -------------------------------------------------------------------
        self.submodules.bridge = UARTWishboneBridge(platform.request("serial"), sys_clk_freq, baudrate=3e6)
        self.bus.add_master(master=self.bridge.wishbone)

        # PCIe PHY ---------------------------------------------------------------------------------
        self.submodules.pcie_phy = S7PCIEPHY(platform, platform.request("pcie_x1"))

        # Host Transport (USB/Ethernet/...) --------------------------------------------------------
        self.submodules.transport = transport

        # Transport <--> Wishbone ------------------------------------------------------------------
        if transport.wishbone_master is not None:
            self.bus.add_master(master=transport.wishbone_master)

        # Transport <--> TLP -----------------------------------------------------------------------
        if transport.tlp is not None:
            self.comb += [
                self.pcie_phy.source.connect(transport.tlp.sender.sink),
                transport.tlp.receiver.source.connect(self.pcie_phy.sink),
            ]

        # Wishbone --> MSI -------------------------------------------------------------------------
        self.submodules.msi = MSI()
        self.comb += self.msi.source.connect(self.pcie_phy.msi)

        # Led blink --------------------------------------------------------------------------------
        usb_counter = Signal(32)
        self.sync.usb += usb_counter.eq(usb_counter + 1)
        self.comb += platform.request("user_led", 0).eq(usb_counter[26])

        pcie_counter = Signal(32)
        self.sync.pcie += pcie_counter.eq(pcie_counter + 1)
        self.comb += platform.request("user_led", 1).eq(pcie_counter[26])

        # Analyzer ---------------------------------------------------------------------------------
        if with_analyzer:
            analyzer_signals = [
                self.pcie_phy.sink,
                self.pcie_phy.source,
            ]
            self.submodules.analyzer = LiteScopeAnalyzer(analyzer_signals, 1024, csr_csv="test/analyzer.csv")
