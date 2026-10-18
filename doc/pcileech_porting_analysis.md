# PCIe Screamer → multi-board, PCILeech-compatible : design analysis

Status: **analysis / design** (no gateware yet).
Branch: `claude/pcie-screamer-porting-ak3ksh`.

This document captures the architecture agreed for turning this LiteX project into a
**multi-board, PCILeech-compatible** DMA tool, while keeping every currently-supported
target building. It is the reference for the implementation phases that follow.

---

## 1. Goal and constraints

1. Add support for new boards — starting with the **Acorn CLE215+ on the LiteX Acorn
   Baseboard Mini** (Xilinx Artix-7 `xc7a200t`) and the **Sipeed Tang Mega 138K Pro Dock**
   (Gowin `GW5AST-138`) — and, as far as possible, **any LiteX-supported board** that has a
   PCIe PHY (LitePCIe) and a usable host link.
2. **Keep all existing targets building**, including the original `pcie_screamer` and
   `pcie_screamer_m2` (FT601 / USB3).
3. Stay **compatible with the PCILeech / LeechCore host tools**, including over Ethernet.
4. Not limited to Artix-7: the design must reach other FPGA families (Gowin, UltraScale,
   Lattice, Altera) that LitePCIe already covers.

The consequence of (3) is decisive: "compatible with PCILeech" is defined by **LeechCore's
`device_fpga.c` wire protocol**, not by "it speaks UDP". The contract is reproduced in §3.

---

## 2. Architecture : fast-path gateware + C control firmware

A soft CPU cannot sit in the bulk DMA path (per-dword handling at line rate is far beyond a
~100 MHz VexRiscv). So the design is a **split**:

```
 Host (PCILeech / LeechCore device_fpga.c)
        │  transport (UDP / FT601 / FT245 / SFP …)
        ▼
 ┌──────────────────────────── LiteX SoC ─────────────────────────────┐
 │ FAST PATH  (gateware, family-independent):                          │
 │   transport ⇄ PCILeech dword framing ⇄ raw TLP ⇄ LitePCIe          │
 │                                                                     │
 │ CONTROL PATH (C firmware on VexRiscv — reuses PCILeech protocol     │
 │   definitions; GPLv3):                                              │
 │   connect handshake · CMD register · version/device-ID · DRP ·      │
 │   MSI · config-space shadow contents / device "spoof"              │
 │                                                                     │
 │ PHY LAYER (the only board-specific part):                           │
 │   LitePCIe {s7, us, usp, gw5a, c5, lfcpnx, sv}                      │
 │   + host-transport PHY (LiteEth, FT601, FT245, SerDes/LiteICLink)   │
 └─────────────────────────────────────────────────────────────────────┘
```

- **Fast path (gateware):** carries the bulk TLP/dword stream. This is the analogue of the
  existing `gateware/tlp.py` path, but wrapped in **PCILeech framing** (§3) instead of the
  enjoy-digital framing. It is a Migen stream pipeline; the CPU is not in the loop.
- **Control path (C firmware):** the low-rate, logic-heavy part (handshake, CMD register,
  version, DRP, config-space shadow). This is where PCILeech protocol code is reused.
- **PHY layer:** the only per-board surface — a LitePCIe PHY and a host-transport PHY.

### What "reuse LeechCore C code" means precisely

`device_fpga.c` is **host-side** (runs on the operator PC, talks *to* the FPGA). PCILeech's
FPGA side is entirely SystemVerilog; there is no device-side C to lift wholesale. Reuse is:

- **Share protocol definitions** (command/dword formats, CMD-register bit layout,
  config/DRP word syntax, magic constants, RO register map) in a **common header** included
  by both the host and the new **device-side firmware**, so the two ends cannot drift.
- The firmware is a **new device-side implementation** that satisfies what `device_fpga.c`
  sends; it does not run the host driver loop.

### Licensing boundary

PCILeech / LeechCore are **GPLv3**; this project is **BSD-2-Clause**. Firmware that reuses
Leech code becomes GPLv3 — it must live in its own module/directory with explicit licensing,
kept separate from the BSD gateware.

---

## 3. The PCILeech wire contract (mined from source)

Confirmed from `pcileech-fpga` (`pcileech_fifo.sv`, `pcileech_mux.sv`,
`pcileech_com.sv`, `pcileech_eth.sv`) and LeechCore (`device_fpga.c`). This is the spec the
fast path + firmware must satisfy.

### 3.1 Transport seam (all transports share it)

