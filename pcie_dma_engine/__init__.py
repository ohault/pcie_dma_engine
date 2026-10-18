# This file is Copyright (c) 2016-2020 Florent Kermarrec <florent@enjoy-digital.fr>
# This file is Copyright (c) 2018-2019 Pierre-Olivier Vauboin <po@lambdaconcept.com>
# License: BSD

# PCIe DMA Engine : open, multi-board PCIe DMA gateware built on LiteX.
#
# Library API: create_soc() builds a PCIeDMAEngineSoC for a given board, with a pluggable host
# transport. The command-line interface lives in __main__ (python -m pcie_dma_engine).

import importlib

from pcie_dma_engine.gateware.core import PCIeDMAEngineSoC
from pcie_dma_engine.gateware.transport.usb import USBTransport

# Boards -------------------------------------------------------------------------------------------

# board name -> (platform module, transport). Transports are kept pluggable so that new boards
# (Ethernet, ...) can be added without touching the shared SoC.
BOARDS = {
    "pcie_screamer":    {"platform": "pcie_dma_engine.platforms.pcie_screamer",    "transport": "usb"},
    "pcie_screamer_m2": {"platform": "pcie_dma_engine.platforms.pcie_screamer_m2", "transport": "usb"},
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

    return PCIeDMAEngineSoC(platform, sys_clk_freq, transport=transport, with_analyzer=with_analyzer)
