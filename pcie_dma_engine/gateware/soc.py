# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# Board-independent PCIe DMA Engine SoC.
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

from pcie_dma_engine.gateware.msi import MSI
from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine

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

# PCIeDMAEngineSoC --------------------------------------------------------------------------------------

class PCIeDMAEngineSoC(SoCMini):
    def __init__(self, platform, sys_clk_freq=int(100e6), transport=None, with_analyzer=True):
        assert transport is not None

        # SoCMini ----------------------------------------------------------------------------------
        SoCMini.__init__(self, platform, sys_clk_freq, ident="PCIe DMA Engine SoC", ident_version=True)

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

# PcileechUsbSoC ----------------------------------------------------------------------------------------

class PcileechUsbSoC(SoCMini):
    """PCILeech-compatible SoC over an opaque FT601/USB transport.

    Same unified pattern as the Acorn (Ethernet) target -- an opaque transport (❶) feeds the LeechCore
    payload (❸, PCILeechEngine) which drives the PCIe TLP bus through the shared TlpAdapter (❷). Only
    the transport differs from the Acorn build: FT601/USB3 instead of Ethernet/UDP. The board presents
    to LeechCore as a PCIeSquirrel (v4.14, id 0x0C) over the FT601 FPGA transport.
    """
    def __init__(self, platform, sys_clk_freq=int(100e6), transport=None, with_analyzer=False):
        assert transport is not None

        SoCMini.__init__(self, platform, sys_clk_freq, ident="PCIe DMA Engine SoC (PCILeech/FT601)", ident_version=True)

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = _CRG(platform, sys_clk_freq)

        # Serial Wishbone Bridge -------------------------------------------------------------------
        self.submodules.bridge = UARTWishboneBridge(platform.request("serial"), sys_clk_freq, baudrate=3e6)
        self.bus.add_master(master=self.bridge.wishbone)

        # PCIe PHY ---------------------------------------------------------------------------------
        self.submodules.pcie_phy = pcie_phy = S7PCIEPHY(platform, platform.request("pcie_x1"),
            data_width    = 64,
            bar0_size     = 0x20000,
            with_cfg_mgmt = True,
        )

        # Host Transport (opaque dword seam) -------------------------------------------------------
        self.submodules.transport = transport

        # PCILeech payload: command/register control plane + TLP data plane (shared TlpAdapter). ----
        self.submodules.pcileech = pcileech = PCILeechEngine(
            version_major = 4,
            version_minor = 14,
            device_id     = 0x0c,          # 0x0C = PCIeSquirrel (SQRL Acorn / screamer-class)
            custom_value  = 0xffffffff,
            with_cfg_mgmt = True,
        )
        self.comb += [
            # Transport dword seam (host <-> FPGA).
            transport.source.connect(pcileech.sink),
            pcileech.source.connect(transport.sink),
            # PCIe TLP data plane.
            pcileech.tlp_sink.connect(pcie_phy.sink),
            pcie_phy.source.connect(pcileech.tlp_source),
            # Enumerated requester id -> LeechCore "Device PCIe ID" at PCIE 0x0008.
            pcileech.pcie_id.eq(pcie_phy.id),
            # PCIe live-config bridge (cfg_mgmt) -- tied idle inside the engine for now.
            pcie_phy.cfg_mgmt_addr.eq(pcileech.cfg_mgmt_addr),
            pcie_phy.cfg_mgmt_function_number.eq(0),
            pcie_phy.cfg_mgmt_read.eq(pcileech.cfg_mgmt_read),
            pcie_phy.cfg_mgmt_write.eq(pcileech.cfg_mgmt_write),
            pcie_phy.cfg_mgmt_write_data.eq(pcileech.cfg_mgmt_write_data),
            pcie_phy.cfg_mgmt_byte_enable.eq(pcileech.cfg_mgmt_byte_enable),
            pcileech.cfg_mgmt_read_data.eq(pcie_phy.cfg_mgmt_read_data),
            pcileech.cfg_mgmt_read_write_done.eq(pcie_phy.cfg_mgmt_read_write_done),
        ]

        # Wishbone --> MSI -------------------------------------------------------------------------
        self.submodules.msi = MSI()
        self.comb += self.msi.source.connect(pcie_phy.msi)

        # Led blink --------------------------------------------------------------------------------
        usb_counter = Signal(32)
        self.sync.usb += usb_counter.eq(usb_counter + 1)
        self.comb += platform.request("user_led", 0).eq(usb_counter[26])

        pcie_counter = Signal(32)
        self.sync.pcie += pcie_counter.eq(pcie_counter + 1)
        self.comb += platform.request("user_led", 1).eq(pcie_counter[26])

        # Analyzer ---------------------------------------------------------------------------------
        if with_analyzer:
            analyzer_signals = [pcie_phy.sink, pcie_phy.source]
            self.submodules.analyzer = LiteScopeAnalyzer(analyzer_signals, 1024, csr_csv="test/analyzer.csv")
