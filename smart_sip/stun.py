"""
Minimal STUN client (RFC 5389) to discover public (IP, port) for NAT traversal.
Used so the engine can advertise the correct address without manual port forwarding.
"""

import socket
import struct
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# RFC 5389
STUN_MAGIC = 0x2112A442
BINDING_REQUEST = 0x0001
BINDING_RESPONSE = 0x0101
ATTR_XOR_MAPPED_ADDRESS = 0x0020
ATTR_MAPPED_ADDRESS = 0x0001

# Default STUN servers (no auth required)
DEFAULT_STUN_SERVERS = [
    ("stun.l.google.com", 19302),
    ("stun1.l.google.com", 19302),
    ("stun.stunprotocol.org", 3478),
]


def _make_binding_request() -> bytes:
    """Build STUN Binding Request (RFC 5389)."""
    import random
    # Header: 2B type, 2B length, 4B magic, 12B transaction ID
    tid = struct.pack("!III", random.getrandbits(32), random.getrandbits(32), random.getrandbits(32))
    return struct.pack("!HHI", BINDING_REQUEST, 0, STUN_MAGIC) + tid


def _parse_xor_mapped_address(data: bytes, magic: int) -> Optional[Tuple[str, int]]:
    """Parse XOR-MAPPED-ADDRESS attribute (RFC 5389)."""
    if len(data) < 8:
        return None
    # 1 byte reserved, 1 byte family, 2 bytes port, 4 bytes address
    family = data[1]
    if family != 0x01:  # IPv4
        return None
    port_xor = struct.unpack("!H", data[2:4])[0]
    addr_xor = struct.unpack("!I", data[4:8])[0]
    port = port_xor ^ (magic >> 16)
    addr_int = addr_xor ^ magic
    addr = socket.inet_ntoa(struct.pack("!I", addr_int))
    return (addr, port)


def _parse_mapped_address(data: bytes) -> Optional[Tuple[str, int]]:
    """Parse MAPPED-ADDRESS attribute (no XOR)."""
    if len(data) < 8:
        return None
    family = data[1]
    if family != 0x01:
        return None
    port = struct.unpack("!H", data[2:4])[0]
    addr = socket.inet_ntoa(data[4:8])
    return (addr, port)


def _parse_binding_response(data: bytes, magic: int) -> Optional[Tuple[str, int]]:
    """Parse STUN Binding Response, return (ip, port) from XOR-MAPPED or MAPPED-ADDRESS."""
    if len(data) < 20:
        return None
    msg_type = struct.unpack("!H", data[0:2])[0]
    if (msg_type & 0x3FFF) != (BINDING_RESPONSE & 0xFFFF):
        return None
    length = struct.unpack("!H", data[2:4])[0]
    pos = 20
    while pos + 4 <= 20 + length:
        attr_type = struct.unpack("!H", data[pos : pos + 2])[0]
        attr_len = struct.unpack("!H", data[pos + 2 : pos + 4])[0]
        pos += 4
        if pos + attr_len > len(data):
            break
        value = data[pos : pos + attr_len]
        pos += attr_len
        if (attr_len % 4) != 0:
            pos += 4 - (attr_len % 4)
        if attr_type == ATTR_XOR_MAPPED_ADDRESS:
            result = _parse_xor_mapped_address(value, magic)
            if result:
                return result
        elif attr_type == ATTR_MAPPED_ADDRESS:
            result = _parse_mapped_address(value)
            if result:
                return result
    return None


def get_mapped_address(sock: socket.socket, stun_host: str = None, stun_port: int = None) -> Optional[Tuple[str, int]]:
    """
    Discover the public (IP, port) for the given bound UDP socket using STUN.
    Uses the socket's existing bind so the NAT mapping is for the real SIP port.
    Returns (public_ip, public_port) or None on failure.
    """
    host = stun_host or DEFAULT_STUN_SERVERS[0][0]
    port = stun_port if stun_port is not None else DEFAULT_STUN_SERVERS[0][1]
    try:
        req = _make_binding_request()
        sock.settimeout(3.0)
        sock.sendto(req, (host, port))
        data, _ = sock.recvfrom(512)
        sock.settimeout(0.5)  # restore
        if len(data) < 20:
            return None
        magic = struct.unpack("!I", data[4:8])[0]
        return _parse_binding_response(data, magic)
    except Exception as e:
        logger.debug(f"STUN {host}:{port} failed: {e}")
        return None


def get_mapped_address_try_servers(sock: socket.socket) -> Optional[Tuple[str, int]]:
    """Try default STUN servers until one returns a result."""
    for host, port in DEFAULT_STUN_SERVERS:
        result = get_mapped_address(sock, host, port)
        if result:
            return result
    return None
