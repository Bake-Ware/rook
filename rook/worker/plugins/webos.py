"""webos.* and tv.* — LG webOS TVs as device workers.

This worker becomes the *delegate* for the TVs listed in ``webos.tvs``: each
TV shows on the band as a worker of its own (``rook_workers`` lists
``office-tv``, with ``delegate`` naming this worker), with ``tv.*`` caps and a
live heartbeat ``hb.tv = {on, app, volume, muted}``. See
``docs/design/devices.md``.

It talks to each TV over the TV's own control API (SSAP, ``ws://<tv>:3000``),
keeping one connection open per TV and subscribing to the foreground app,
power state and volume, so the heartbeat is current without polling. A TV
that is off simply has ``on: false``; its row stays, because the delegate is
still there to wake it (``tv.power_on`` sends Wake-on-LAN to the MAC address
learned while it was on).

Pairing: the first connection needs a person to accept an "allow this
device?" prompt on the TV (``tv.pair``). The key the TV returns is kept in
this plugin's data directory (mode 600); ``tv.pair(key=...)`` installs a key
paired elsewhere instead.

Config (settings, or env):
    WEBOS_TVS   ``name=host`` pairs, comma-separated, e.g.
                ``office-tv=192.0.2.10, living-room=192.0.2.11``. A MAC address
                for Wake-on-LAN can follow the host: ``office-tv=192.0.2.10@aa:bb:cc:dd:ee:ff``
                (otherwise it is learned). Blank leaves the plugin off.

Capabilities (on each TV device):
    tv.status, tv.apps, tv.inputs                      read
    tv.toast, tv.launch, tv.input, tv.volume, tv.mute,
    tv.media, tv.button, tv.open_url, tv.screen,
    tv.power_off, tv.power_on                          write (physical)
    tv.pair                                            admin
And on this worker: ``webos.list`` (every TV and its state) and
``webos.discover`` (find LG TVs on the local network).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time
from typing import Any

from ...core.db import atomic_write_json
from ...core.devices import Device, valid_device_name
from ...core.pluginkit import ArgError, fail, require_str
from ..plugin import Plugin, capability, setting
from . import _webos_ssap as ssap

log = logging.getLogger("rook.worker.webos")

_MAC = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.I)
BUTTONS = ("HOME", "BACK", "UP", "DOWN", "LEFT", "RIGHT", "ENTER", "MENU", "EXIT",
           "INFO", "RED", "GREEN", "YELLOW", "BLUE", "PLAY", "PAUSE", "STOP",
           "REWIND", "FASTFORWARD", "VOLUMEUP", "VOLUMEDOWN", "MUTE", "CHANNELUP",
           "CHANNELDOWN", "DASH", "ASTERISK", "CC", "QMENU", "1", "2", "3", "4", "5",
           "6", "7", "8", "9", "0")
RETRY_MIN, RETRY_MAX = 5.0, 30.0
STABLE_SECS = 60.0   # a connection that lasted this long resets the backoff


def parse_tvs(spec: str) -> list[tuple[str, str, str | None]]:
    """``"office-tv=192.0.2.10@aa:bb:…, den=tv.example.com"`` ->
    ``[(name, host, mac|None), …]``. Malformed entries raise ValueError."""
    out = []
    for part in (spec or "").replace("\n", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, sep, rest = part.partition("=")
        name, rest = name.strip(), rest.strip()
        if not sep or not rest or not valid_device_name(name):
            raise ValueError(f"bad TV entry {part!r}: expected name=host")
        host, _, mac = rest.partition("@")
        host, mac = host.strip(), mac.strip() or None
        if not host:
            raise ValueError(f"bad TV entry {part!r}: no host")
        if mac and not _MAC.match(mac):
            raise ValueError(f"bad MAC address in {part!r}")
        out.append((name, host, mac.lower().replace("-", ":") if mac else None))
    names = [n.lower() for n, _, _ in out]
    if len(names) != len(set(names)):
        raise ValueError("TV names must be unique")
    return out


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + raw * 16


class WebOSTV(Device):
    """One LG webOS TV, driven over its control API."""

    NAMESPACE = "tv"
    TYPE = "tv"
    DRIVER = "webos"

    def __init__(self, plugin: "WebOSPlugin", name: str, host: str,
                 mac: str | None = None) -> None:
        super().__init__(key=name.lower(), name=name, description=f"LG webOS TV at {host}")
        self.plugin = plugin
        self.host = host
        self.mac = mac
        self.client: ssap.SSAPClient | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.state: dict[str, Any] = {"on": False, "reachable": False,
                                      "paired": bool(plugin.key_for(self))}
        self.apps: dict[str, str] = {}          # id -> title
        self.inputs: list[dict] = []
        self.model: str | None = None

    # -- device protocol ---------------------------------------------------
    def info(self) -> dict:
        out = super().info()
        if self.model:
            out["model"] = self.model
        return out

    def heartbeat(self) -> dict | None:
        s = self.state
        hb: dict[str, Any] = {"on": bool(s.get("on"))}
        if s.get("on"):
            for k in ("app", "volume", "muted"):
                if s.get(k) is not None:
                    hb[k] = s[k]
            if s.get("screen") == "off":
                hb["screen"] = "off"
        if not s.get("paired"):
            hb["paired"] = False
        return hb

    # -- connection --------------------------------------------------------
    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True   # a call still in flight must not reconnect
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def _run(self) -> None:
        delay = RETRY_MIN
        while True:
            try:
                client = await self._connect()
            except asyncio.CancelledError:
                raise
            except ssap.NotPaired as e:
                self._down(paired=False, error=str(e))
                client = None
            except Exception as e:
                self._down(error=f"{type(e).__name__}: {e}")
                client = None
            if client is not None:
                since = time.monotonic()
                await client.closed.wait()
                self._down(error="connection closed")
                if time.monotonic() - since >= STABLE_SECS:
                    delay = RETRY_MIN
                    continue
                # Dropped soon after connecting (standby, flaky Wi-Fi): back off
                # as for a failed connect, not a tight reconnect loop.
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), delay)
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, RETRY_MAX)

    def _down(self, *, paired: bool | None = None, error: str = "") -> None:
        self.state.update(on=False, reachable=False, screen=None, error=error or None)
        if paired is not None:
            self.state["paired"] = paired
        self.client = None

    async def _connect(self) -> ssap.SSAPClient:
        """The live client, connecting (and registering) if needed."""
        async with self._lock:
            if self._stopped:
                raise ssap.SSAPError("the webos plugin stopped driving this TV")
            if self.client is not None and self.client.connected:
                return self.client
            client = ssap.SSAPClient(self.host)
            await client.connect(self.plugin.key_for(self))
            return await self._adopt(client)

    async def _adopt(self, client: ssap.SSAPClient) -> ssap.SSAPClient:
        """Make a registered client the live one (lock held)."""
        if self.client is not None and self.client is not client:
            await self.client.close()
        self.client = client
        self.state.update(reachable=True, paired=True, error=None, seen=time.time())
        await self._prime(client)
        return client

    async def _prime(self, c: ssap.SSAPClient) -> None:
        """Subscribe first, so on/off, the app and the volume are live even if
        a catalogue request fails; then fill the catalogues, each on its own."""
        for uri, cb in ((ssap.POWER_STATE, self._on_power), (ssap.FOREGROUND_APP, self._on_app),
                        ("audio/getVolume", self._on_volume)):
            try:
                await c.subscribe(uri, cb)
            except Exception:
                log.debug("%s: subscribing to %s failed", self.name, uri, exc_info=True)
        try:
            points = await c.request(ssap.LAUNCH_POINTS)
            self.apps = {p["id"]: p.get("title") or p["id"]
                         for p in points.get("launchPoints") or [] if p.get("id")}
        except Exception:
            log.debug("%s: listing apps failed", self.name, exc_info=True)
        try:
            self.inputs = [
                {"id": d.get("id"), "label": d.get("label"), "app": d.get("appId"),
                 "connected": bool(d.get("connected"))}
                for d in (await c.request(ssap.INPUTS)).get("devices") or []]
        except Exception:
            self.inputs = []
        try:
            info = await c.request(ssap.CONNECTION_INFO)
            mac = ((info.get("wiredInfo") or {}).get("macAddress")
                   or (info.get("wifiInfo") or {}).get("macAddress"))
            if mac and _MAC.match(mac) and mac.lower() != (self.mac or ""):
                self.mac = mac.lower()
                self.plugin.save_mac(self, self.mac)
        except Exception:
            pass
        try:
            self.model = (await c.request(ssap.SYSTEM_INFO)).get("modelName") or self.model
        except Exception:
            pass
        if self.state.get("app_id"):   # name it now that the catalogues are in
            self.state["app"] = self.title(self.state["app_id"])

    def title(self, app_id: str) -> str:
        """An app's title; external inputs (``com.webos.app.hdmi1``) are not
        launch points, so they are named by their input label."""
        if app_id in self.apps:
            return self.apps[app_id]
        for d in self.inputs:
            if d.get("app") == app_id and d.get("label"):
                return d["label"]
        return app_id

    def _on_app(self, p: dict) -> None:
        app = p.get("appId") or ""
        self.state["app_id"] = app or None
        self.state["app"] = self.title(app) if app else None
        if app and self.state.get("power") is None:
            self.state["on"] = True

    def _on_power(self, p: dict) -> None:
        st = str(p.get("state") or "")
        self.state["power"] = st or None
        self.state["on"] = st in ("Active", "Screen Off", "Screen Saver")
        self.state["screen"] = "off" if st == "Screen Off" else ("on" if st == "Active" else None)

    def _on_volume(self, p: dict) -> None:
        vs = p.get("volumeStatus") or p
        if vs.get("volume") is not None:
            self.state["volume"] = vs.get("volume")
        muted = vs.get("muteStatus", vs.get("muted"))
        if muted is not None:
            self.state["muted"] = bool(muted)

    async def _live(self) -> ssap.SSAPClient:
        """A connected client for a cap, or a clear failure."""
        try:
            return await self._connect()
        except ssap.NotPaired:
            fail(f"{self.name} is not paired: run tv.pair while someone is at the TV")
        except Exception as e:
            fail(f"{self.name} is off or unreachable ({type(e).__name__}: {e})")

    async def _req(self, uri: str, payload: dict | None = None) -> dict:
        c = await self._live()
        try:
            return await c.request(uri, payload)
        except ssap.SSAPError as e:
            fail(f"{self.name}: {e}")

    # -- caps --------------------------------------------------------------
    @capability("status", risk="read", tags=("physical",))
    async def _status(self) -> dict:
        """Live state: ``{on, power, app, app_id, volume, muted, screen, reachable, paired}``."""
        if not self.state.get("reachable"):
            try:
                await self._connect()
            except Exception:
                pass
        s = {k: v for k, v in self.state.items() if v is not None}
        s.update(name=self.name, host=self.host)
        if self.model:
            s["model"] = self.model
        if self.mac:
            s["mac"] = self.mac
        return s

    @capability("apps", risk="read", tags=("physical",))
    async def _apps(self) -> dict:
        """Installed apps the TV can launch: ``{items: [{id, title}]}``."""
        await self._live()
        return {"items": [{"id": k, "title": v} for k, v in sorted(self.apps.items(),
                                                                key=lambda kv: kv[1].lower())]}

    @capability("inputs", risk="read", tags=("physical",))
    async def _inputs(self) -> dict:
        """External inputs (HDMI…): ``{items: [{id, label, connected}]}``."""
        await self._live()
        return {"items": list(self.inputs)}

    @capability("toast", risk="write", tags=("physical",))
    async def _toast(self, message: str) -> dict:
        """Show a short notification on the TV screen."""
        message = require_str(message, "message", max_len=500)
        await self._req(ssap.TOAST, {"message": message})
        return {"ok": True}

    def _resolve_app(self, app: str) -> str:
        if app in self.apps:
            return app
        low = app.lower()
        exact = [k for k, v in self.apps.items() if v.lower() == low]
        if exact:
            return exact[0]
        part = [k for k, v in self.apps.items() if low in v.lower() or low in k.lower()]
        if len(part) == 1:
            return part[0]
        if part:
            fail(f"{app!r} matches several apps: "
                 + ", ".join(f"{self.apps[k]} ({k})" for k in part[:8]))
        fail(f"no app matches {app!r}; tv.apps lists them")

    @capability("launch", risk="write", tags=("physical",))
    async def _launch(self, app: str, params: dict | None = None) -> dict:
        """Launch an app by id or title (``"YouTube"``, ``"netflix"``), with
        optional launch ``params``. A YouTube video:
        ``params={"contentTarget": "https://www.youtube.com/tv?v=<id>"}``."""
        await self._live()
        app_id = self._resolve_app(require_str(app, "app", max_len=200))
        payload: dict[str, Any] = {"id": app_id}
        if params is not None:
            if not isinstance(params, dict):
                raise ArgError("params must be an object")
            payload["params"] = params
        await self._req(ssap.LAUNCH, payload)
        return {"ok": True, "app_id": app_id, "app": self.title(app_id)}

    @capability("input", risk="write", tags=("physical",))
    async def _input(self, input: str) -> dict:
        """Switch to an external input by id or label (``"HDMI 1"``, ``"HDMI_2"``)."""
        await self._live()
        want = require_str(input, "input", max_len=50).lower().replace(" ", "").replace("_", "")
        for d in self.inputs:
            for k in ("id", "label"):
                if str(d.get(k) or "").lower().replace(" ", "").replace("_", "") == want:
                    await self._req(ssap.SWITCH_INPUT, {"inputId": d["id"]})
                    return {"ok": True, "input": d["id"], "label": d.get("label")}
        fail(f"no input matches {input!r}; tv.inputs lists them")

    @capability("volume", risk="write", tags=("physical",))
    async def _volume(self, level: int | None = None, step: int | None = None) -> dict:
        """Set the volume to ``level`` (0-100), or move it by ``step`` (e.g. +3, -2)."""
        if (level is None) == (step is None):
            raise ArgError("pass exactly one of level or step")
        if level is not None:
            if not isinstance(level, int) or isinstance(level, bool) or not 0 <= level <= 100:
                raise ArgError("level must be an integer 0-100")
            await self._req(ssap.SET_VOLUME, {"volume": level})
        else:
            if not isinstance(step, int) or isinstance(step, bool) or not -20 <= step <= 20 or not step:
                raise ArgError("step must be a non-zero integer from -20 to 20")
            uri = ssap.VOLUME_UP if step > 0 else ssap.VOLUME_DOWN
            for _ in range(abs(step)):
                await self._req(uri)
        await asyncio.sleep(0.3)   # let the subscription report the new level
        return {"ok": True, "volume": self.state.get("volume"), "muted": self.state.get("muted")}

    @capability("mute", risk="write", tags=("physical",))
    async def _mute(self, on: bool = True) -> dict:
        """Mute (``on=true``) or unmute (``on=false``)."""
        await self._req(ssap.SET_MUTE, {"mute": bool(on)})
        return {"ok": True, "muted": bool(on)}

    @capability("media", risk="write", tags=("physical",))
    async def _media(self, action: str) -> dict:
        """Playback control for the current app: play, pause, stop, rewind, fast_forward."""
        uri = ssap.MEDIA.get(str(action).lower())
        if uri is None:
            raise ArgError(f"action must be one of {', '.join(ssap.MEDIA)}")
        await self._req(uri)
        return {"ok": True, "action": action}

    @capability("button", risk="write", tags=("physical",))
    async def _button(self, name: str) -> dict:
        """Press a remote button: HOME, BACK, UP, DOWN, LEFT, RIGHT, ENTER, MENU, EXIT, digits…"""
        key = require_str(name, "name", max_len=20).upper()
        if key not in BUTTONS:
            raise ArgError(f"unknown button {name!r}; one of {', '.join(BUTTONS)}")
        c = await self._live()
        try:
            await c.button(key)
        except (ssap.SSAPError, OSError) as e:
            fail(f"{self.name}: {e}")
        return {"ok": True, "button": key}

    @capability("open_url", risk="write", tags=("physical",))
    async def _open_url(self, url: str) -> dict:
        """Open an http(s) URL in the TV's web browser."""
        url = require_str(url, "url", max_len=2000)
        if not re.match(r"^https?://", url, re.I):
            raise ArgError("url must start with http:// or https://")
        await self._req(ssap.OPEN_URL, {"target": url})
        return {"ok": True}

    @capability("screen", risk="write", tags=("physical",))
    async def _screen(self, on: bool) -> dict:
        """Turn just the screen off (sound keeps playing) or back on."""
        await self._req(ssap.SCREEN_ON if on else ssap.SCREEN_OFF, {"standbyMode": "active"})
        return {"ok": True, "screen": "on" if on else "off"}

    @capability("power_off", risk="write", tags=("physical",))
    async def _power_off(self) -> dict:
        """Turn the TV off."""
        await self._req(ssap.POWER_OFF)
        return {"ok": True}

    @capability("power_on", risk="write", tags=("physical",))
    async def _power_on(self) -> dict:
        """Turn the TV on with Wake-on-LAN. Needs the TV's MAC (learned while
        it was on, or given in webos.tvs) and the TV's "Turn on via Wi-Fi /
        network" setting; the delegate must be on the TV's network."""
        if self.state.get("on") and self.state.get("reachable"):
            return {"ok": True, "already_on": True}
        if not self.mac:
            fail(f"{self.name}: MAC address unknown; turn it on once by hand while the "
                 "delegate is running, or add @<mac> to its webos.tvs entry")
        pkt = magic_packet(self.mac)
        sent = await asyncio.to_thread(_send_wol, pkt, self.host)
        self._wake.set()   # reconnect soon rather than at the next retry
        return {"ok": bool(sent), "mac": self.mac, "sent_to": sent}

    @capability("pair", risk="admin", tags=("physical",), timeout=120)
    async def _pair(self, key: str | None = None, timeout: int = 60) -> dict:
        """Pair with the TV. Without ``key`` the TV shows an "allow this
        device?" prompt that a person must accept with the remote within
        ``timeout`` seconds; with ``key``, install a key paired elsewhere."""
        if key is not None:
            key = require_str(key, "key", max_len=200)
        # On a connection of its own: the TV's live one keeps serving other
        # caps while someone walks to the TV, and stays if pairing fails.
        client = ssap.SSAPClient(self.host)
        try:
            got = await client.connect(key, prompt=key is None,
                                       prompt_timeout=max(10, min(int(timeout), 110)))
        except ssap.NotPaired as e:
            fail(f"{self.name}: pairing refused ({e})")
        except asyncio.TimeoutError:
            fail(f"{self.name}: nobody accepted the prompt in time")
        except Exception as e:
            fail(f"{self.name} is off or unreachable ({type(e).__name__}: {e})")
        self.plugin.save_key(self, got or key)
        async with self._lock:
            if self._stopped:
                await client.close()
                fail("the webos plugin stopped driving this TV")
            await self._adopt(client)
        self._wake.set()
        return {"ok": True, "paired": True}


