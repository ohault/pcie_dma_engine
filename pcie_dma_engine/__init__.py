# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# PCIe DMA Engine SoC : open, multi-board PCIe DMA gateware built on LiteX.
#
# Library API: create_soc(board, ...) builds the SoC for a board by dispatching to its target
# module (pcie_dma_engine/targets/<board>.py). Every board has a platform (pcie_dma_engine/platforms)
# and a target (pcie_dma_engine/targets), matching the LiteX-Boards convention. The command-line
# entry point is ./build.py.

import importlib

# board name -> target module (each exposes create_soc(**kwargs)).
BOARDS = {
    "pcie_screamer":        "pcie_dma_engine.targets.pcie_screamer",
    "pcie_screamer_m2":     "pcie_dma_engine.targets.pcie_screamer_m2",
    "acorn_baseboard_mini": "pcie_dma_engine.targets.acorn_baseboard_mini",
}

def import_target(board):
    """Return the target module for a board (it exposes create_soc() and, optionally, the
    add_arguments()/argdict() pair that declares the board's own build.py options)."""
    if board not in BOARDS:
        raise ValueError(f"Unknown board '{board}', available: {', '.join(BOARDS)}")
    return importlib.import_module(BOARDS[board])

def create_soc(board, **kwargs):
    return import_target(board).create_soc(**kwargs)
