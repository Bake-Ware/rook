"""TVs on the Deck's network, driven directly as the firmware does (dev.cpp,
lan.cpp): a Roku over ECP (HTTP to port 8060), an LG webOS TV over its
websocket API (port 3000, or TLS on 3001 for newer sets: a PROMPT pairing
once, the client key kept, buttons through the pointer-input socket). A
device is named as a profile links it: "roku@192.168.1.20", "webos@192.168.1.30".
Standard library only (the worker plugin uses it too).
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import ssl
import struct
import threading
import urllib.request

from .paths import data_dir

ROKU_KEYS = {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "ok": "Select", "back": "Back",
             "home": "Home", "menu": "Info", "settings": "Info", "info": "Info", "vol_up": "VolumeUp",
             "vol_down": "VolumeDown", "mute": "VolumeMute", "power": "Power", "play": "Play", "pause": "Play",
             "rewind": "Rev", "forward": "Fwd", "replay": "InstantReplay", "ch_up": "ChannelUp", "ch_down": "ChannelDown"}
LG_KEYS = {"up": "UP", "down": "DOWN", "left": "LEFT", "right": "RIGHT", "ok": "ENTER", "back": "BACK", "home": "HOME",
           "menu": "MENU", "settings": "MENU", "info": "INFO", "exit": "EXIT", "vol_up": "VOLUMEUP",
           "vol_down": "VOLUMEDOWN", "mute": "MUTE", "play": "PLAY", "pause": "PAUSE", "stop": "STOP",
           "rewind": "REWIND", "forward": "FASTFORWARD", "ch_up": "CHANNELUP", "ch_down": "CHANNELDOWN"}
LG_PERMS = ["APP_TO_APP", "CLOSE", "CONTROL_AUDIO", "CONTROL_DISPLAY", "CONTROL_INPUT_JOYSTICK",
            "CONTROL_INPUT_MEDIA_PLAYBACK", "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_TEXT", "CONTROL_INPUT_TV",
            "CONTROL_MOUSE_AND_KEYBOARD", "CONTROL_POWER", "CONTROL_TV_SCREEN", "LAUNCH", "LAUNCH_WEBAPP",
            "READ_APP_STATUS", "READ_INPUT_DEVICE_LIST", "READ_INSTALLED_APPS", "READ_POWER_STATE",
            "READ_RUNNING_APPS", "READ_SETTINGS", "WRITE_NOTIFICATION_TOAST", "WRITE_SETTINGS"]


class LanError(Exception):
    pass


def parse_link(link: str) -> tuple[str, str]:
    """"roku@1.2.3.4" -> ("roku", "1.2.3.4")."""
    m = re.match(r"^(roku|webos)@([A-Za-z0-9.\-:]+)$", link or "")
    if not m:
        raise LanError(f"{link!r} is not a device link (roku@address or webos@address)")
    return m.group(1), m.group(2)


# -- Roku --------------------------------------------------------------------------------

def roku_key(addr: str, key: str, timeout: float = 3.0) -> str:
    k = ROKU_KEYS.get(key, key)
    if k == key and not (key[:1].isupper()):
        raise LanError(f"a Roku has no key called {key}")
    req = urllib.request.Request(f"http://{addr}:8060/keypress/{k}", data=b"", method="POST")
    try:
        urllib.request.urlopen(req, timeout=timeout).read()
    except OSError as e:
        raise LanError(f"no answer from the Roku at {addr} ({e})") from None
    return k


# -- a websocket client, enough for webOS (RFC 6455: text frames, client masking) ---------

class _WS:
    def __init__(self, host: str, port: int, tls: bool, timeout: float, path: str = "/"):
        raw = socket.create_connection((host, port), timeout=timeout)
        if tls:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE     # webOS sets present a self-signed certificate
            raw = ctx.wrap_socket(raw, server_hostname=host)
        self.s = raw
        key = base64.b64encode(os.urandom(16)).decode()
        self.s.sendall((f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            c = self.s.recv(1)
            if not c:
                raise LanError("the TV closed the connection")
            head += c
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise LanError("the TV refused the websocket")

    def send(self, text: str) -> None:
        data = text.encode()
        mask = os.urandom(4)
        n = len(data)
        hdr = bytes([0x81]) + (bytes([0x80 | n]) if n < 126 else bytes([0x80 | 126]) + struct.pack(">H", n) if n < 65536
                               else bytes([0x80 | 127]) + struct.pack(">Q", n))
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _exact(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            c = self.s.recv(n - len(out))
            if not c:
                raise LanError("the TV closed the connection")
            out += c
        return out

    def recv(self) -> str:
        while True:
            b0, b1 = self._exact(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._exact(8))[0]
            data = self._exact(n)
            op = b0 & 0x0F
            if op == 0x8:
                raise LanError("the TV closed the connection")
            if op == 0x9:                       # ping: pong
                self.s.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if op in (0x1, 0x0):
                return data.decode(errors="replace")

    def close(self) -> None:
        try:
            self.s.close()
        except OSError:
            pass


class WebOS:
    """One LG TV's session: opened on first use, kept, opened again when it drops."""

    _lock = threading.Lock()
    _sessions: dict[str, "WebOS"] = {}

    def __init__(self, addr: str):
        self.addr = addr
        self.main: _WS | None = None
        self.ptr: _WS | None = None
        self.n = 0

    @classmethod
    def get(cls, addr: str) -> "WebOS":
        with cls._lock:
            return cls._sessions.setdefault(addr, cls(addr))

    @staticmethod
    def _keys() -> dict:
        try:
            return json.loads((data_dir() / "lan.json").read_text()).get("webos_keys", {})
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _keep_key(addr: str, key: str) -> None:
        p = data_dir() / "lan.json"
        try:
            d = json.loads(p.read_text())
        except (OSError, ValueError):
            d = {}
        d.setdefault("webos_keys", {})[addr] = key
        p.write_text(json.dumps(d, indent=1))
        os.chmod(p, 0o600)

    def paired(self) -> bool:
        return bool(self._keys().get(self.addr))

    def open(self, prompt: bool = False) -> None:
        if self.main is not None:
            return
        try:
            ws = _WS(self.addr, 3000, False, 2.0)
        except (OSError, LanError):
            ws = _WS(self.addr, 3001, True, 6.0)
        key = self._keys().get(self.addr)
        if not key and not prompt:
            ws.close()
            raise LanError("this LG TV is not paired yet: pair it from Settings > Find devices")
        payload = {"forcePairing": False, "pairingType": "PROMPT",
                   "manifest": {"appVersion": "1.1", "manifestVersion": 1, "permissions": LG_PERMS}}
        if key:
            payload["client-key"] = key
        ws.send(json.dumps({"type": "register", "id": "register_0", "payload": payload}))
        ws.s.settimeout(60.0 if not key else 8.0)
        while True:
            a = json.loads(ws.recv())
            if a.get("id") != "register_0":
                continue
            if a.get("type") == "registered":
                k = (a.get("payload") or {}).get("client-key")
                if k and k != key:
                    self._keep_key(self.addr, k)
                break
            if a.get("type") == "error":
                ws.close()
                raise LanError("the TV said no: " + str(a.get("error")))
        ws.s.settimeout(5.0)
        self.main = ws

    def request(self, uri: str, payload: dict | None = None) -> dict:
        self.open()
        self.n += 1
        rid = f"r{self.n}"
        self.main.send(json.dumps({"type": "request", "id": rid, "uri": "ssap://" + uri, "payload": payload or {}}))
        while True:
            a = json.loads(self.main.recv())
            if a.get("id") == rid:
                if a.get("type") == "error":
                    raise LanError(str(a.get("error")))
                return a.get("payload") or {}

    def button(self, name: str) -> None:
        if self.ptr is None:
            url = self.request("com.webos.service.networkinput/getPointerInputSocket").get("socketPath", "")
            m = re.match(r"^(wss?)://([^:/]+):(\d+)(/.*)$", url)
            if not m:
                raise LanError("the TV gave no pointer socket")
            self.ptr = _WS(m.group(2), int(m.group(3)), m.group(1) == "wss", 5.0, m.group(4))
        self.ptr.send(f"type:button\nname:{name}\n\n")

    def drop(self) -> None:
        for w in (self.ptr, self.main):
            if w is not None:
                w.close()
        self.ptr = self.main = None