def _send_wol(pkt: bytes, host: str) -> list[str]:
    sent = []
    for addr in ("255.255.255.255", host):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.sendto(pkt, (addr, 9))
            sent.append(addr)
        except OSError as e:
            log.debug("wake-on-lan to %s failed: %s", addr, e)
    return sent


class WebOSPlugin(Plugin):
    NAMESPACE = "webos"
    SETTINGS = (
        setting("tvs", str, default="", scope="worker", env="WEBOS_TVS", apply="reload",
                label="TVs",
                help="name=host pairs, comma-separated (office-tv=192.0.2.10). "
                     "Each becomes a device worker this worker answers for. "
                     "Blank leaves webos.* off."),
    )

    def __init__(self) -> None:
        super().__init__()
        self._tvs: dict[str, WebOSTV] = {}

    def _spec(self) -> list[tuple[str, str, str | None]]:
        try:
            return parse_tvs(self.settings.get("tvs") or "")
        except ValueError as e:
            log.warning("webos.tvs: %s", e)
            return []

    def available(self) -> bool:
        return bool(self._spec()) and ssap.supported()

    def unavailable_reason(self) -> str:
        if not ssap.supported():
            return "Needs the Python websockets package (pip install websockets)."
        return "No TVs configured: set webos.tvs (name=host pairs)."

    # -- persisted state ---------------------------------------------------
    def _load(self, fname: str) -> dict:
        try:
            with open(self.data_dir / fname, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, fname: str, data: dict) -> None:
        atomic_write_json(self.data_dir / fname, data, indent=1)

    def key_for(self, tv: WebOSTV) -> str | None:
        return self._load("keys.json").get(tv.key) or None

    def save_key(self, tv: WebOSTV, key: str) -> None:
        keys = self._load("keys.json")
        keys[tv.key] = key
        self._save("keys.json", keys)

    def save_mac(self, tv: WebOSTV, mac: str) -> None:
        macs = self._load("macs.json")
        macs[tv.key] = mac
        self._save("macs.json", macs)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        macs = self._load("macs.json")
        for name, host, mac in self._spec():
            tv = WebOSTV(self, name, host, mac or macs.get(name.lower()))
            self._tvs[tv.key] = tv
            tv.start()

    async def stop(self) -> None:
        tvs, self._tvs = list(self._tvs.values()), {}
        await asyncio.gather(*(tv.stop() for tv in tvs), return_exceptions=True)

    def devices(self) -> list[Device]:
        return list(self._tvs.values())

    # -- caps on the delegate -----------------------------------------------
    @capability("list", risk="read")
    async def _list(self) -> dict:
        """Every configured TV: ``{items: [{name, host, on, app, reachable, paired}]}``."""
        items = []
        for tv in self._tvs.values():
            s = tv.state
            items.append({"name": tv.name, "host": tv.host, "on": bool(s.get("on")),
                          "app": s.get("app"), "reachable": bool(s.get("reachable")),
                          "paired": bool(s.get("paired")), "error": s.get("error")})
        return {"items": items}

    @capability("discover", risk="read", timeout=20)
    async def _discover(self, timeout: float = 3.0) -> dict:
        """Find LG webOS TVs on this worker's network (SSDP), for adding to webos.tvs."""
        t = max(1.0, min(float(timeout), 10.0))
        found = await asyncio.to_thread(_ssdp_search, t)
        known = {tv.host: tv.name for tv in self._tvs.values()}
        return {"items": [{**f, "configured_as": known.get(f["host"])} for f in found]}


def _ssdp_search(timeout: float) -> list[dict]:
    msg = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: \"ssdp:discover\"\r\n"
           "MX: 2\r\nST: urn:lge-com:service:webos-second-screen:1\r\n\r\n").encode()
    seen: dict[str, dict] = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP) as s:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        s.settimeout(0.5)
        s.sendto(msg, ("239.255.255.250", 1900))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            headers = {}
            for line in data.decode(errors="replace").split("\r\n")[1:]:
                k, _, v = line.partition(":")
                if k:
                    headers[k.strip().lower()] = v.strip()
            if "webos" not in (headers.get("st", "") + headers.get("server", "")).lower():
                continue
            seen.setdefault(ip, {"host": ip, "server": headers.get("server"),
                                 "location": headers.get("location")})
    return list(seen.values())


PLUGIN = WebOSPlugin