Every PCILeech transport plugs in behind `pcileech_com` and exposes the **same 32-bit
dword-FIFO interface**: `dout[31:0]/dout_valid` (FPGA→host) and
`din[31:0]/din_wr_en/din_ready` (host→FPGA). Transport selection in the reference design is a
compile macro (`ENABLE_FT601`, `ENABLE_ETH`). `pcileech_com` does 32↔64-bit conversion and
uses a **resync magic `0x66665555`** (two in a row → realign the 32→64 pairing).

### 3.2 Return framing (FPGA → host), from `pcileech_mux` + `device_fpga.c`

- Stream is walked in **32-byte groups**: 1 status dword + 7 data dwords.
- Fillers equal to **`0x55556666`** are skipped (dword→qword resync).
- A group is valid only if `(status & 0xf000_0000) == 0xe000_0000` (top nibble **`0xE`**).
- Each data word's **source nibble** is the low nibble of the status word, shifted right by 4
  after each word. Nibble = `(ctx << 2) | port`, with ports:
  `0=PCIe TLP, 1=PCIe CFG, 2=loopback, 3=command` (mux priority, 0 highest).
- PCIe CFG replies are additionally tagged `0x7731_0000` in the data word's high half.
- Command/register responses: upper 16 bits = tag (echoed address), lower 16 bits = data
  (byte-swapped).

### 3.3 Command word (host → FPGA), 8 bytes, from `device_fpga.c`

| Byte | Field |
|---|---|
| 0..1 | data (value), little-endian |
| 2..3 | byte mask (`0xff` = byte enabled) |
| 4    | `(addr_hi) \| (flags & 0xC000)>>8` — bit7 = write-regbank, bit6 = shadow-cfg |
| 5    | addr low |
| 6    | type/target: `0x10\|t` = read, `0x20\|t` = write, `t = flags & 0x03` |
| 7    | **magic `0x77`** |

Register banks / flags: `FPGA_REG_CORE = 0x0003`, `FPGA_REG_PCIE = 0x0001`,
`FPGA_REG_READWRITE = 0x8000`, `FPGA_REG_SHADOWCFGSPACE = 0xC000`,
`FPGA_REG_READONLY = 0x0000`. A write only takes effect with the RW (addr bit15) set.

### 3.4 Core read-only register file (`pcileech_fifo.sv` `ro`)

| Offset | Content |
|---|---|
| +000 | MAGIC = **`0xab89`** |
| +004 | SIZEOF (bytes) |
| +008 | VERSION MAJOR |
| +009 | VERSION MINOR |
| +00A | DEVICE ID |
| +010 | UPTIME (100 MHz ticks) |
| +018 | INACTIVITY TIMER base |
| +020 | DRP data |
| +022 | `pcie_present` (bit0), `pcie_perst_n` (bit1) |
| +024 | CUSTOM VALUE |

### 3.5 Key CMD/control register bits (`rw`)

`[16]` inactivity-timer en · `[17]` send-count en · `[18]` wait-for-DRP (reset 1) ·
`[20]` DRP RD · `[21]` DRP WR · `[31]` global reset · `[200]` PCIe core reset (reset 1) ·
`[201]` PCIe subsystem reset · `[202]` CFGTLP processing en (reset 1) · `[203]` CFGTLP zero ·
`[204]` CFGTLP filter · `[205]` clk100 en (reset 1) · `[206]` CFGTLP PCIe write en.
DRP: `drp_en=rw[20]|rw[21]`, `drp_we=rw[21]`, `drp_addr=rw[224+:9]`, `drp_di=rw[208+:16]`.
Default shadow IDs: subsys-vendor `0x10EE`, subsys `0x0007`, vendor `0x10EE`,
device `0x0666`, rev `0x02`.

### 3.6 Connect handshake (`device_fpga.c`)

1. Pipe resync: write four `0x55556666` qwords.
2. Version query v4: `00 00 00 00 00 08 13 77`; v3 fallback: `00 00 00 00 01 00 03 77`.
3. Read CORE RO (`FPGA_REG_CORE|READONLY = 0x0003`) identity → magic `0xab89`, version,
   device-ID.
4. Write CORE RW `0x0008` = `0x000186a0` (1 ms inactivity timer).
5. Read PCIe RO `0x0008` for device-ID (byte-swapped), up to 4 tries; if zero, check PCIe RW
   `0x0000` magic `0x6745`, then hot-reset.

### 3.7 Ethernet (UDP) specifics (`device_fpga.c` / `pcileech_eth.sv`)