def webos_key(addr: str, key: str) -> str:
    k = LG_KEYS.get(key, key)
    if k == key and not key[:1].isupper():
        raise LanError(f"an LG TV has no key called {key}")
    tv = WebOS.get(addr)
    with WebOS._lock:
        try:
            tv.button(k)
        except (OSError, LanError, ValueError):
            tv.drop()                          # once more, on a fresh session
            try:
                tv.button(k)
            except (OSError, ValueError) as e:
                tv.drop()
                raise LanError(f"no answer from the LG TV at {addr} ({e})") from None
    return k


def send_key(link: str, key: str) -> str:
    """The key to the linked device. Answers what it was sent as."""
    driver, addr = parse_link(link)
    return roku_key(addr, key) if driver == "roku" else webos_key(addr, key)


def pair(link: str) -> None:
    """An LG TV shows its prompt; waits a minute for someone to accept. A Roku needs none."""
    driver, addr = parse_link(link)
    if driver == "webos":
        tv = WebOS.get(addr)
        with WebOS._lock:
            tv.drop()
            tv.open(prompt=True)


# -- finding them (SSDP) -----------------------------------------------------------------

def scan(seconds: float = 3.0) -> list[dict]:
    """Rokus and LG TVs that answer an SSDP search: [{name, link, driver, address}]."""
    found: dict[str, dict] = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    s.settimeout(0.3)
    for st in ("roku:ecp", "urn:lge-com:service:webos-second-screen:1"):
        msg = (f"M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\nMX: 2\r\nST: {st}\r\n\r\n")
        try:
            s.sendto(msg.encode(), ("239.255.255.250", 1900))
        except OSError:
            pass
    import time
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            data, (ip, _) = s.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        text = data.decode(errors="replace").lower()
        if "roku:ecp" in text:
            found[ip] = {"driver": "roku", "address": ip, "link": f"roku@{ip}", "name": f"Roku {ip}"}
        elif "webos-second-screen" in text or "lge" in text:
            found.setdefault(ip, {"driver": "webos", "address": ip, "link": f"webos@{ip}", "name": f"LG TV {ip}"})
    s.close()
    for d in found.values():                    # a Roku tells its name
        if d["driver"] == "roku":
            try:
                x = urllib.request.urlopen(f"http://{d['address']}:8060/query/device-info", timeout=1.5).read().decode()
                m = re.search(r"<user-device-name>([^<]+)</user-device-name>", x) or re.search(r"<friendly-device-name>([^<]+)<", x)
                if m:
                    d["name"] = m.group(1)[:20]
            except OSError:
                pass
    return sorted(found.values(), key=lambda d: d["name"])
