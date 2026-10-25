# License: BSD

# Host transport interface (unified architecture seam ❶ -- see doc/unified_tlp_architecture.md).
#
# A transport is an OPAQUE mover of 32-bit dwords between the FPGA and the monitoring host. It owns the
# link (bring-up, segmentation, the 0x55556666 dword<->qword resync, any keepalive) and exposes exactly
# two stream endpoints and NO payload-specific port, so any payload can run over any transport:
#
#     source : dwords host -> FPGA
#     sink   : dwords FPGA -> host
#
# Subclasses (EthTransport, the FT601 USBTransport, a future usb3_pipe Usb3Transport) call
# super().__init__() and then drive self.source / consume self.sink.

from migen import *

from litex.soc.interconnect import stream

from pcie_dma_engine.gateware.pcileech.common import dword_layout


class HostTransport(Module):
    def __init__(self):
        self.sink   = stream.Endpoint(dword_layout())  # FPGA -> host (TX).
        self.source = stream.Endpoint(dword_layout())  # host -> FPGA (RX).


def connect_payload(transport, payload):
    """Wire a transport to a payload at the ❶ seam: transport RX -> payload, payload -> transport TX.

    Returns a list of comb statements (add to a Module's self.comb)."""
    return [
        transport.source.connect(payload.sink),
        payload.source.connect(transport.sink),
    ]
