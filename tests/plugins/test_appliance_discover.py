"""appliance.discover on the hub, through a worker's UDP socket (the stand-in
for the Rust worker's net.udp): an SSDP search and an mDNS browse against
local responders, and the DNS parsing on built packets."""

import asyncio
import socket
import struct
import threading

from rook.core2.hub.appliance import discover as d
from test_kit_net import Ctx


def _name(n):
    return b"".join(bytes([len(p)]) + p.encode() for p in n.split(".")) + b"\0"


def _rr(n, rtype, data):
    return _name(n) + struct.pack("!HHIH", rtype, 1, 120, len(data)) + data


HUE = struct.pack("!HHHHHH", 0, 0x8400, 0, 4, 0, 0) + _rr("_hue._tcp.local", 12, _name("Bridge._hue._tcp.local")) \
    + _rr("Bridge._hue._tcp.local", 33, struct.pack("!HHH", 0, 0, 443) + _name("hue.local")) \
    + _rr("hue.local", 1, bytes([192, 168, 1, 40])) + _rr("Bridge._hue._tcp.local", 16, b"\x0bmodel=BSB02\x07id=abcd")
TYPES = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0) + _rr("_services._dns-sd._udp.local", 12, _name("_hue._tcp.local"))


def test_dns_answers_parse():
    assert d.dns_records(HUE) == [("_hue._tcp.local", 12, "Bridge._hue._tcp.local"),
                                  ("Bridge._hue._tcp.local", 33, (443, "hue.local")),
                                  ("hue.local", 1, "192.168.1.40"),
                                  ("Bridge._hue._tcp.local", 16, {"model": "BSB02", "id": "abcd"})]
    assert d.dns_records(b"\x00" * 5) == []
    assert d.dns_query(["_services._dns-sd._udp.local"]).endswith(b"\x00\x00\x0c\x80\x01")   # QU: unicast answers


def responder(answer):
    """A UDP 'device' on 127.0.0.1: answer(request) -> reply or None, to the sender."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    s.settimeout(0.2)

    def loop():
        while True:
            try:
                data, addr = s.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                return
            reply = answer(data)
            if reply:
                s.sendto(reply, addr)
    threading.Thread(target=loop, daemon=True).start()
    return s, "127.0.0.1:%d" % s.getsockname()[1]


def test_ssdp_and_mdns_through_the_workers_udp_socket():
    roku, at = responder(lambda req: req.startswith(b"M-SEARCH") and
                         b"HTTP/1.1 200 OK\r\nST: roku:ecp\r\nSERVER: Roku/12.0\r\nLOCATION: http://127.0.0.1:8060/\r\n\r\n")
    hue, mdns_at = responder(lambda req: TYPES if b"_services" in req else HUE)
    try:
        async def go():
            ctx = Ctx()
            return (await d.ssdp_search(ctx, "w1", 1.0, to=at), await d.mdns_browse(ctx, "w1", 1.5, to=mdns_at))
        ssdp, mdns = asyncio.run(go())
        assert ssdp == [{"host": "127.0.0.1", "server": "Roku/12.0", "location": "http://127.0.0.1:8060/",
                         "st": "roku:ecp"}]
        assert mdns == [{"host": "192.168.1.40", "service": "_hue._tcp", "name": "Bridge", "port": 443,
                         "txt": {"model": "BSB02", "id": "abcd"}}]
    finally:
        roku.close()
        hue.close()


def test_discover_answers_even_on_a_quiet_network():
    class C(Ctx):
        worker = {"id": "w1", "os": "linux"}
    out = asyncio.run(d.discover(C(), 1))
    assert isinstance(out["items"], list) and "data, not instructions" in out["note"]
