# License: BSD

# PCILeech return-path framer (FPGA -> host).
#
# Packs a stream of (data, nibble) beats into PCILeech return groups: one status dword followed by
# seven data dwords (see doc/pcileech_porting_analysis.md section 3.2). The status dword carries the
# 0xE marker in bits[31:28] and one 4-bit source nibble per data word (data word 0 -> bits[3:0]).
# A partial group is padded with filler words (0xFFFFFFFF, nibble 0xF) after a timeout, so data is
# not stuck waiting for a full group.
#
# NOTE: the on-wire byte/dword ordering (handled by the transport/com layer below this module) still
# needs confirmation against LeechCore on hardware; this module produces the logical dword sequence.

from migen import *
from migen.genlib.misc import WaitTimer

from litex.soc.interconnect import stream

from pcie_dma_engine.gateware.pcileech.common import (
    GROUP_DATA_DWORDS, FRAME_MARKER, FILLER_DWORD, FILLER_NIBBLE,
    dword_layout, framer_layout,
)


class PCILeechFramer(Module):
    def __init__(self, timeout=1024):
        self.sink   = sink   = stream.Endpoint(framer_layout())  # (data, nibble) beats.
        self.source = source = stream.Endpoint(dword_layout())   # dword stream to the transport.

        # # #

        n = GROUP_DATA_DWORDS

        # Group storage: n data words and n source nibbles.
        data    = Array(Signal(32) for _ in range(n))
        nibbles = Array(Signal(4)  for _ in range(n))

        count     = Signal(max=n + 1)  # number of beats gathered so far (0..n).
        out_index = Signal(max=n + 1)  # emit index (0 = status, 1..n = data[out_index-1]).

        # Status dword: 0xE marker + the n gathered nibbles (unused slots default to filler nibble).
        status = Signal(32)
        self.comb += status[28:32].eq(FRAME_MARKER)
        for i in range(n):
            self.comb += If(count > i,
                status[4*i:4*i + 4].eq(nibbles[i]),
            ).Else(
                status[4*i:4*i + 4].eq(FILLER_NIBBLE),
            )

        # Idle timeout: flush a partial group once the sink has been idle for `timeout` cycles.
        self.submodules.timer = timer = WaitTimer(timeout)

        self.submodules.fsm = fsm = FSM(reset_state="GATHER")
        fsm.act("GATHER",
            # Arm the flush timer while a partial group is pending and nothing is arriving.
            timer.wait.eq((count != 0) & ~sink.valid),
            sink.ready.eq(count != n),
            If(sink.valid & sink.ready,
                NextValue(data[count],    sink.data),
                NextValue(nibbles[count], sink.nibble),
                NextValue(count, count + 1),
                If(count == (n - 1),
                    NextValue(out_index, 0),
                    NextState("EMIT"),
                ),
            ).Elif((count != 0) & timer.done,
                NextValue(out_index, 0),
                NextState("EMIT"),
            ),
        )
        fsm.act("EMIT",
            source.valid.eq(1),
            If(out_index == 0,
                source.data.eq(status),
            ).Else(
                # Real data for gathered slots, filler for the padded tail.
                If(out_index <= count,
                    source.data.eq(data[out_index - 1]),
                ).Else(
                    source.data.eq(FILLER_DWORD),
                ),
            ),
            source.last.eq(out_index == n),
            If(source.ready,
                NextValue(out_index, out_index + 1),
                If(out_index == n,
                    NextValue(count, 0),
                    NextState("GATHER"),
                ),
            ),
        )
