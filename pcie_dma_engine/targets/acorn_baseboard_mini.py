# License: BSD

# LiteX Acorn Baseboard Mini + Acorn CLE215+ target.
#
# PCIe x1 + SFP 1000BASE-X on a shared GTP QPLL, matching the hardware-tested topology of
# enjoy-digital/litex_wr_nic (acorn_liteeth_nic.py), with all White-Rabbit parts omitted.
#
# Current scope (Phase 3): the SFP 1000BASE-X link carries the PCILeech 32-bit dword seam over UDP
# (host port 28474) via EthTransport, brought up in a dword loopback self-test. The PCIe PHY is
# instantiated and shares the QPLL (so the real GTP topology is exercised), but its raw TLP path is
# not yet routed to the PCILeech fast-path (TLP <-> dword <-> framer + command/register core) --
# that is the follow-on slice. On-wire validation needs the real board + LeechCore.

from migen import *

from litex.build.io import DifferentialInput
from litex.soc.cores.clock import S7PLL
from litex.soc.integration.soc_core import SoCMini

from litepcie.phy.s7pciephy import S7PCIEPHY

# LiteEth GTP QPLL + 1000BASE-X / 2500BASE-X: support both the older (hardware-tested) and current
# LiteEth module layouts.
try:
    from liteeth.phy.a7_gtp import QPLL, QPLLSettings
    from liteeth.phy.a7_1000basex import A7_1000BASEX, A7_2500BASEX
except ImportError:
    from liteeth.phy.serial.gtp_7series import QPLL, QPLLSettings
    from liteeth.phy.serial.basex.wrappers.a7_gtp import A7_1000BASEX, A7_2500BASEX

# Ethernet speed -> (PHY class, QPLL feedback divider). Both use a 156.25 MHz fabric refclk on QPLL
# channel 1 (refclksel GTGREFCLK, fbdiv_45=4); only fbdiv differs (see LiteEth gen.py). The PMA sets
# the GTP output dividers from the PHY's line rate (1.25 Gbps / 3.125 Gbps).
ETH_SPEEDS = {
    "1G":   (A7_1000BASEX, 4),
    "2.5G": (A7_2500BASEX, 5),
}

from pcie_dma_engine.platforms import acorn_baseboard_mini
from pcie_dma_engine.gateware.transport.eth import EthTransport
from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine

# CRG ----------------------------------------------------------------------------------------------

class _CRG(Module):
    def __init__(self, platform, sys_clk_freq):
        self.clock_domains.cd_sys        = ClockDomain()
        self.clock_domains.cd_refclk_eth = ClockDomain()

        # # #

        clk200    = platform.request("clk200")
        clk200_se = Signal()
        self.specials += DifferentialInput(clk200.p, clk200.n, clk200_se)

        self.submodules.pll = pll = S7PLL()
        pll.register_clkin(clk200_se, 200e6)
        pll.create_clkout(self.cd_sys, sys_clk_freq)

        # Ethernet QPLL reference (156.25 MHz), from a dedicated PLL.
        self.submodules.eth_pll = eth_pll = S7PLL()
        eth_pll.register_clkin(clk200_se, 200e6)
        eth_pll.create_clkout(self.cd_refclk_eth, 156.25e6, margin=0)

# AcornBaseboardMiniSoC ----------------------------------------------------------------------------

