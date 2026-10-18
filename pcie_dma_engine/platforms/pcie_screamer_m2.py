# License: BSD

# LambdaConcept PCIe Screamer M2 (Xilinx Artix-7 xc7a35t). Thin wrapper over the upstream
# LiteX-Boards platform so the pin map stays in sync with LiteX-Boards.

from litex_boards.platforms import lambdaconcept_pcie_screamer_m2


class Platform(lambdaconcept_pcie_screamer_m2.Platform):
    pass
