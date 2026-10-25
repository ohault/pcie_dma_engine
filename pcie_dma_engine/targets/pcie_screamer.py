# License: BSD

# LambdaConcept PCIe Screamer target (FT601/USB3): PCILeech-compatible over an opaque FT601 transport
# (unified architecture) -- the LeechCore payload runs over USB exactly as over Ethernet on the Acorn.

from pcie_dma_engine.gateware.soc import PcileechUsbSoC
from pcie_dma_engine.gateware.transport.usb import Ft601Transport
from pcie_dma_engine.platforms import pcie_screamer


def add_arguments(parser):
    parser.add_argument("--with-analyzer", action="store_true", help="Enable the LiteScope analyzer")

def argdict(args):
    return dict(with_analyzer=args.with_analyzer)

def create_soc(with_analyzer=False, **kwargs):
    platform     = pcie_screamer.Platform()
    sys_clk_freq = int(100e6)
    transport    = Ft601Transport(platform)
    return PcileechUsbSoC(platform, sys_clk_freq, transport=transport, with_analyzer=with_analyzer)
