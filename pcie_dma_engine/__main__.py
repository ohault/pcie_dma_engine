#!/usr/bin/env python3

# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# PCIe DMA Engine command-line interface: python -m pcie_dma_engine [options]

import os
import argparse

from litex.soc.integration.builder import *

from pcie_dma_engine import BOARDS, create_soc

# Build --------------------------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(prog="pcie_dma_engine", description="PCIe DMA Engine Gateware")
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
