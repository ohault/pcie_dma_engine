# Hardware validation — Acorn CLE215+ Mini (Ethernet)

How to bring up and validate the `acorn_baseboard_mini` target on real hardware. Everything in the
repo so far is verified by **simulation + elaboration only**; this is the procedure to validate it
on-wire against a real host.

## 1. Build & flash

```sh
# 1 Gbps (default) or 2.5G:
./build.py --board acorn_baseboard_mini --eth-speed 1G --build
./build.py --board acorn_baseboard_mini --load      # JTAG load (volatile)
# or --flash to program SPI flash
```

Needs Vivado for the Artix-7 bitstream. See the Acorn Ethernet (SFP) notes in the README for the
required SFP module per speed.

## 2. Network setup

- Connect `SFP0` to the host: a 1000BASE-X SFP (or 2.5G SFP for `--eth-speed 2.5G`), fiber or a
  passive SFP DAC, to a matching host NIC/switch port.
- The SoC's IP/MAC default to `192.168.1.50` / `0x10e2d5000000` (set in
  `targets/acorn_baseboard_mini.py`). Put the host NIC on the same subnet (e.g. `192.168.1.100`).
- Host UDP port used by the protocol: **28474**.

## 3. Sanity-check the handshake (no LeechCore)

`software/pcileech_udp_probe.py` speaks the PCILeech dword protocol directly over UDP and reads the
CORE identity — a quick way to confirm the link + control plane before involving LeechCore:

```sh
python3 software/pcileech_udp_probe.py 192.168.1.50
```

Expected:

```
received <N> bytes
  magic     : 0xab89 (OK)
  version   : 4.12
  device_id : 0x05
```

`magic = 0xab89` confirms the Ethernet transport, dword pairing/endianness, command decode, and the
return framer are all working end-to-end. If you get no bytes, check the SFP link, the IP/subnet,
and that the bitstream is loaded. The identity reports as a NeTV2 (device id 5, version 4.12): that
is the pcileech-fpga Ethernet/UDP board LeechCore recognizes for the `RAWUDP` transport, so it must
present a version >= 4 and a known device id or LeechCore rejects the v4 identity.

## 4. Run PCILeech / LeechCore

The board presents as a UDP FPGA device. Use PCILeech's FPGA-over-UDP device (see LeechCore's
`device_fpga` documentation for the exact `-device` string for your version, e.g. an `rawudp`/`fpga`
UDP form with the board IP). Start with a low-rate operation (device info) before DMA.

## 5. Current status & known gaps

Working in simulation, expected to work on hardware (validate with the probe first):

- Ethernet (SFP 1000BASE-X / 2500BASE-X) transport, UDP port 28474.
- PCILeech control plane: CORE register reads (identity/handshake) and writes (CMD register).
- TLP data plane: `pcie_phy` ↔ dword stream ↔ UDP, both directions (framer + inject).

Not yet implemented — expect these to be needed for full enumeration / DMA and to require
hardware iteration:

- **Config-space shadow — host side DONE, target side NOT FEASIBLE on this hard IP.** The
  host-editable config image + shadow command protocol are implemented and simulation-tested
  (command type `11` + address bit 14; `{4'b1100, addr, addr_lo, 0}` tag, no byteswap; 256-byte
  header, default device/vendor `0x0666`/`0x10EE`). *Feasibility finding:* making the **target
  machine** actually see this image requires the PCIe IP to route config TLPs to user logic
  (external config space). The Artix-7 7-series PCIe hard IP as wrapped by LitePCIe's `S7PCIEPHY`
  does **not** expose that — it offers only `cfg_mgmt` (read/modify its *own* config space) and
  `cfg_msg`, with config TLPs consumed internally. So target-side shadow presentation is not
  achievable here without regenerating the Xilinx PCIe IP in a different mode (out of scope); the
  shadow is host-read/write only.
- **PCIe live config (target `01`, `dcfg` path) — bridge IMPLEMENTED (validate framing).** A
  `cfg_mgmt` bridge serves host reads of the live PCIe config (e.g. device id at `0x0008`) via
  LitePCIe's `with_cfg_mgmt` interface, replying on the CFG channel. Simulation-tested with a fake
  `cfg_mgmt` responder. The exact v4 CFG reply framing (`{addr, byteswap(half)}` on `PORT_CFG` here)
  comes from a LeechCore session-helper not yet cross-checked — confirm it on hardware / capture.
- **CFG write path and the loopback channel** (type `10`) on the return path.
- **TLP dword ordering / byte-swap** through the width converters: confirm against LeechCore.

When validating, capture the first failing exchange (e.g. with `tcpdump`/Wireshark on UDP 28474) and
compare against the dword protocol in `doc/pcileech_porting_analysis.md` section 3.
