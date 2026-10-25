# License: BSD

# LambdaConcept PCIe Screamer M2 target (FT601/USB3).
#
# Default: PCILeech-compatible over an opaque FT601 transport (unified architecture) -- the LeechCore
# payload runs over USB exactly as over Ethernet on the Acorn. Use --legacy for the original
# Etherbone + raw-TLP-bridge host path (enjoy-digital tooling).

from pcie_dma_engine.gateware.soc import PCIeDMAEngineSoC, PcileechUsbSoC
from pcie_dma_engine.gateware.transport.usb import USBTransport, Ft601Transport
from pcie_dma_engine.platforms import pcie_screamer_m2


def add_arguments(parser):
    parser.add_argument("--legacy", action="store_true",
                        help="Legacy Etherbone + raw-TLP host path (default: PCILeech over FT601)")
    parser.add_argument("--with-analyzer", action="store_true", help="Enable the LiteScope analyzer")
    parser.add_argument("--with-loopback", action="store_true", help="Enable the USB loopback path (legacy only)")

def argdict(args):
    return dict(legacy=args.legacy, with_analyzer=args.with_analyzer, with_loopback=args.with_loopback)

def create_soc(legacy=False, with_analyzer=False, with_loopback=False, **kwargs):
    platform     = pcie_screamer_m2.Platform()
    sys_clk_freq = int(100e6)
    if legacy:
        transport = USBTransport(platform, sys_clk_freq, with_loopback=with_loopback)
        return PCIeDMAEngineSoC(platform, sys_clk_freq, transport=transport, with_analyzer=with_analyzer)
    transport = Ft601Transport(platform)
    return PcileechUsbSoC(platform, sys_clk_freq, transport=transport, with_analyzer=with_analyzer)
