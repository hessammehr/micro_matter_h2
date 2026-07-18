#!/usr/bin/env python3
"""Commission and control one Matter-over-Thread light through an ESP32-H2."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Final

import serial

SLIP_END: Final = 0xC0
SLIP_ESC: Final = 0xDB
SLIP_ESC_END: Final = 0xDC
SLIP_ESC_ESC: Final = 0xDD
FRAME_IPV6_TO_H2: Final = 0x01
FRAME_IPV6_TO_HOST: Final = 0x02
FRAME_STATUS_REQUEST: Final = 0x10
FRAME_STATUS_RESPONSE: Final = 0x11

TUNSETIFF: Final = 0x400454CA
IFF_TUN: Final = 0x0001
IFF_NO_PI: Final = 0x1000
TUN_NAME: Final = "matter0"
ROLE_NAMES: Final = ["disabled", "detached", "child", "router", "leader"]
ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"


@dataclass(frozen=True)
class RadioStatus:
    version: int
    role: int
    mleid: ipaddress.IPv6Address
    dataset: bytes
    link_local: ipaddress.IPv6Address | None = None
    injected_packets: int = 0
    rejected_packets: int = 0
    received_packets: int = 0
    last_inject_error: int = 0
    child_count: int = 0
    neighbor_count: int = 0
    peer_rloc16: int = 0xFFFF

    @property
    def role_name(self) -> str:
        return ROLE_NAMES[self.role] if self.role < len(ROLE_NAMES) else str(self.role)

    @property
    def mesh_prefix(self) -> ipaddress.IPv6Network:
        return ipaddress.IPv6Network((self.mleid, 64), strict=False)

    @property
    def peer_address(self) -> ipaddress.IPv6Address | None:
        if self.peer_rloc16 == 0xFFFF:
            return None
        iid = 0x000000FFFE000000 | self.peer_rloc16
        return ipaddress.IPv6Address(int(self.mesh_prefix.network_address) | iid)


class SlipDecoder:
    def __init__(self) -> None:
        self.data = bytearray()
        self.escaped = False

    def feed(self, data: bytes):
        for byte in data:
            if byte == SLIP_END:
                if self.data:
                    yield bytes(self.data)
                self.data.clear()
                self.escaped = False
            elif self.escaped:
                if byte == SLIP_ESC_END:
                    self.data.append(SLIP_END)
                elif byte == SLIP_ESC_ESC:
                    self.data.append(SLIP_ESC)
                else:
                    self.data.clear()
                self.escaped = False
            elif byte == SLIP_ESC:
                self.escaped = True
            elif len(self.data) <= 1281:
                self.data.append(byte)
            else:
                self.data.clear()


def slip_encode(frame: bytes) -> bytes:
    output = bytearray((SLIP_END,))
    for byte in frame:
        if byte == SLIP_END:
            output.extend((SLIP_ESC, SLIP_ESC_END))
        elif byte == SLIP_ESC:
            output.extend((SLIP_ESC, SLIP_ESC_ESC))
        else:
            output.append(byte)
    output.append(SLIP_END)
    return bytes(output)


def sudo(*command: str) -> None:
    subprocess.run(["sudo", *command], check=True)


def configure_tun(status: RadioStatus) -> int:
    """Create a host endpoint for the H2's mesh-local IPv6 address.

    No forwarding is enabled and no Ethernet/Wi-Fi route is added.
    """
    if not Path(f"/sys/class/net/{TUN_NAME}").exists():
        user = os.environ.get("USER", str(os.getuid()))
        sudo("ip", "tuntap", "add", "dev", TUN_NAME, "mode", "tun", "user", user)

    sudo("ip", "link", "set", "dev", TUN_NAME, "mtu", "1280", "multicast", "on", "up")
    sudo("ip", "-6", "addr", "flush", "dev", TUN_NAME, "scope", "global")
    sudo("ip", "-6", "addr", "flush", "dev", TUN_NAME, "scope", "link")
    sudo("ip", "-6", "addr", "add", f"{status.mleid}/64", "dev", TUN_NAME, "nodad")
    if status.link_local is not None:
        sudo(
            "ip", "-6", "addr", "add", f"{status.link_local}/64",
            "dev", TUN_NAME, "scope", "link", "nodad",
        )
    sudo("ip", "-6", "route", "replace", str(status.mesh_prefix), "dev", TUN_NAME)

    fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
    request = struct.pack("16sH", TUN_NAME.encode(), IFF_TUN | IFF_NO_PI)
    fcntl.ioctl(fd, TUNSETIFF, request)
    return fd


def remove_tun() -> None:
    if Path(f"/sys/class/net/{TUN_NAME}").exists():
        sudo("ip", "link", "delete", "dev", TUN_NAME)


def packet_summary(packet: bytes) -> str:
    if len(packet) < 40:
        return f"short IPv6 packet ({len(packet)} bytes)"
    source = ipaddress.IPv6Address(packet[8:24])
    destination = ipaddress.IPv6Address(packet[24:40])
    protocol = packet[6]
    detail = ""
    if protocol == socket.IPPROTO_UDP and len(packet) >= 48:
        source_port, destination_port = struct.unpack("!HH", packet[40:44])
        detail = f" UDP {source_port}→{destination_port}"
    return f"{source}→{destination}{detail} ({len(packet)} bytes)"


def _internet_checksum(data: bytes) -> int:
    if len(data) & 1:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    result = (~total) & 0xFFFF
    return result or 0xFFFF


def _update_udp_checksum(packet: bytearray) -> None:
    udp_length = struct.unpack("!H", packet[44:46])[0]
    packet[46:48] = b"\0\0"
    pseudo_header = (
        bytes(packet[8:40])
        + struct.pack("!I", udp_length)
        + b"\0\0\0"
        + bytes((socket.IPPROTO_UDP,))
    )
    checksum = _internet_checksum(pseudo_header + bytes(packet[40 : 40 + udp_length]))
    packet[46:48] = struct.pack("!H", checksum)


def translate_mdns_to_thread(packet: bytes, mleid: ipaddress.IPv6Address) -> bytes:
    """Map host link-local mDNS onto Thread realm-local multicast."""
    if (
        len(packet) >= 48
        and packet[6] == socket.IPPROTO_UDP
        and packet[24:40] == socket.inet_pton(socket.AF_INET6, "ff02::fb")
        and struct.unpack("!H", packet[42:44])[0] == 5353
    ):
        translated = bytearray(packet)
        translated[8:24] = mleid.packed
        translated[24:40] = socket.inet_pton(socket.AF_INET6, "ff03::fb")
        _update_udp_checksum(translated)
        return bytes(translated)
    return packet


def translate_mdns_to_host(packet: bytes) -> bytes:
    """Map Thread realm-local mDNS back to the group joined by Linux Matter."""
    if (
        len(packet) >= 48
        and packet[6] == socket.IPPROTO_UDP
        and packet[24:40] == socket.inet_pton(socket.AF_INET6, "ff03::fb")
        and struct.unpack("!H", packet[42:44])[0] == 5353
    ):
        translated = bytearray(packet)
        translated[24:40] = socket.inet_pton(socket.AF_INET6, "ff02::fb")
        _update_udp_checksum(translated)
        return bytes(translated)
    return packet


def _dns_name(labels: list[bytes]) -> bytes:
    return b"".join(bytes((len(label),)) + label for label in labels) + b"\0"


def synthesize_operational_mdns(
    query_packet: bytes, peer_address: ipaddress.IPv6Address
) -> bytes | None:
    """Answer an operational Matter query with a known direct Thread locator.

    CASE still authenticates the peer. This substitutes only for broken mDNS
    advertisement on the single-device test mesh.
    """
    if (
        len(query_packet) < 64
        or query_packet[6] != socket.IPPROTO_UDP
        or struct.unpack("!H", query_packet[42:44])[0] != 5353
    ):
        return None
    dns = query_packet[48:]
    if len(dns) < 17 or struct.unpack("!H", dns[4:6])[0] == 0:
        return None

    labels: list[bytes] = []
    offset = 12
    while offset < len(dns) and dns[offset] != 0:
        length = dns[offset]
        offset += 1
        if length > 63 or offset + length > len(dns):
            return None
        labels.append(dns[offset : offset + length])
        offset += length
    if offset + 5 > len(dns) or len(labels) < 4:
        return None
    if labels[-3:] != [b"_matter", b"_tcp", b"local"]:
        return None

    instance = _dns_name(labels)
    target = _dns_name([b"matter-h2-peer", b"local"])
    cache_flush_in = 0x8001
    ttl = 120

    def rr(name: bytes, rr_type: int, data: bytes) -> bytes:
        return name + struct.pack("!HHIH", rr_type, cache_flush_in, ttl, len(data)) + data

    srv = rr(instance, 33, struct.pack("!HHH", 0, 0, 5540) + target)
    txt_data = b"\x08SII=5000\x07SAI=300\x08SAT=4000"
    txt = rr(instance, 16, txt_data)
    aaaa = rr(target, 28, peer_address.packed)
    dns_response = struct.pack("!HHHHHH", 0, 0x8400, 0, 2, 0, 1) + srv + txt + aaaa

    udp_length = 8 + len(dns_response)
    response = bytearray(40 + udp_length)
    response[0] = 0x60
    response[4:8] = struct.pack("!HBB", udp_length, socket.IPPROTO_UDP, 64)
    response[8:24] = peer_address.packed
    response[24:40] = query_packet[8:24]
    source_port = struct.unpack("!H", query_packet[40:42])[0]
    response[40:48] = struct.pack("!HHHH", 5353, source_port, udp_length, 0)
    response[48:] = dns_response
    _update_udp_checksum(response)
    return bytes(response)


class H2Bridge:
    def __init__(self, port: str) -> None:
        self.serial = serial.Serial(port, baudrate=115200, timeout=0.2, exclusive=True)
        self.trace = os.environ.get("MATTER_H2_TRACE") == "1"
        self.peer_address: ipaddress.IPv6Address | None = None
        self.decoder = SlipDecoder()
        self.status: RadioStatus | None = None
        self.tun_fd: int | None = None
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.write_lock = threading.Lock()

    def send_frame(self, frame_type: int, payload: bytes = b"") -> None:
        frame = slip_encode(bytes((frame_type,)) + payload)
        with self.write_lock:
            self.serial.write(frame)
            self.serial.flush()

    @staticmethod
    def parse_status(frame: bytes) -> RadioStatus:
        if len(frame) < 20:
            raise RuntimeError("short status frame from H2")
        version, role, dataset_len = frame[1:4]
        base_length = 20 + dataset_len
        link_local = None
        if version == 1 and len(frame) == base_length:
            diagnostics = (0, 0, 0, 0, 0)
        elif version == 2 and len(frame) == base_length + 14:
            diagnostics = struct.unpack("<IIIBB", frame[base_length:])
        elif version == 3 and len(frame) == base_length + 30:
            link_local = ipaddress.IPv6Address(frame[base_length : base_length + 16])
            diagnostics = (*struct.unpack("<IIIBB", frame[base_length + 16 :]), 0)
        elif version == 4 and len(frame) == base_length + 31:
            link_local = ipaddress.IPv6Address(frame[base_length : base_length + 16])
            diagnostics = (*struct.unpack("<IIIBBB", frame[base_length + 16 :]), 0xFFFF)
        elif version == 5 and len(frame) == base_length + 33:
            link_local = ipaddress.IPv6Address(frame[base_length : base_length + 16])
            diagnostics = struct.unpack("<IIIBBBH", frame[base_length + 16 :])
        else:
            raise RuntimeError("invalid H2 status length or version")
        return RadioStatus(
            version=version,
            role=role,
            mleid=ipaddress.IPv6Address(frame[4:20]),
            dataset=frame[20:base_length],
            link_local=link_local,
            injected_packets=diagnostics[0],
            rejected_packets=diagnostics[1],
            received_packets=diagnostics[2],
            last_inject_error=diagnostics[3],
            child_count=diagnostics[4],
            neighbor_count=diagnostics[5] if len(diagnostics) > 5 else 0,
            peer_rloc16=diagnostics[6] if len(diagnostics) > 6 else 0xFFFF,
        )

    def wait_for_status(self, timeout: float = 15) -> RadioStatus:
        deadline = time.monotonic() + timeout
        next_request = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_request:
                self.send_frame(FRAME_STATUS_REQUEST)
                next_request = time.monotonic() + 1
            for frame in self.decoder.feed(self.serial.read(512)):
                if frame[0] == FRAME_STATUS_RESPONSE:
                    self.status = self.parse_status(frame)
                    self.peer_address = self.status.peer_address
                    if self.status.version not in (1, 2, 3, 4, 5):
                        raise RuntimeError(f"unsupported bridge protocol {self.status.version}")
                    return self.status
        raise TimeoutError("H2 did not answer; ensure the direct-bridge firmware is flashed")

    def start(self) -> RadioStatus:
        status = self.wait_for_status()
        self.tun_fd = configure_tun(status)
        self.thread = threading.Thread(target=self._relay, name="h2-ipv6", daemon=True)
        self.thread.start()
        return status

    def _handle_frame(self, frame: bytes) -> None:
        if not frame:
            return
        if frame[0] == FRAME_IPV6_TO_HOST and self.tun_fd is not None:
            packet = translate_mdns_to_host(frame[1:])
            if 40 <= len(packet) <= 1280 and packet[0] >> 4 == 6:
                if self.trace:
                    print(f"Thread→host {packet_summary(packet)}", file=sys.stderr)
                os.write(self.tun_fd, packet)
        elif frame[0] == FRAME_STATUS_RESPONSE:
            self.status = self.parse_status(frame)
            self.peer_address = self.status.peer_address
            if self.trace:
                print(
                    f"Thread state: {self.status.role_name}, "
                    f"{self.status.child_count} children, "
                    f"{self.status.neighbor_count} neighbors, "
                    f"peer {self.peer_address or 'none'}",
                    file=sys.stderr,
                )

    def _relay(self) -> None:
        assert self.tun_fd is not None
        next_status = 0.0
        while not self.stop_event.is_set():
            if self.trace and time.monotonic() >= next_status:
                self.send_frame(FRAME_STATUS_REQUEST)
                next_status = time.monotonic() + 2
            readable, _, _ = select.select([self.serial.fileno(), self.tun_fd], [], [], 0.5)
            if self.serial.fileno() in readable:
                data = self.serial.read(4096)
                for frame in self.decoder.feed(data):
                    self._handle_frame(frame)
            if self.tun_fd in readable:
                try:
                    packet = os.read(self.tun_fd, 1280)
                except BlockingIOError:
                    continue
                if packet:
                    assert self.status is not None
                    if self.peer_address is not None:
                        response = synthesize_operational_mdns(packet, self.peer_address)
                        if response is not None:
                            if self.trace:
                                print(
                                    f"synthetic DNS {packet_summary(response)}",
                                    file=sys.stderr,
                                )
                            os.write(self.tun_fd, response)
                    # Send Thread mDNS at both scopes. Some devices answer
                    # link-local queries while others follow the Thread realm-local
                    # convention. Non-mDNS traffic produces only one packet.
                    translated = translate_mdns_to_thread(packet, self.status.mleid)
                    packets = (packet,) if translated == packet else (packet, translated)
                    for thread_packet in packets:
                        if self.trace:
                            print(
                                f"host→Thread {packet_summary(thread_packet)}",
                                file=sys.stderr,
                            )
                        self.send_frame(FRAME_IPV6_TO_H2, thread_packet)

    def close(self) -> None:
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=2)
        if self.tun_fd is not None:
            os.close(self.tun_fd)
        self.serial.close()

    def __enter__(self) -> H2Bridge:
        return self

    def __exit__(self, *_args) -> None:
        self.close()


class MatterController:
    def __init__(self, dataset: bytes, bluetooth_adapter: int = 0) -> None:
        import chip.CertificateAuthority
        import chip.native
        from chip.ChipStack import ChipStack

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        chip.native.Init(bluetooth_adapter)
        self.stack = ChipStack(
            persistentStoragePath=str(STATE_DIR / "chip.json"),
            # This process is only a controller; it must not advertise itself as
            # a Matter server on the Pi's infrastructure interfaces.
            enableServerInteractions=False,
        )
        self.ca_manager = chip.CertificateAuthority.CertificateAuthorityManager(
            chipStack=self.stack
        )
        self.ca_manager.LoadAuthoritiesFromStorage()
        if self.ca_manager.activeCaList:
            authority = self.ca_manager.activeCaList[0]
        else:
            authority = self.ca_manager.NewCertificateAuthority()
            authority.maximizeCertChains = False
        if authority.adminList:
            admin = authority.adminList[0]
        else:
            admin = authority.NewFabricAdmin(vendorId=0xFFF1, fabricId=1)
        self.controller = admin.NewController(
            paaTrustStorePath=str(STATE_DIR / "paa-root-certs")
        )
        self.controller.SetThreadOperationalDataset(threadOperationalDataset=dataset)

    async def commission(self, code: str, node_id: int) -> None:
        from chip.discovery import DiscoveryType

        result = await self.controller.CommissionWithCode(
            setupPayload=code,
            nodeid=node_id,
            discoveryType=DiscoveryType.DISCOVERY_ALL,
        )
        if result != node_id:
            raise RuntimeError(f"commissioned unexpected node ID {result}")

    async def set_on(self, node_id: int, endpoint: int, value: bool) -> None:
        from chip.clusters import Objects as Clusters

        command = Clusters.OnOff.Commands.On() if value else Clusters.OnOff.Commands.Off()
        await self.controller.SendCommand(
            nodeid=node_id,
            endpoint=endpoint,
            payload=command,
        )

    def close(self) -> None:
        self.controller.Shutdown()
        self.ca_manager.Shutdown()
        self.stack.Shutdown()


def stored_node_id() -> int:
    path = STATE_DIR / "device.json"
    if not path.exists():
        raise RuntimeError("no commissioned device; run the commission command first")
    return int(json.loads(path.read_text())["node_id"])


def store_node_id(node_id: int) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "device.json").write_text(json.dumps({"node_id": node_id}) + "\n")


async def fetch_paa_certificates() -> None:
    from matter_server.server.helpers.paa_certificates import fetch_certificates

    await fetch_certificates(
        STATE_DIR / "paa-root-certs",
        fetch_test_certificates=False,
        fetch_production_certificates=True,
    )


async def run_matter(args: argparse.Namespace, status: RadioStatus) -> None:
    if status.role not in (2, 3, 4):
        raise RuntimeError(
            f"Thread network is not ready (H2 is {status.role_name}); retry in a few seconds"
        )
    if not status.dataset:
        raise RuntimeError("H2 returned no active Thread dataset")

    if args.command == "commission":
        sudo("rfkill", "unblock", "bluetooth")
        await fetch_paa_certificates()

    controller = MatterController(status.dataset, bluetooth_adapter=0)
    try:
        if args.command == "commission":
            code = args.code or input("Matter setup code: ").strip()
            code = code.replace(" ", "")
            await controller.commission(code, args.node)
            store_node_id(args.node)
            print(f"Commissioned as node {args.node}")
        elif args.command in ("on", "off"):
            node_id = args.node if args.node is not None else stored_node_id()
            await controller.set_on(node_id, args.endpoint, args.command == "on")
            print(f"Node {node_id} endpoint {args.endpoint}: {args.command}")
    finally:
        controller.close()


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", default="/dev/ttyACM0", help="H2 USB serial port")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("radio", help="show H2 Thread status without creating a TUN interface")
    commission = sub.add_parser("commission", help="commission the bulb over BLE + Thread")
    commission.add_argument("--code", help="Matter manual/QR setup code; prompted if omitted")
    commission.add_argument("--node", type=int, default=1)
    for name in ("on", "off"):
        command = sub.add_parser(name, help=f"turn the commissioned bulb {name}")
        command.add_argument("--node", type=int)
        command.add_argument("--endpoint", type=int, default=1)
    sub.add_parser("cleanup", help="remove the local matter0 TUN interface")
    return parser


async def async_main(args: argparse.Namespace) -> None:
    if args.command == "cleanup":
        remove_tun()
        return

    with H2Bridge(args.port) as bridge:
        if args.command == "radio":
            status = bridge.wait_for_status()
        else:
            status = bridge.start()
        print(
            f"H2: {status.role_name}, ML-EID {status.mleid}, "
            f"link-local {status.link_local or 'unknown'}, "
            f"dataset {len(status.dataset)} bytes, children {status.child_count}, "
            f"neighbors {status.neighbor_count}, peer {status.peer_address or 'none'}, "
            f"IPv6 host→Thread {status.injected_packets} ok/{status.rejected_packets} rejected "
            f"(last OT error {status.last_inject_error}), Thread→host {status.received_packets}"
        )
        if args.command != "radio":
            await run_matter(args, status)


def main() -> None:
    args = make_parser().parse_args()
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass
    except Exception as err:
        print(f"error: {err}", file=sys.stderr)
        raise SystemExit(1) from err


if __name__ == "__main__":
    main()
