#!/usr/bin/env python3

# License: BSD

# Convenience launcher for the PCIe DMA Engine CLI.
# Equivalent to `python -m pcie_dma_engine`; e.g. ./build.py --board pcie_screamer --build

from pcie_dma_engine.__main__ import main

if __name__ == "__main__":
    main()
