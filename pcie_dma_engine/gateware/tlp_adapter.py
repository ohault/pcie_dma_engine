# License: BSD

# Shared TLP width adapter (payload <-> LitePCIe PHY).
#
# Converts between the PHY's 64-bit phy_layout TLP stream and a 32-bit TLP dword stream, and is the
# single home for the width-conversion correctness that LiteX's StrideConverter gets wrong on
# litepcie's phy_layout (whose data field is named "dat", not "data"):
#
#   - Inject (32 -> 64): an odd-length TLP's last 64-bit beat carries one valid dword; force its
#     byte-enables to 0x0f so the PHY transmits exactly the requested dword count. A stale high dword
#     would turn a 3-DW MRd into a 4-DW TLP that the root port rejects as a Malformed TLP.
#   - Return (64 -> 32): expose the stream under a "data"-named layout so the down-converter drops the
#     stale high dword on an odd-length completion (and retimes "last" onto the real final dword),
#     and derive a per-TLP "first" from the "last" boundary (the S7PCIEPHY leaves source.first at 0).
#
# Byte order and host framing (e.g. the LeechCore source nibble) are the *payload's* concern and are
# deliberately NOT done here, so any payload / any FPGA family can reuse this module unchanged.

from migen import *

from litex.soc.interconnect import stream
from litex.soc.interconnect.stream import StrideConverter

from litepcie.common import phy_layout


def tlp_dword_layout():
    """A 32-bit TLP dword stream; the Endpoint's first/last qualifiers mark TLP boundaries."""
    return stream.EndpointDescription([("dat", 32)])


def _data_layout(w):
    # A phy-like layout whose data field is named "data" so StrideConverter's byte-enable handling
    # engages (litepcie's phy_layout names it "dat", which disables that handling).
    return stream.EndpointDescription([("data", w), ("be", w // 8)])


class TlpAdapter(Module):
    def __init__(self):
        # Payload side (32-bit TLP dwords; .last ends a TLP, .first marks its start on return).
        self.inj = stream.Endpoint(tlp_dword_layout())   # payload -> bus (inject)
        self.ret = stream.Endpoint(tlp_dword_layout())   # bus -> payload (return)
        # PHY side (LitePCIe phy_layout(64)).
        self.phy_tx = stream.Endpoint(phy_layout(64))    # -> pcie_phy.sink
        self.phy_rx = stream.Endpoint(phy_layout(64))    # <- pcie_phy.source

        # # #

        # --- Inject: 32 -> 64, masking the stale high dword on an odd-length last beat. -----------
        self.submodules.inj_conv = inj_conv = StrideConverter(phy_layout(32), phy_layout(64))
        self.comb += [
            inj_conv.sink.valid.eq(self.inj.valid),
            inj_conv.sink.dat.eq(self.inj.dat),
            inj_conv.sink.be.eq(0xf),
            inj_conv.sink.last.eq(self.inj.last),
            self.inj.ready.eq(inj_conv.sink.ready),
        ]
        inj_half     = Signal()  # 0: next accepted inject dword is the low half of a new 64-bit word.
        last_partial = Signal()  # the pending last output beat carries a single (low) dword.
        self.sync += [
            If(inj_conv.sink.valid & inj_conv.sink.ready,
                If(inj_conv.sink.last, inj_half.eq(0)).Else(inj_half.eq(~inj_half)),
            ),
            If(inj_conv.sink.valid & inj_conv.sink.ready & inj_conv.sink.last & ~inj_half,
                last_partial.eq(1)),
            If(self.phy_tx.valid & self.phy_tx.ready & self.phy_tx.last,
                last_partial.eq(0)),
        ]
        self.comb += [
            inj_conv.source.connect(self.phy_tx, omit={"be"}),
            If(self.phy_tx.last & last_partial,
                self.phy_tx.be.eq(0x0f),
            ).Else(
                self.phy_tx.be.eq(0xff),
            ),
        ]

        # --- Return: 64 -> 32, dropping the stale tail dword and deriving per-TLP first. ----------
        self.submodules.ret_conv = ret_conv = StrideConverter(_data_layout(64), _data_layout(32))
        self.comb += [
            ret_conv.sink.valid.eq(self.phy_rx.valid),
            ret_conv.sink.first.eq(self.phy_rx.first),
            ret_conv.sink.last.eq(self.phy_rx.last),
            ret_conv.sink.data.eq(self.phy_rx.dat),
            ret_conv.sink.be.eq(self.phy_rx.be),
            self.phy_rx.ready.eq(ret_conv.sink.ready),
        ]
        in_tlp = Signal()  # within a TLP: the next dword after a non-last dword continues it.
        self.sync += If(ret_conv.source.valid & ret_conv.source.ready,
            in_tlp.eq(~ret_conv.source.last),
        )
        self.comb += [
            self.ret.valid.eq(ret_conv.source.valid),
            self.ret.dat.eq(ret_conv.source.data),
            self.ret.first.eq(~in_tlp),
            self.ret.last.eq(ret_conv.source.last),
            ret_conv.source.ready.eq(self.ret.ready),
        ]
