# License: BSD

# LambdaConcept PCIe Screamer M2 target (FT601/USB3): shared PCIeDMAEngineSoC + USB transport.

from pcie_dma_engine.gateware.soc import PCIeDMAEngineSoC
from pcie_dma_engine.gateware.transport.usb import USBTransport
from pcie_dma_engine.platforms import pcie_screamer_m2


def add_arguments(parser):
    parser.add_argument("--with-analyzer", action="store_true", help="Enable the LiteScope analyzer")
    parser.add_argument("--with-loopback", action="store_true", help="Enable the USB loopback path")

def argdict(args):
    return dict(with_analyzer=args.with_analyzer, with_loopback=args.with_loopback)

def create_soc(with_analyzer=False, with_loopback=False, **kwargs):
    platform     = pcie_screamer_m2.Platform()
    sys_clk_freq = int(100e6)
    transport    = USBTransport(platform, sys_clk_freq, with_loopback=with_loopback)
    return PCIeDMAEngineSoC(platform, sys_clk_freq, transport=transport, with_analyzer=with_analyzer)
