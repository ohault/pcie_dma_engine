# License: BSD

# PCILeech protocol engine: command/register control plane + TLP data plane + response framer.
#
# Implements the host<->FPGA PCILeech protocol (see doc/pcileech_porting_analysis.md section 3),
# matching pcileech_com.sv / pcileech_fifo.sv:
#
#   host -> FPGA : a stream of 32-bit dwords. Two dwords form a 64-bit word, paired first-dword-to-
#                  high ((prev << 32) | new). Two consecutive 0x66665555 resync the pairing. Every
#                  valid word has magic 0x77 in [7:0] and a 2-bit type in [9:8]:
#                    00 = PCIe TLP   (data in [63:32], last in [10])
#                    11 = command    (register read/write, target CORE)
#                  (01 = CFG, 10 = loopback: not yet handled.)
#   FPGA -> host : return groups built by PCILeechFramer. Sources are merged by a priority arbiter:
#                    PORT_TLP (received PCIe TLPs)  > PORT_COMMAND (register responses).
#
# Command word bit layout (pcileech_fifo.sv):
#   [7:0] magic 0x77   [9:8] target (3=CORE)   [12] read   [13] write
#   [31:16] address    [31] RW-file   [30] shadow-cfg
#   value = {cmd[48+:8], cmd[56+:8]}   mask = {cmd[32+:8], cmd[40+:8]}  (byte-swapped)
# Register read: data16 = file[(addr[14:0] << 3) +: 16]; response = {addr[15:0], byteswap(data16)}.
#
# NOTE: TLP dword ordering/byte-swap and the full enumeration (PCIe register bank, writes) still
# need validation against LeechCore on hardware; this is the first integrated implementation.

from migen import *

from litex.soc.interconnect import stream

from litepcie.common import phy_layout

from pcie_dma_engine.gateware.tlp_adapter import TlpAdapter

from pcie_dma_engine.gateware.pcileech.common import (
    RO_MAGIC, PORT_TLP, PORT_CFG, PORT_COMMAND, CMD_MAGIC, dword_layout, framer_layout,
)
from pcie_dma_engine.gateware.pcileech.framer import PCILeechFramer
from pcie_dma_engine.gateware.payload import HostPayload

# Register-bank targets / channel types (command word bits [9:8]).
TARGET_CORE = 0b11
TYPE_TLP    = 0b00

RESYNC_WORD = 0x66665555

RO_BITS = 320  # 40 bytes
RW_BITS = 240  # 30 bytes


