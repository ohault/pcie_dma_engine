#!/usr/bin/env python3

# License: BSD

# Full-stack Ethernet (UDP) simulation: host UDP model <-> real LiteEthUDPIPCore (inside
# EthTransport) <-> PCILeech engine. Unlike test_transport_eth.py (which drives the engine through
# an isolated StrideConverter), this exercises the WHOLE LiteEth MAC/IP/UDP packetizer path, so it
# catches on-wire frame-formation bugs: byte order, UDP length, and byte-enables.
#
# Regression guard for the `be` (byte-enable) bug that made UDP port 28474 silent on hardware while
# ICMP answered ping: EthTransport did not set port.sink.be, so the dw=32->dw=8 down-converter
# dropped the last word and produced a bad Ethernet FCS; the host NIC silently discarded every
# reply. The LiteEth MAC *model* validates the FCS (raises on a bad one), so this test fails if the
# device emits a malformed frame and passes only when the reply is a well-formed datagram the host
# can parse (magic 0xab89, version 1.0, device id 0x01).
#
# Needs LiteEth's simulation models (liteeth/test/model); skips cleanly if they are not importable.
# Run: python3 test/test_transport_eth_fullstack.py

import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Locate LiteEth's test models relative to the installed liteeth package. LiteEth's models live in a
# package literally named `test` (liteeth/test/model), which collides with this repo's own `test`
# dir. Put LiteEth's `test` ahead of the repo root on sys.path so `test.model` resolves to LiteEth's;
# this file imports nothing from the repo's own test package, so shadowing it here is harmless.
try:
    import liteeth
    _liteeth_root = os.path.dirname(os.path.dirname(liteeth.__file__))  # dir that contains test/
    sys.path.insert(0, _liteeth_root)
    sys.path.insert(1, _REPO_ROOT)
    from test.model import phy, mac, arp, ip, udp  # noqa: E402
    _HAVE_MODELS = True
except Exception as e:  # pragma: no cover - environment dependent
    sys.path.insert(0, _REPO_ROOT)
    _HAVE_MODELS = False
    _IMPORT_ERR = e

from migen import *                                   # noqa: E402
from litex.gen.sim import run_simulation              # noqa: E402

from pcie_dma_engine.gateware.transport.eth import EthTransport      # noqa: E402
from pcie_dma_engine.gateware.pcileech.engine import PCILeechEngine  # noqa: E402

UDP_PORT  = 28474
HOST_PORT = 0x1234
VER_MAJOR, VER_MINOR, DEVICE_ID = 4, 12, 5


def _ip(s):
    from liteeth.common import convert_ip
    return convert_ip(s)


def read_cmd(addr, target=0x03):
    return [0, 0, 0, 0, (addr >> 8) & 0x3f, addr & 0xff, 0x10 | target, 0x77]


def parse_groups(data):
    resp = {}
    for i in range(0, len(data) - 31, 32):
        status = int.from_bytes(bytes(data[i:i+4]), "little")  # LeechCore reads dwords little-endian
        if (status & 0xf0000000) != (0xe << 28):
            continue
        for k in range(7):
            nib = (status >> (4*k)) & 0xf
            dw  = int.from_bytes(bytes(data[i+4+4*k:i+8+4*k]), "little")
            if (nib & 0x3) == 3:
                lo = dw & 0xffff
                addr = ((lo & 0xff) << 8) | ((lo >> 8) & 0xff)  # addr = byteswap(low 16)
                resp[addr] = (dw >> 16) & 0xffff                # data = high 16
    return resp


def build_dut():
    model_mac, dut_mac = 0x112233445566, 0x10e2d5000000
    model_ip, dut_ip   = _ip("192.168.1.101"), _ip("192.168.1.50")

    class CapUDP(udp.UDP):
        def __init__(self, ipmod, ip_address, replies):
            self.replies = replies
            udp.UDP.__init__(self, ipmod, ip_address, debug=False, loopback=False)
        def process(self, packet):
            self.replies.append(list(packet))

    class DUT(Module):
        def __init__(self):
            self.replies = []
            self.phy_model = phy.PHY(8, debug=False)
            self.mac_model = mac.MAC(self.phy_model, debug=False, loopback=False)
            self.arp_model = arp.ARP(self.mac_model, model_mac, model_ip, debug=False)
            self.ip_model  = ip.IP(self.mac_model, model_mac, model_ip, debug=False, loopback=False)
            self.udp_model = CapUDP(self.ip_model, model_ip, self.replies)
            self.submodules.transport = t = EthTransport(self.phy_model, dut_mac, dut_ip, 100000,
                                                        flush_timeout=64)
            self.submodules.engine = e = PCILeechEngine(VER_MAJOR, VER_MINOR, DEVICE_ID,
                                                       with_cfg_mgmt=False)
            self.comb += [t.source.connect(e.sink), e.source.connect(t.sink)]
            self.dut_ip = dut_ip

    return DUT()


def test_identity_fullstack():
    if not _HAVE_MODELS:
        print(f"[SKIP] LiteEth sim models not importable: {_IMPORT_ERR}")
        return

    dut = build_dut()

    def gen(dut):
        payload = read_cmd(0x00) + read_cmd(0x08) + read_cmd(0x0a)
        req = udp.UDPPacket(payload)
        req.src_port = HOST_PORT
        req.dst_port = UDP_PORT
        req.length   = len(payload) + 8
        req.checksum = 0
        dut.udp_model.send(req, target_ip=dut.dut_ip)
        for _ in range(20000):
            if dut.replies:
                break
            yield

    # The LiteEth MAC model raises ValueError on a bad FCS, so a malformed reply fails here.
    run_simulation(dut, {
        "sys":    [gen(dut)],
        "eth_tx": [dut.phy_model.phy_sink.generator(), dut.phy_model.generator()],
        "eth_rx": [dut.phy_model.phy_source.generator()],
    }, {"sys": 10, "eth_rx": 10, "eth_tx": 10})

    assert dut.replies, "no UDP reply from device (engine/transport silent)"
    payload = dut.replies[0]
    assert len(payload) == 32, f"reply payload {len(payload)} bytes, expected a full 32-byte group"
    resp = parse_groups(payload)
    assert resp.get(0x00) == 0xab89, f"magic {resp.get(0x00)} != 0xab89"
    assert (resp.get(0x08, 0) & 0xff) == VER_MAJOR
    assert (resp.get(0x0a, 0) & 0xff) == DEVICE_ID
    print(f"[OK] full-stack UDP: well-formed reply, magic=0xab89, version={VER_MAJOR}.{VER_MINOR}, "
          f"device_id={DEVICE_ID:#04x}")


if __name__ == "__main__":
    test_identity_fullstack()
    print("Full-stack Ethernet transport test passed.")
