# License: BSD

# Ethernet (UDP) host transport for the PCILeech fast-path.
#
# Carries the 32-bit dword seam (see doc/pcileech_porting_analysis.md section 3.1) over UDP, so the
# PCILeech / LeechCore host can talk to the device as a UDP FPGA device (host UDP port 28474).
# Replaces NeTV2's closed FC1003 core with open LiteEth.
#
# Convention (matching FT601Sync): `sink` = dwords toward the host (TX), `source` = dwords from the
# host (RX). The device replies to the address/port the last request came from.
#
# STATUS: first implementation; elaboration and on-wire validation happen with a real Ethernet PHY
# on the Acorn board bring-up. Not yet wired into a SoC.

from migen import *
from migen.genlib.misc import WaitTimer

from litex.soc.interconnect import stream

from liteeth.core import LiteEthUDPIPCore

from pcie_dma_engine.gateware.pcileech.common import dword_layout, UDP_PORT

# Largest UDP payload, in dwords (1024 bytes), matching the PCILeech reference design.
MAX_DWORDS = 256


def _bswap32(sig):
    # Reverse the byte order of a 32-bit signal.
    #
    # LiteEth's dw=32 UDP port is little-endian: the first payload byte on the wire lands in the
    # dword's LSB (StrideConverter 8<->32 with reverse=False). The PCILeech dword protocol (engine +
    # host probe + LeechCore) is big-endian within a dword (first byte = MSB). We byte-swap at this
    # seam, both directions, so the engine keeps its big-endian convention over a real LiteEth link.
    return Cat(sig[24:32], sig[16:24], sig[8:16], sig[0:8])


class EthTransport(Module):
    def __init__(self, phy, mac_address, ip_address, sys_clk_freq,
                 udp_port=UDP_PORT, flush_timeout=1024):
        self.sink   = sink   = stream.Endpoint(dword_layout())  # FPGA -> host (TX).
        self.source = source = stream.Endpoint(dword_layout())  # host -> FPGA (RX).

        # # #

        # UDP/IP core + a 32-bit UDP port.
        self.submodules.core = core = LiteEthUDPIPCore(
            phy         = phy,
            mac_address = mac_address,
            ip_address  = ip_address,
            clk_freq    = sys_clk_freq,
        )
        udp = core.udp
        port = udp.crossbar.get_port(udp_port, dw=32)

        # Remote endpoint: latched from the last received datagram, used as the TX destination.
        remote_ip   = Signal(32)
        remote_port = Signal(16)

        # RX (host -> FPGA): forward payload dwords, latch the sender's address/port.
        self.comb += [
            source.valid.eq(port.source.valid),
            source.data.eq(_bswap32(port.source.data)),  # LE wire dword -> BE protocol dword.
            source.last.eq(port.source.last),
            port.source.ready.eq(source.ready),
        ]
        self.sync += If(port.source.valid & port.source.ready,
            remote_ip.eq(port.source.ip_address),
            remote_port.eq(port.source.src_port),
        )

        # TX (FPGA -> host): buffer dwords, then emit a UDP datagram of known length.
        self.submodules.fifo = fifo = stream.SyncFIFO(dword_layout(), 2*MAX_DWORDS)
        self.comb += sink.connect(fifo.sink)

        self.submodules.timer = timer = WaitTimer(flush_timeout)

        send_count = Signal(max=MAX_DWORDS + 1)
        beat       = Signal(max=MAX_DWORDS + 1)

        self.submodules.fsm = fsm = FSM(reset_state="IDLE")
        fsm.act("IDLE",
            # Flush when a full datagram is buffered, or after a timeout with data pending.
            timer.wait.eq(fifo.level != 0),
            If(fifo.level >= MAX_DWORDS,
                NextValue(send_count, MAX_DWORDS),
                NextValue(beat, 0),
                NextState("SEND"),
            ).Elif((fifo.level != 0) & timer.done,
                NextValue(send_count, fifo.level),
                NextValue(beat, 0),
                NextState("SEND"),
            ),
        )
        fsm.act("SEND",
            port.sink.valid.eq(1),
            port.sink.src_port.eq(udp_port),
            port.sink.dst_port.eq(remote_port),
            port.sink.ip_address.eq(remote_ip),
            port.sink.length.eq(send_count << 2),  # bytes
            port.sink.be.eq(0xf),  # all 4 bytes valid: we only ever emit whole dwords. Without this
                                   # the dw=32->dw=8 converter drops the last word and corrupts the
                                   # frame FCS, so the host NIC silently discards the datagram.
            port.sink.data.eq(_bswap32(fifo.source.data)),  # BE protocol dword -> LE wire dword.
            port.sink.last.eq(beat == (send_count - 1)),
            fifo.source.ready.eq(port.sink.ready),
            If(port.sink.valid & port.sink.ready,
                NextValue(beat, beat + 1),
                If(port.sink.last,
                    NextState("IDLE"),
                ),
            ),
        )
