#!/usr/bin/env python3

# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

import os
import argparse
import importlib

from litex.soc.integration.builder import *

from gateware.core import ScreamerSoC
from gateware.transport.usb import USBTransport

# Boards -------------------------------------------------------------------------------------------

# board name -> (platform module, transport factory). Transports are kept pluggable so that new
# boards (Ethernet, ...) can be added without touching the shared SoC.
BOARDS = {
    "pcie_screamer":    {"platform": "platforms.pcie_screamer",    "transport": "usb"},
    "pcie_screamer_m2": {"platform": "platforms.pcie_screamer_m2", "transport": "usb"},
}

def create_soc(board, with_analyzer=False, with_loopback=False):
    if board not in BOARDS:
        raise ValueError(f"Unknown board '{board}', available: {', '.join(BOARDS)}")
    info         = BOARDS[board]
    platform     = importlib.import_module(info["platform"]).Platform()
    sys_clk_freq = int(100e6)

    if info["transport"] == "usb":
        transport = USBTransport(platform, sys_clk_freq, with_loopback=with_loopback)
    else:
        raise ValueError(f"Unknown transport '{info['transport']}' for board '{board}'")

    return ScreamerSoC(platform, sys_clk_freq, transport=transport, with_analyzer=with_analyzer)

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="PCIe Screamer Test Gateware")
    parser.add_argument("--board",         default="pcie_screamer", choices=list(BOARDS), help="Board to build")
    parser.add_argument("--m2",            action="store_true", help="use M2 variant of PCIe Screamer (alias for --board pcie_screamer_m2)")
    parser.add_argument("--with-analyzer", action="store_true", help="enable Analyzer")
    parser.add_argument("--with-loopback", action="store_true", help="enable USB Loopback")
    parser.add_argument("--build", action="store_true", help="Build bitstream")
    parser.add_argument("--load",  action="store_true", help="Load bitstream")
    parser.add_argument("--flash", action="store_true", help="Flash bitstream")
    args = parser.parse_args()

    board = "pcie_screamer_m2" if args.m2 else args.board

    soc     = create_soc(board, with_analyzer=args.with_analyzer, with_loopback=args.with_loopback)
    builder = Builder(soc, csr_csv="test/csr.csv")
    builder.build(run=args.build)

    if args.load:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(os.path.join(builder.gateware_dir, soc.build_name + ".bit"))

    if args.flash:
        prog = soc.platform.create_programmer()
        prog.load_bitstream(os.path.join(builder.gateware_dir, soc.build_name + ".bin"))

if __name__ == "__main__":
    main()
