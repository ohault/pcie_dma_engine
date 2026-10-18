#!/usr/bin/env python3

# License: BSD

# Elaboration smoke test: elaborate every supported board to Verilog (without running the vendor
# toolchain) so that a change to one board cannot silently break the others. Each board is built in
# its own subprocess for isolation.
#
# Usage: python3 test/test_elaborate.py

import os
import sys
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pcie_screamer import BOARDS

_CHILD = """
import os, sys
sys.path.insert(0, {root!r})
from litex.soc.integration.builder import Builder
from pcie_screamer import create_soc
soc = create_soc({board!r}, with_analyzer=True, with_loopback=False)
Builder(soc, output_dir={out!r}, csr_csv=os.path.join({out!r}, "csr.csv"),
    compile_software=False, compile_gateware=False).build(run=False)
print("ELAB_OK", {board!r}, soc.build_name)
"""

def main():
    root     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    failures = []
    for board in BOARDS:
        out = tempfile.mkdtemp(prefix=f"elab_{board}_")
        code = _CHILD.format(root=root, board=board, out=out)
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
        ok = (r.returncode == 0) and ("ELAB_OK" in r.stdout)
        print(f"[{'OK ' if ok else 'FAIL'}] {board}")
        if not ok:
            failures.append(board)
            sys.stderr.write(r.stdout[-2000:] + "\n" + r.stderr[-2000:] + "\n")
    if failures:
        sys.exit(f"Elaboration failed for: {', '.join(failures)}")
    print(f"All {len(BOARDS)} board(s) elaborated successfully.")

if __name__ == "__main__":
    main()
