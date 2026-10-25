# License: BSD

# LiteX Acorn Baseboard Mini + Acorn CLE215+ (Xilinx Artix-7 xc7a200t).
#
# Thin wrapper over the upstream LiteX-Boards sqrl_acorn platform with the "Mini" baseboard
# extension (which routes PCIe x1 and the SFP cages directly to FPGA pins). Reused rather than
# copied so pin definitions stay in sync with LiteX-Boards.

from litex_boards.platforms import sqrl_acorn


class Platform(sqrl_acorn.Platform):
    # The Acorn boots in Master SPIx4 mode: the flash's QE bit must be set or it won't load the
    # bitstream on power-up (same as litex-acorn-baseboard's flash.py).
    flash_kwargs = {"enable_quad": True}

    def __init__(self, variant="cle-215+", toolchain="vivado"):
        sqrl_acorn.Platform.__init__(self, variant=variant, toolchain=toolchain)
        # Mini baseboard: PCIe x1, SFP-0/1, serial, ... directly on the FPGA pins.
        self.add_extension(sqrl_acorn.litex_acorn_baseboard_mini, prepend=True)
