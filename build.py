#!/usr/bin/env python3

# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# PCIe DMA Engine SoC — build entry point.
#   ./build.py --board <name> [--build] [--load] [--flash] [board-specific options]
#
# There is no default board: --board is required. Board-specific options (e.g. --eth-speed for the
# SFP boards, --with-analyzer for the FT601 boards) are contributed by the selected board's target
# module, so only the options that actually apply to that board are accepted and shown in --help.

import os
import argparse

from litex.soc.integration.builder import *

from pcie_dma_engine import BOARDS, import_target, create_soc


def main():
    # Phase 1 — resolve the board so we can ask its target for board-specific options.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--board", choices=list(BOARDS))
    board  = pre.parse_known_args()[0].board
    target = import_target(board) if board else None

    # Phase 2 — the real parser: common options + the selected board's own options.
    parser = argparse.ArgumentParser(prog="build.py", description="PCIe DMA Engine SoC build entry point")
    parser.add_argument("--board", required=True, choices=list(BOARDS), help="Board to build (required)")
    if target is not None and hasattr(target, "add_arguments"):
        target.add_arguments(parser.add_argument_group(f"{board} options"))
    actions = parser.add_argument_group("build actions")
    actions.add_argument("--build", action="store_true", help="Build the bitstream")
    actions.add_argument("--load",  action="store_true", help="Load the bitstream")
    actions.add_argument("--flash", action="store_true", help="Flash the bitstream")
    args = parser.parse_args()

    target     = import_target(args.board)
    soc_kwargs = target.argdict(args) if hasattr(target, "argdict") else {}
    soc        = create_soc(args.board, **soc_kwargs)

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