class AcornBaseboardMiniSoC(SoCMini):
    def __init__(self, platform, sys_clk_freq=int(125e6), eth_speed="1G",
                 ip_address="192.168.1.50", mac_address=0x10e2d5000000, **kwargs):
        if eth_speed not in ETH_SPEEDS:
            raise ValueError(f"Unknown eth_speed '{eth_speed}', available: {', '.join(ETH_SPEEDS)}")
        eth_phy_cls, eth_fbdiv = ETH_SPEEDS[eth_speed]

        # SoCMini ----------------------------------------------------------------------------------
        SoCMini.__init__(self, platform, sys_clk_freq, ident="PCIe DMA Engine SoC", ident_version=True)

        # CRG --------------------------------------------------------------------------------------
        self.submodules.crg = _CRG(platform, sys_clk_freq)

        # PCIe PHY (x1) ----------------------------------------------------------------------------
        self.submodules.pcie_phy = pcie_phy = S7PCIEPHY(platform, platform.request("pcie_x1"),
            data_width                = 64,
            bar0_size                 = 0x20000,
            pclk_mux_direct_from_mmcm = True,
            with_cfg_mgmt             = True,  # expose the hard-IP config-management interface
        )
        pcie_phy.add_gt_loc_constraints(["GTPE2_CHANNEL_X0Y7"], by_pipe_lane=False)

        # Shared GTP QPLL: PCIe on channel 0, Ethernet on channel 1. -------------------------------
        qpll_pcie_settings = QPLLSettings(refclksel=0b001, fbdiv=5,         fbdiv_45=5, refclk_div=1)
        qpll_eth_settings  = QPLLSettings(refclksel=0b111, fbdiv=eth_fbdiv, fbdiv_45=4, refclk_div=1)
        platform.add_platform_command("set_property SEVERITY {{Warning}} [get_drc_checks REQP-49]")
        self.submodules.qpll = qpll = QPLL(
            gtrefclk0     = pcie_phy.pcie_refclk,
            qpllsettings0 = qpll_pcie_settings,
            gtgrefclk1    = self.crg.cd_refclk_eth.clk,
            qpllsettings1 = qpll_eth_settings,
        )
        pcie_phy.use_external_qpll(qpll_channel=qpll.channels[0])

        # Ethernet PHY on SFP-0 (1000BASE-X / 2500BASE-X; polarities inverted on Acorn/baseboard). --
        self.submodules.ethphy = ethphy = eth_phy_cls(
            qpll_channel = qpll.channels[1],
            data_pads    = platform.request("sfp", 0),
            sys_clk_freq = sys_clk_freq,
            rx_polarity  = 1,
            tx_polarity  = 0,
        )

        # PCILeech dword seam over UDP (host port 28474). ------------------------------------------
        self.submodules.transport = transport = EthTransport(
            phy          = ethphy,
            mac_address  = mac_address,
            ip_address   = ip_address,
            sys_clk_freq = sys_clk_freq,
        )

        # PCILeech core: command/register control plane + TLP data plane. --------------------------
        self.submodules.pcileech = pcileech = PCILeechEngine(
            version_major = 1,
            version_minor = 0,
            device_id     = 0x01,
            with_cfg_mgmt = True,
        )
        self.comb += [
            # Transport dword seam (host <-> FPGA).
            transport.source.connect(pcileech.sink),
            pcileech.source.connect(transport.sink),
            # PCIe TLP data plane.
            pcileech.tlp_sink.connect(pcie_phy.sink),      # injected TLPs -> PCIe bus
            pcie_phy.source.connect(pcileech.tlp_source),  # received TLPs -> host
            # PCIe live-config bridge (cfg_mgmt).
            pcie_phy.cfg_mgmt_addr.eq(pcileech.cfg_mgmt_addr),
            pcie_phy.cfg_mgmt_function_number.eq(0),
            pcie_phy.cfg_mgmt_read.eq(pcileech.cfg_mgmt_read),
            pcie_phy.cfg_mgmt_write.eq(pcileech.cfg_mgmt_write),
            pcie_phy.cfg_mgmt_write_data.eq(pcileech.cfg_mgmt_write_data),
            pcie_phy.cfg_mgmt_byte_enable.eq(pcileech.cfg_mgmt_byte_enable),
            pcileech.cfg_mgmt_read_data.eq(pcie_phy.cfg_mgmt_read_data),
            pcileech.cfg_mgmt_read_write_done.eq(pcie_phy.cfg_mgmt_read_write_done),
        ]

# Builder entry point ------------------------------------------------------------------------------

def add_arguments(parser):
    parser.add_argument("--eth-speed", default="1G", choices=["1G", "2.5G"],
                        help="SFP Ethernet line speed (default: 1G)")
    parser.add_argument("--variant",   default="cle-215+", choices=["cle-215+", "cle-215", "cle-101"],
                        help="Acorn module variant (default: cle-215+)")

def argdict(args):
    return dict(eth_speed=args.eth_speed, variant=args.variant)

def create_soc(variant="cle-215+", eth_speed="1G", **kwargs):
    platform = acorn_baseboard_mini.Platform(variant=variant)
    return AcornBaseboardMiniSoC(platform, eth_speed=eth_speed)
