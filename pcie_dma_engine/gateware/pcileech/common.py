# License: BSD

# PCILeech wire-protocol constants and codecs.
#
# These mirror the PCILeech / LeechCore contract (see doc/pcileech_porting_analysis.md section 3),
# so that the LiteX fast-path and the host (LeechCore device_fpga.c) agree on the wire format.
# Keeping them in one place is the "shared protocol definitions" referenced by the design doc.
#
# NOTE: on-wire byte/dword endianness between the transport and the host still has to be confirmed
# on real hardware against LeechCore; the values below are the logical contract.

from litex.soc.interconnect.stream import EndpointDescription

# Framing (FPGA -> host) ---------------------------------------------------------------------------

# Each return group is 8 dwords: 1 status dword + 7 data dwords.
GROUP_DATA_DWORDS = 7
FRAME_MARKER      = 0xe          # status[31:28] == 0xE marks a valid group.
FILLER_DWORD      = 0xffffffff   # mux-level padding data word (source nibble 0xF, ignored by host).
FILLER_NIBBLE     = 0xf
COM_RESYNC        = 0x55556666   # com-level dword->qword resync filler (host skips these).

# Source ports (low 2 bits of a data word's 4-bit source nibble; nibble = (ctx << 2) | port).
PORT_TLP      = 0  # PCIe TLP        (mux priority 0, highest)
PORT_CFG      = 1  # PCIe config
PORT_LOOPBACK = 2  # loopback
PORT_COMMAND  = 3  # command/register responses

CFG_REPLY_TAG = 0x77310000       # high half of a PCIe CFG reply data word.

# Command words (host -> FPGA) ---------------------------------------------------------------------

CMD_MAGIC  = 0x77                # command word byte[7].
TYPE_READ  = 0x10                # byte[6] = TYPE_READ  | target
TYPE_WRITE = 0x20                # byte[6] = TYPE_WRITE | target

# Register banks / flags (target = flags & 0x03; bank-select bits live in flags & 0xC000).
FPGA_REG_CORE           = 0x0003
FPGA_REG_PCIE           = 0x0001
FPGA_REG_READONLY       = 0x0000
FPGA_REG_READWRITE      = 0x8000
FPGA_REG_SHADOWCFGSPACE = 0xc000

# Core read-only register file (byte offsets into `ro`, see design doc section 3.4).
RO_MAGIC        = 0xab89         # ro[15:0] at +000.
RO_OFF_MAGIC    = 0x00
RO_OFF_SIZEOF   = 0x04
RO_OFF_VER_MAJ  = 0x08
RO_OFF_VER_MIN  = 0x09
RO_OFF_DEVICE   = 0x0a
RO_OFF_UPTIME   = 0x10
RO_OFF_DRP_DATA = 0x20
RO_OFF_CUSTOM   = 0x24

# Transport ----------------------------------------------------------------------------------------

UDP_PORT = 28474                 # host-side UDP port (LeechCore device_fpga.c).

# Stream layouts -----------------------------------------------------------------------------------

def dword_layout():
    # The transport seam: a stream of 32-bit dwords (host <-> FPGA).
    return EndpointDescription([("data", 32)])

def framer_layout():
    # Framer input: a data dword plus its 4-bit source nibble ((ctx << 2) | port).
    return EndpointDescription([("data", 32), ("nibble", 4)])

# Host-side command-word codec (pure Python; for reference models and tests) -----------------------

def encode_command(addr, value=0, mask=0xffff, write=False, bank=FPGA_REG_CORE):
    """Encode an 8-byte PCILeech register command word (little-endian on the wire)."""
    target = bank & 0x03
    b = bytes([
        value & 0xff, (value >> 8) & 0xff,                 # [0:1] data (LE)
        mask  & 0xff, (mask  >> 8) & 0xff,                 # [2:3] byte mask
        ((addr >> 8) & 0x3f) | ((bank & 0xc000) >> 8),     # [4]   addr_hi | bank-select
        addr & 0xff,                                       # [5]   addr_lo
        (TYPE_WRITE if write else TYPE_READ) | target,     # [6]   type | target
        CMD_MAGIC,                                         # [7]   magic 0x77
    ])
    return b

def decode_command(b):
    """Decode an 8-byte command word into a dict (inverse of encode_command)."""
    assert len(b) == 8, "command word must be 8 bytes"
    assert b[7] == CMD_MAGIC, f"bad magic {b[7]:#04x}"
    write = (b[6] & 0xf0) == TYPE_WRITE
    return {
        "value":  b[0] | (b[1] << 8),
        "mask":   b[2] | (b[3] << 8),
        "addr":   b[5] | ((b[4] & 0x3f) << 8),
        "bank":   ((b[4] & 0xc0) << 8) | (b[6] & 0x03),
        "write":  write,
        "target": b[6] & 0x03,
    }

def status_dword(nibbles):
    """Build a return-group status dword from up to 7 source nibbles (data word 0 -> bits[3:0]).

    Unused slots are padded with the filler nibble, matching the framer's filler data words.
    """
    assert len(nibbles) <= GROUP_DATA_DWORDS
    v = FRAME_MARKER << 28
    for i in range(GROUP_DATA_DWORDS):
        n = nibbles[i] if i < len(nibbles) else FILLER_NIBBLE
        v |= (n & 0xf) << (4 * i)
    return v