- **UDP port `28474`** (host side). (NeTV2 FPGA side is DHCP-first then static; service
  constant `0x0112`; closed `FC1003_RMII` core — replaced by **LiteEth** here.)
- Keepalive / init packet (sent each ReadPipe): `01 00 01 00 80 02 23 77` = write CORE RW
  `0x02` value `0x0001` (inactivity-timer enable).
- Read-complete marker (seen just before the buffer): `0xeffffff3` then `0xdeceffff`
  (the FIFO's idle fire `0xffff`/`0xcede`).

> To confirm during implementation: exact byte/dword endianness end-to-end, the full RO
> register layout beyond the fields above, the device-string/URI form for UDP, and the parts
> of `device_fpga.c` past the first 100 KB (TLP/BAR RX dispatch detail).

---

## 4. Transport taxonomy

All transports sit behind the §3.1 dword seam. PCILeech transports reduce to **four classes**;
"USB-C" on CaptainDMA is just FT601 behind a USB-C connector, and Thunderbolt (ZDMA/GBOX) is
PCIe-over-TB via a **separate, proprietary** FPGA (not reimplementable here).

| Host path | LiteX block | LeechCore support | Portable? | ~BW | Tier |
|---|---|---|---|---|---|
| **Ethernet UDP** (incl. **SFP→host NIC**) | LiteEth UDP (any PHY incl. SFP 1000BASE-X/10GBASE-R) | ✅ UDP (port 28474) | ✅ any LiteEth board | ~100 MB/s @1G | **1** |
| **FT601 USB3** | `FT601Sync` (already in repo) | ✅ FT601 | needs FT601 chip | 200–400 MB/s | **2** |
| **FT2232H/FT232H USB2** | LiteX `FT245PHY{A}synchronous` | ✅ FT2232 | needs FTDI chip (common) | ~30 MB/s | 3 |
| **SFP↔SFP bridge (2 boards)** | **LiteICLink** (raw SerDes) or LiteEth-over-SFP | ✅ via converter board | 2-board | ≤10G link | 3 |
| **Soft-USB (USB-C→FPGA)** | `usb2_phy` + LUNA CDC | ❌ not native → needs host shim | GPIO-only boards | tens MB/s | 4 |
| **PCIe-to-host / Thunderbolt** | 2nd LitePCIe endpoint | ✅ (TB=PCIe) but needs bridge HW | dual-PCIe only | very high | 5 |

Notes:
- **FT601 re-unifies the legacy boards:** the original Screamer hardware already has an FT601
  and this repo already has `FT601Sync`; running the PCILeech dword protocol over it makes the
  original board a real PCILeech device over USB3 — "keep originals building" and
  "PCILeech-compatible" stop being separate tracks for those boards.
- **SFP, single board** = the Ethernet transport over an SFP PHY (DAC straight to a host NIC).
  The **Acorn Mini is SFP-only**, so this is its native path — delivered for free by Tier 1.
- **SFP, two boards** = the open, ZDMA-style split: Board A = full PCILeech SoC shipping the
  dword stream over SerDes; **Board B = stateless media converter** (SerDes-dwords ⇄ UDP/USB).
  Keep all protocol/firmware on Board A. Candidate building block: **LiteICLink** (see §7).

---

## 5. Per-board plan

### 5.1 Acorn CLE215+ / Baseboard Mini (Xilinx `xc7a200t`) — lowest risk
- PCIe PHY: `S7PCIEPHY` (64-bit), raw-TLP path as today.
- Transport: **Ethernet over SFP** (`A7_1000BASEX`), shared **GTP QPLL** with PCIe —
  recipe already exists in litex-boards `litex_acorn_baseboard_mini` target.
- Reuse pattern closest to the proven NeTV2 design, with **LiteEth replacing the closed
  FC1003** behind the same dword-FIFO interface.

### 5.2 Sipeed Tang Mega 138K Pro (Gowin `GW5AST-138`) — research track
- PCIe PHY: `GW5APCIEPHY` (**256-bit, big-endian**, downloaded netlist) → fast-path needs a
  width/endianness adapter vs the 64-bit S7 path.
- Transport: RGMII Gigabit (dock has `eth`) → LiteEth UDP; same protocol as the Acorn.
- No PCILeech precedent on Gowin — reachable **only** because the protocol lives in portable C
  + a family-independent fast-path; the per-board delta is the PHY + the 256-bit adapter.

### 5.3 Legacy `pcie_screamer`, `pcie_screamer_m2` (FT601)
- Kept building unchanged. Optionally upgraded to the PCILeech protocol over their existing
  FT601 (§4) to join the PCILeech family.

---

## 6. Multi-target project structure

litex-boards idiom: thin per-target files over a shared core. The transport, PHY and CRG are
seams; the protocol core and firmware are shared.

```
pcie_dma_engine/       # Python package
  __init__.py          # library API: create_soc(), BOARDS
  __main__.py          # CLI: python -m pcie_dma_engine --board <name>
  gateware/
    core.py            # PCIeDMAEngineSoC base (protocol-agnostic wiring)
    pcileech/          # PCILeech fast-path framing (dword mux, 0xE/0x77, resync) [BSD]
    transport/
      usb.py           # existing FT601Sync ⇄ dword seam
      eth.py           # LiteEth UDP  ⇄ dword seam        [later]
      ft245.py         # LiteX FT245 ⇄ dword seam         [later]
      serdes.py        # LiteICLink bridge (2-board)      [later]
    tlp.py, msi.py, usb.py, etherbone.py, ft601.py   # existing (legacy family)
  platforms/           # pcie_screamer*.py + acorn_baseboard_mini.py, tang_mega_138k_pro.py
  targets/             # thin: CRG + PHY + transport per board
firmware/              # device-side C control plane [GPLv3, isolated]
  pcileech_defs.h      # shared protocol definitions (mirror device_fpga.c)
```

**"All targets build" guard:** vendor toolchains (Vivado / Gowin) are needed for bitstreams,
but every target can be **elaborated to Verilog without them** (`builder.build(run=False)`) in
CI on every commit — the regression net that proves a new board did not break the others.
(The Gowin PHY `do_finalize` downloads `gw5apciephy.zip`; gate or pre-cache it offline.)

### Non-breaking migration order
1. Extract `PCIeDMAEngineSoC` + USB transport wrapper; re-express `pcie_screamer`/`_m2` as thin
   targets (**no behaviour change** — diff generated CSR/verilog).
2. Add `--board` dispatcher + elaboration smoke test (the safety net).
3. Add PCILeech fast-path + Ethernet transport + **Acorn** target (S7, 64-bit).
4. Add control-plane firmware + handshake; validate against LeechCore on the Acorn.
5. Add **Tang Mega** (GW5A PHY + 256-bit adapter) as a separate research track.
6. Optionally bring legacy FT601 boards onto the PCILeech protocol; add FT245, SFP-bridge.

---

## 7. Further perspectives (out of scope of this release)

Directions worth keeping in mind for later work, beyond the scope of this release:

- **liteeth PR #198 (RoCEv2):** an RDMA-over-UDP engine. It is not the PCILeech protocol, so
  it does not provide PCILeech compatibility by itself, but it is an interesting separate
  high-performance memory-transport path to revisit.
- **litepcie issue #110 (PCIe NTB):** a PCIe Non-Transparent Bridge is a different
  architecture (a true PCIe↔PCIe bridge) that could enable inter-domain setups in the future.
- **Thunderbolt (ZDMA/GBOX):** the host side relies on a separate proprietary FPGA, so it is
  not reproducible within this open project.

For the board-to-board-over-SFP path (§4), candidate building blocks to evaluate include
**LiteICLink** (raw SerDes) and **LiteEth 1000BASE-X / 10GBASE-R over SFP**.

---

## 8. Open questions to resolve during implementation

1. End-to-end endianness (dword byte order) across LiteEth ↔ fast path ↔ LitePCIe, especially
   the GW5A big-endian 256-bit PHY.
2. Config-space shadow latency: which TLP responses are CPU-served vs a small gateware
   responder backed by CPU-maintained tables (PCIe completion-timeout budget).
3. Exact feature scope of PCILeech to target first: DMA read/write only, vs full config-space
   emulation + device spoof.
4. Soft-USB host shim (if that path is ever pursued) — LeechCore has no native CDC-ACM device.
5. Remaining `device_fpga.c` detail past the first 100 KB (TLP/BAR RX dispatch).

---

## 9. Immediate next step

Phase 1 of §6: the **non-breaking refactor** — extract `PCIeDMAEngineSoC` + the FT601 transport
wrapper, re-express the two legacy targets as thin targets, add the `--board` dispatcher (`python -m pcie_dma_engine`) and
the elaboration smoke test — so the regression net exists before any new board or the PCILeech
fast path lands.
