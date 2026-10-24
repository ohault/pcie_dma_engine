#!/usr/bin/env python3

# License: BSD

# Standalone host-side probe for the PCIe DMA Engine SoC Ethernet (UDP) transport.
#
# Speaks the PCILeech dword protocol directly over UDP (host port 28474) to sanity-check an
# Ethernet board (e.g. acorn_baseboard_mini) WITHOUT LeechCore: it issues CORE register reads and
# prints the identity (magic 0xab89, version, device id). Use it for bring-up; the authoritative
# client is PCILeech / LeechCore.
#
# Wire format (see doc/pcileech_porting_analysis.md section 3, matching the gateware):
#   - command word: 8 bytes, sent in order; magic 0x77 is byte[7].
#   - transport dwords are little-endian on the wire, exactly as LeechCore's device_fpga.c reads
#     them (x86 reads each 4-byte group as a native uint32); so a dword is decoded with
#     int.from_bytes(.., "little").
#   - return: 32-byte groups (1 status + 7 data dwords); status top nibble 0xE; each data word's
#     source nibble is the next low nibble of the status word; command responses carry
#     {addr[31:16], byteswap(data)[15:0]}.

import sys
import socket
import argparse

UDP_PORT      = 28474
MAGIC         = 0x77
TYPE_READ     = 0x10
TARGET_CORE   = 0x03
FRAME_MARKER  = 0xe
PORT_COMMAND  = 3

# CORE RO register offsets (bytes).
RO = {0x00: "magic", 0x08: "version", 0x0a: "device_id"}


def read_cmd(addr, target=TARGET_CORE):
    """8-byte CORE read command for a byte address (RO file)."""
    return bytes([0x00, 0x00, 0x00, 0x00,
                  (addr >> 8) & 0x3f, addr & 0xff,
                  TYPE_READ | target, MAGIC])


def parse_responses(data):
    """Parse 32-byte groups from a received buffer; return {addr: value} for command responses."""
    resp = {}
    for i in range(0, len(data) - 31, 32):
        status = int.from_bytes(data[i:i + 4], "little")
        if (status & 0xf0000000) != (FRAME_MARKER << 28):
            continue
        for k in range(7):
            nib = (status >> (4 * k)) & 0xf
            dw  = int.from_bytes(data[i + 4 + 4 * k:i + 8 + 4 * k], "little")
            if (nib & 0x3) == PORT_COMMAND:
                addr = (dw >> 16) & 0xffff
                bs   = dw & 0xffff
                resp[addr] = ((bs & 0xff) << 8) | ((bs >> 8) & 0xff)  # undo byteswap
    return resp


def main():
    p = argparse.ArgumentParser(description="PCIe DMA Engine SoC UDP probe")
    p.add_argument("ip", help="board IP address")
    p.add_argument("--port", type=int, default=UDP_PORT, help=f"UDP port (default {UDP_PORT})")
    p.add_argument("--timeout", type=float, default=1.0, help="receive timeout (s)")
    args = p.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(args.timeout)
    s.connect((args.ip, args.port))

    # Issue the identity reads.
    payload = b"".join(read_cmd(a) for a in RO)
    s.send(payload)

    buf = b""
    try:
        while True:
            buf += s.recv(2048)
    except socket.timeout:
        pass

    if not buf:
        print(f"No response from {args.ip}:{args.port} (check IP/SFP link/firmware).")
        return 1

    resp = parse_responses(buf)
    magic = resp.get(0x00)
    ok = magic == 0xab89
    print(f"received {len(buf)} bytes")
    print(f"  magic     : {magic:#06x} {'(OK)' if ok else '(expected 0xab89)'}" if magic is not None else "  magic     : <none>")
    if 0x08 in resp:
        v = resp[0x08]
        print(f"  version   : {v & 0xff}.{(v >> 8) & 0xff}")
    if 0x0a in resp:
        print(f"  device_id : {resp[0x0a] & 0xff:#04x}")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
