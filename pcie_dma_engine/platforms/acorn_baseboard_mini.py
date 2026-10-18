# License: BSD

# LiteX Acorn Baseboard Mini + Acorn CLE215+ (Xilinx Artix-7 xc7a200t).
#
# Thin wrapper over the upstream LiteX-Boards sqrl_acorn platform with the "Mini" baseboard
# extension (which routes PCIe x1 and the SFP cages directly to FPGA pins). Reused rather than
# copied so pin definitions stay in sync with LiteX-Boards.

from litex_boards.platforms import sqrl_acorn


class Platform(sqrl_acorn.Platform):
    def __init__(self, variant="cle-215+", toolchain="vivado"):
        sqrl_acorn.Platform.__init__(self, variant=variant, toolchain=toolchain)
        # Mini baseboard: PCIe x1, SFP-0/1, serial, ... directly on the FPGA pins.
        self.add_extension(sqrl_acorn.litex_acorn_baseboard_mini, prepend=True)
