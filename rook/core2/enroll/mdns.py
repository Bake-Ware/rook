"""Finding a hub on the local network (spec 12): DNS-SD over mDNS, service
``_rook-hub._tcp``, just enough of RFC 6762/6763 for that one service.

The hub (with ``enroll: {discover: true, url: …}``) runs a :class:`Responder`
that answers PTR queries for the service with PTR, SRV and TXT records; the
TXT carries ``url=`` (where devices enroll) and ``hub=`` (its hub id).
:func:`discover` sends one query from an ephemeral port, so responders answer
it directly (a legacy unicast query, RFC 6762 6.7), and lists what came back.

What it finds is a hint, not trust: the device still enrolls over TLS and
the person approves its code on their own hub, which shows the hub id.
"""
from __future__ import annotations

import asyncio
import logging
import socket
import struct
import time
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger("rook.hub.mdns")

GROUP, PORT = "224.0.0.251", 5353
SERVICE = "_rook-hub._tcp.local"
PTR, TXT, SRV, ANY = 12, 16, 33, 255
IN, QU, FLUSH = 1, 0x8000, 0x8000
TTL = 120


def _name(name: str) -> bytes:
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")[:63]
        out += bytes([len(raw)]) + raw
    return out + b"\0"


def _read_name(msg: bytes, i: int) -> tuple[str, int]:
    """A (possibly compressed) name at ``i``; returns it and the offset after it."""
    labels, end, hops = [], None, 0
    while True:
        n = msg[i]
        if n & 0xC0 == 0xC0:              # a pointer
            if end is None:
                end = i + 2
            i = ((n & 0x3F) << 8) | msg[i + 1]
            hops += 1
            if hops > 32:
                raise ValueError("name pointer loop")
            continue
        if n == 0:
            return ".".join(labels), (end if end is not None else i + 1)
        labels.append(msg[i + 1:i + 1 + n].decode("utf-8", "replace"))
        i += 1 + n


def _rr(name: str, rtype: int, data: bytes, flush: bool = False) -> bytes:
    return _name(name) + struct.pack("!HHIH", rtype, IN | (FLUSH if flush else 0), TTL, len(data)) + data


def query(qid: int = 0) -> bytes:
    """One PTR question for the service, unicast response asked for."""
    return struct.pack("!6H", qid, 0, 1, 0, 0, 0) + _name(SERVICE) + struct.pack("!HH", PTR, IN | QU)


def answer(qid: int, url: str, hub_id: str, question: bool) -> bytes:
    """PTR, SRV and TXT for this hub. A legacy unicast reply echoes the
    query's id and question."""
    instance = f"Rook hub {hub_id[:8]}.{SERVICE}"
    parts = urlsplit(url)
    port = parts.port or (443 if parts.scheme == "https" else 80)
    host = f"{(parts.hostname or 'rook').split('.')[0]}.local"
    txt = b"".join(bytes([len(s)]) + s for s in (f"url={url}".encode()[:255], f"hub={hub_id}".encode()))
    records = [_rr(SERVICE, PTR, _name(instance)),
               _rr(instance, SRV, struct.pack("!HHH", 0, 0, port) + _name(host), flush=True),
               _rr(instance, TXT, txt, flush=True)]
    head = struct.pack("!6H", qid, 0x8400, 1 if question else 0, len(records), 0, 0)
    return head + (_name(SERVICE) + struct.pack("!HH", PTR, IN) if question else b"") + b"".join(records)


def asks_for_us(msg: bytes) -> tuple[bool, bool]:
    """(is a query for the service, wants a unicast reply)."""
    qid, flags, qd, *_ = struct.unpack("!6H", msg[:12])
    if flags & 0x8000:
        return False, False                # a response, not a query
    i, ours, qu = 12, False, False
    for _ in range(qd):
        name, i = _read_name(msg, i)
        qtype, qclass = struct.unpack("!HH", msg[i:i + 4])
        i += 4
        if name.lower() == SERVICE.lower() and qtype in (PTR, ANY):
            ours, qu = True, qu or bool(qclass & QU)
    return ours, qu


def parse(msg: bytes) -> list[dict]:
    """The hubs a response describes: [{url, hub, name}] from its TXT records."""
    _, flags, qd, an, ns, ar = struct.unpack("!6H", msg[:12])
    if not flags & 0x8000:
        return []
    i = 12
    for _ in range(qd):
        _, i = _read_name(msg, i)
        i += 4
    out = []
    for _ in range(an + ns + ar):
        name, i = _read_name(msg, i)
        rtype, _, _, n = struct.unpack("!HHIH", msg[i:i + 10])
        i += 10
        data, i = msg[i:i + n], i + n
        if rtype != TXT or not name.lower().endswith(SERVICE.lower()):
            continue
        fields, j = {}, 0
        while j < len(data):
            s = data[j + 1:j + 1 + data[j]].decode("utf-8", "replace")
            j += 1 + data[j]
            k, _, v = s.partition("=")
            fields[k] = v
        if fields.get("url"):
            out.append({"url": fields["url"], "hub": fields.get("hub", ""), "name": name.split(".")[0]})
    return out


def discover(timeout: float = 2.0, addr: tuple[str, int] = (GROUP, PORT)) -> list[dict]:
    """Ask the local network for Rook hubs; [{url, hub, name}], one per url."""
    found: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.bind(("", 0))
        s.sendto(query(), addr)
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            s.settimeout(left)
            try:
                msg, _ = s.recvfrom(9000)
            except (socket.timeout, OSError):
                break
            try:
                for hub in parse(msg):
                    found.setdefault(hub["url"], hub)
            except (ValueError, IndexError, struct.error):
                continue
    return list(found.values())


class Responder(asyncio.DatagramProtocol):
    """Answers ``_rook-hub._tcp`` queries for one hub while the hub runs.
    ``port``/``bind``/``group`` are for tests (a loopback port, no multicast)."""

    def __init__(self, url: str, hub_id: str, *, port: int = PORT, bind: str = "", group: bool = True):
        self.url, self.hub_id = url, hub_id
        self.port, self.bind, self.group = port, bind, group
        self.transport: Any = None

    async def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):       # beside avahi or another responder
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        s.bind((self.bind, self.port))
        if self.group:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         socket.inet_aton(GROUP) + socket.inet_aton("0.0.0.0"))
        s.setblocking(False)
        self.transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(lambda: self, sock=s)
        self.port = s.getsockname()[1]
        log.info("discoverable", extra={"fields": {"service": SERVICE, "url": self.url}})

    async def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    def datagram_received(self, msg: bytes, src) -> None:
        try:
            ours, qu = asks_for_us(msg)
        except (ValueError, IndexError, struct.error):
            return
        if not ours:
            return
        legacy = src[1] != PORT
        reply = answer(struct.unpack("!H", msg[:2])[0] if legacy else 0, self.url, self.hub_id, legacy)
        self.transport.sendto(reply, src if legacy or qu or not self.group else (GROUP, PORT))