class PCILeechEngine(HostPayload):
    def __init__(self, version_major=1, version_minor=0, device_id=0x00, custom_value=0,
                 with_cfg_mgmt=False):
        HostPayload.__init__(self)                                       # dword seam: self.sink/self.source.
        sink, source    = self.sink, self.source                        # host <-> FPGA dwords.
        self.tlp_sink   = tlp_sink   = stream.Endpoint(phy_layout(64))   # TLPs to the PCIe bus.
        self.tlp_source = tlp_source = stream.Endpoint(phy_layout(64))   # TLPs from the PCIe bus.
        # PCIe requester id (bus/device/function) assigned by the target's enumeration, already in the
        # sys domain (LitePCIe S7PCIEPHY.id = Cat(function, device, bus)). Served to LeechCore as the
        # "Device PCIe ID" at FPGA_REG_PCIE 0x0008. Left 0 until the host wires the PHY's id in.
        self.pcie_id    = pcie_id    = Signal(16)

        # # #

        # Shared return-path framer.
        self.submodules.framer = framer = PCILeechFramer()
        self.comb += framer.source.connect(source)

        # TLP data-plane activity counters (diagnostic). Count 64-bit TLP words injected toward the
        # PCIe PHY and words received from it, so a host read of these CORE registers after a DMA
        # attempt shows whether read requests egress and whether any TLP (completion) comes back.
        tlp_inj_cnt = Signal(16)
        tlp_rcv_cnt = Signal(16)
        self.sync += [
            If(tlp_sink.valid   & tlp_sink.ready,   tlp_inj_cnt.eq(tlp_inj_cnt + 1)),
            If(tlp_source.valid & tlp_source.ready, tlp_rcv_cnt.eq(tlp_rcv_cnt + 1)),
        ]

        # Read-only register file (CORE bank), byte offsets per design doc section 3.4.
        ro = Signal(RO_BITS)
        self.comb += [
            ro[0:16].eq(RO_MAGIC),         # +000 magic 0xab89
            ro[32:64].eq(RO_BITS >> 3),    # +004 sizeof (40)
            ro[64:72].eq(version_major),   # +008 version major
            ro[72:80].eq(version_minor),   # +009 version minor
            ro[80:88].eq(device_id),       # +00A device id
            ro[96:112].eq(tlp_inj_cnt),    # +00C TLP words injected -> PHY (diagnostic)
            ro[112:128].eq(tlp_rcv_cnt),   # +00E TLP words received <- PHY (diagnostic)
            ro[288:320].eq(custom_value),  # +024 custom value
        ]
        rw = Signal(RW_BITS)  # CORE RW file (writes deferred).

        # RX: pair two dwords into a 64-bit word (first dword -> high half), with resync.
        # The transport is opaque and delivers little-endian wire dwords; the LeechCore command
        # protocol decodes with the magic byte at word[7:0], so normalize each incoming dword to the
        # protocol byte order here, at the payload's input (TX replies are already emitted in the order
        # LeechCore reads little-endian, so the output side needs no swap).
        self.comb += sink.ready.eq(1)
        rx_dw = Signal(32)
        self.comb += rx_dw.eq(Cat(sink.data[24:32], sink.data[16:24], sink.data[8:16], sink.data[0:8]))
        word       = Signal(64)
        have_first = Signal()
        word_stb   = Signal()  # pulses the cycle after a full 64-bit word is latched.
        self.sync += [
            word_stb.eq(0),
            If(sink.valid,
                word.eq(Cat(rx_dw, word[0:32])),  # (prev << 32) | new
                If((rx_dw == RESYNC_WORD) & (word[0:32] == RESYNC_WORD),
                    have_first.eq(0),
                ).Elif(have_first,
                    have_first.eq(0),
                    word_stb.eq(1),
                ).Else(
                    have_first.eq(1),
                ),
            ),
        ]

        # Decode (combinational on the latched word).
        magic    = word[0:8]
        wtype    = word[8:10]
        is_read  = word[12]
        is_write = word[13]
        addr16   = word[16:32]
        f_rw     = word[31]
        addr_bit = Signal(18)
        self.comb += addr_bit.eq(Cat(Signal(3), addr16[0:15]))  # {addr[14:0], 3'b000}

        value16  = Signal(16)
        mask16   = Signal(16)
        self.comb += [
            value16.eq(Cat(word[56:64], word[48:56])),  # {word[48+:8], word[56+:8]}
            mask16.eq(Cat(word[40:48], word[32:40])),   # {word[32+:8], word[40+:8]}
        ]

        valid_word = (magic == CMD_MAGIC)
        is_cmd     = valid_word & (wtype == TARGET_CORE)
        is_tlp_in  = valid_word & (wtype == TYPE_TLP)
        in_range   = addr_bit < Mux(f_rw, RW_BITS, RO_BITS)

        # Shadow-config-space selector (command address bit 14).
        f_shadow   = word[30]
        shadow_adr = word[18:28]  # dword address, in_cmd_address_byte[11:2] (10 bits)
        shadow_lo  = word[17]     # in_cmd_address_byte[1], selects the 16-bit half

        # --- Command plane: ordinary CORE register read response ----------------------------------
        reg_rd = Signal(16)
        self.comb += reg_rd.eq(Mux(f_rw, (rw >> addr_bit)[0:16], (ro >> addr_bit)[0:16]) &
                               Replicate(in_range, 16))
        # Reply dword layout LeechCore's DeviceFPGA_Session_ParseConfigReply expects:
        #   wAddr = byteswap(dword[15:0]); data16 = dword[31:16]
        # i.e. {data16, byteswap(addr16)} -- address (byte-swapped) in the LOW half, data in the HIGH
        # half. (The pcileech-fpga HDL's _cmd_tx_din uses the opposite halves, but its FT601/COM path
        # reorders the bytes before the wire; our LiteEth path does not, so we emit the wire layout
        # directly.) Getting this wrong makes LeechCore read the address as data and reject the v4
        # identity (reported as device v0.0).
        reg_resp = Signal(32)
        self.comb += reg_resp.eq(Cat(addr16[8:16], addr16[0:8], reg_rd))  # {data16, byteswap(addr16)}

        # --- Config-space shadow (host-editable config image) -------------------------------------
        # 256-byte (64-dword) standard config header; host reads/writes it via the shadow command
        # path (type 11 + address bit 14). Response: {4'b1100, addr, addr_lo, 0} tag + 16-bit half
        # (no byteswap). NOTE: presenting this image to the *target* needs PCIe-IP external-config
        # support (not available on the 7-series hard IP used here); the live PCIe config is reachable
        # via cfg_mgmt instead -- both are follow-on, hardware-validated work.
        shadow_init = [0] * 64
        shadow_init[0] = (0x0666 << 16) | 0x10EE  # [0x00] device_id:vendor_id (Xilinx 0x10EE / 0x0666)
        self.specials.shadow_mem = shadow_mem = Memory(32, 64, init=shadow_init)
        self.specials.shadow_rp  = shadow_rp  = shadow_mem.get_port(async_read=True)
        self.specials.shadow_wp  = shadow_wp  = shadow_mem.get_port(write_capable=True, we_granularity=8)
        shadow_half = Signal(16)
        self.comb += [
            shadow_rp.adr.eq(shadow_adr[0:6]),
            shadow_half.eq(Mux(shadow_lo, shadow_rp.dat_r[0:16], shadow_rp.dat_r[16:32])),
        ]
        shadow_tag  = Signal(16)
        shadow_resp = Signal(32)
        self.comb += [
            shadow_tag.eq(0xc000 | (shadow_adr << 2) | (shadow_lo << 1)),
            shadow_resp.eq(Cat(shadow_half, shadow_tag)),
        ]
        # Shadow write: write the selected 16-bit half (byte-enables pick the half).
        self.comb += [
            shadow_wp.adr.eq(shadow_adr[0:6]),
            shadow_wp.dat_w.eq(Cat(value16, value16)),
            shadow_wp.we.eq(0),
            If(word_stb & is_cmd & is_write & f_rw & f_shadow,
                shadow_wp.we.eq(Mux(shadow_lo, 0b0011, 0b1100)),
            ),
        ]

        # Read responses (shadow vs ordinary) share the command channel; mutually exclusive.
        resp = Signal(32)
        self.comb += resp.eq(Mux(f_shadow, shadow_resp, reg_resp))
        self.submodules.cmd_fifo = cmd_fifo = stream.SyncFIFO(framer_layout(), 16)
        self.sync += [
            cmd_fifo.sink.valid.eq(0),
            If(word_stb & is_cmd & is_read,
                cmd_fifo.sink.valid.eq(1),
                cmd_fifo.sink.data.eq(resp),
                cmd_fifo.sink.nibble.eq(PORT_COMMAND),
                cmd_fifo.sink.last.eq(1),
            ),
        ]
        # Register write: masked 16-bit write into the CORE RW file at the byte address. A 16-bit
        # write touches byte A (low, mask[7:0]) and byte A+1 (high, mask[15:8]).
        addr_byte  = addr16[0:15]
        write_stb  = Signal()
        self.comb += write_stb.eq(word_stb & is_cmd & is_write & f_rw)
        for b in range(RW_BITS // 8):
            byte_mask = Signal(8)
            byte_val  = Signal(8)
            self.comb += [
                byte_mask.eq(Mux(addr_byte == b, mask16[0:8],
                             Mux(addr_byte == (b - 1), mask16[8:16], 0))),
                byte_val.eq(Mux(addr_byte == b, value16[0:8],
                            Mux(addr_byte == (b - 1), value16[8:16], 0))),
            ]
            self.sync += If(write_stb,
                rw[8*b:8*b + 8].eq((rw[8*b:8*b + 8] & ~byte_mask) | (byte_val & byte_mask)),
            )

        # (The PCIe register bank (target 01 -> PCIe config path) is handled below / deferred.)

        # --- TLP inject FIFO: the payload buffers the TLP dwords parsed from host commands. --------
        self.submodules.inj_fifo = inj_fifo = stream.SyncFIFO(phy_layout(32), 512)
        self.sync += [
            inj_fifo.sink.valid.eq(0),
            If(word_stb & is_tlp_in,
                inj_fifo.sink.valid.eq(1),
                inj_fifo.sink.dat.eq(word[32:64]),  # [63:32] payload dword
                inj_fifo.sink.be.eq(0xf),
                inj_fifo.sink.last.eq(word[10]),    # last dword of the TLP
            ),
        ]

        # --- Shared TLP width adapter: 32-bit TLP dwords <-> phy_layout(64), byte-enables, stale-tail
        # masking and per-TLP first/last all handled once in TlpAdapter (gateware/tlp_adapter.py).
        self.submodules.tlp_adapter = tlp_adapter = TlpAdapter()
        self.comb += [
            tlp_adapter.inj.valid.eq(inj_fifo.source.valid),
            tlp_adapter.inj.dat.eq(inj_fifo.source.dat),
            tlp_adapter.inj.last.eq(inj_fifo.source.last),
            inj_fifo.source.ready.eq(tlp_adapter.inj.ready),
            tlp_adapter.phy_tx.connect(tlp_sink),
            tlp_source.connect(tlp_adapter.phy_rx),
        ]

        # --- TLP return framing (payload concern: the LeechCore source nibble). -------------------
        # tlp_adapter.ret already delivers wire-order dwords; the payload only stamps the source nibble
        # {FIRST=bit3, LAST=bit2, port=PORT_TLP} (v4.13+ LeechCore ignores any TLP whose first dword is
        # not FIRST-marked) and hands the stream to the framer.
        tlp_ret = stream.Endpoint(framer_layout())
        self.comb += [
            tlp_ret.valid.eq(tlp_adapter.ret.valid),
            tlp_ret.data.eq(tlp_adapter.ret.dat),
            tlp_ret.nibble.eq(Cat(Constant(PORT_TLP, 2), tlp_adapter.ret.last, tlp_adapter.ret.first)),
            tlp_ret.last.eq(tlp_adapter.ret.last),
            tlp_adapter.ret.ready.eq(tlp_ret.ready),
        ]

        # --- PCIe register bank (target 01) -> LeechCore "FPGA_REG_PCIE" -------------------------------
        # LeechCore reads the device's PCIe identity from this bank as a register file (NOT a cfg_mgmt
        # passthrough). The only field it needs to connect is the requester id (bus/device/function)
        # at byte offset 0x0008, which it calls the "Device PCIe ID" and rejects when zero. That id is
        # the PHY's enumerated id (self.pcie_id), already in the sys domain -- no cfg_mgmt (and no
        # clock-domain crossing) is involved. Layout mirrors pcileech_pcie_cfg_a7.sv's read-only file:
        #   +000 magic 0x2301   +004 bytecount   +008 bus_number, +009 device[4:0]|function[7:5].
        # Reads reply on PORT_CFG with the same {data, byteswap(addr)} layout as the CORE bank.
        cfg_ret = None
        if with_cfg_mgmt:
            # cfg_mgmt interface kept exposed for the acorn target's wiring, but tied idle: the live
            # config-space read path (host writes cfg_mgmt_dwaddr/rd_en, reads back cfg_mgmt_do) needs
            # a pcie<->sys CDC and is deferred until the TLP data plane is validated on hardware.
            self.cfg_mgmt_addr            = Signal(10)
            self.cfg_mgmt_read            = Signal()
            self.cfg_mgmt_write           = Signal()
            self.cfg_mgmt_write_data      = Signal(32)
            self.cfg_mgmt_byte_enable     = Signal(4, reset=0xf)
            self.cfg_mgmt_read_data       = Signal(32)
            self.cfg_mgmt_read_write_done = Signal()

            RO_PCIE_BITS = 320
            ro_pcie = Signal(RO_PCIE_BITS)
            self.comb += [
                ro_pcie[0:16].eq(0x2301),             # +000 magic
                ro_pcie[32:64].eq(RO_PCIE_BITS >> 3), # +004 bytecount (bytes)
                ro_pcie[64:72].eq(pcie_id[8:16]),     # +008 bus_number
                ro_pcie[72:77].eq(pcie_id[3:8]),      # +009 device_number[4:0]
                ro_pcie[77:80].eq(pcie_id[0:3]),      # +009 function_number[7:5]
            ]

            is_pcie_rd  = valid_word & (wtype == 0b01) & is_read  # target PCIE read
            pcie_range  = addr_bit < RO_PCIE_BITS
            pcie_reg_rd = Signal(16)
            # Read-only file; RW-bank reads (addr bit15) return 0 but still echo the address so the
            # host's DeviceFPGA_Session_ParseConfigReply marks them covered.
            self.comb += pcie_reg_rd.eq((ro_pcie >> addr_bit)[0:16] &
                                        Replicate(pcie_range & ~f_rw, 16))
            pcie_resp = Signal(32)
            self.comb += pcie_resp.eq(Cat(addr16[8:16], addr16[0:8], pcie_reg_rd))  # {data, byteswap(addr16)}

            self.submodules.cfg_fifo = cfg_fifo = stream.SyncFIFO(framer_layout(), 16)
            self.sync += [
                cfg_fifo.sink.valid.eq(0),
                If(word_stb & is_pcie_rd,
                    cfg_fifo.sink.valid.eq(1),
                    cfg_fifo.sink.data.eq(pcie_resp),
                    cfg_fifo.sink.nibble.eq(PORT_CFG),
                    cfg_fifo.sink.last.eq(1),
                ),
            ]
            cfg_ret = cfg_fifo.source

        # --- Return arbiter: TLP > CFG > command responses -> framer -------------------------------
        if cfg_ret is not None:
            self.comb += [
                If(tlp_ret.valid,
                    tlp_ret.connect(framer.sink),
                ).Elif(cfg_ret.valid,
                    cfg_ret.connect(framer.sink),
                ).Else(
                    cmd_fifo.source.connect(framer.sink),
                ),
            ]
        else:
            self.comb += [
                If(tlp_ret.valid,
                    tlp_ret.connect(framer.sink),
                ).Else(
                    cmd_fifo.source.connect(framer.sink),
                ),
            ]
