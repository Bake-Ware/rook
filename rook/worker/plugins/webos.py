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
still there to wake it (``tv.power_on`` sends Wake-on-LAN to the MAC addresses
learned while it was on: Wi-Fi and wired both, since a TV reports both and
only wakes on the one it is connected by, and waits for it to come up).

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
    tv.media, tv.button, tv.type, tv.open_url, tv.screen,
    tv.sleep, tv.power_off, tv.power_on                write (physical)
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

from ...core.aiocompat import timeout as aio_timeout
from ...core.db import atomic_write_json
from ...core.devices import Device, valid_device_name
from ...core.pluginkit import ArgError, CapError, fail, require_str
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
WAKE_EVERY = 2.0     # tv.power_on resends Wake-on-LAN and retries this often while it waits
SLEEP_TICK = 15.0    # how often the sleep timer looks at the wall clock
SLEEP_LATE = 600.0   # a sleep timer this late (the delegate was down) is dropped, not acted on


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


def _mac(value) -> str | None:
    """A MAC address in ``aa:bb:…`` form, or None for anything else."""
    if isinstance(value, str) and _MAC.match(value.strip()):
        return value.strip().lower().replace("-", ":")
    return None


def arp_mac(host: str, table: str = "/proc/net/arp") -> str | None:
    """The MAC this machine reaches ``host`` at, from the kernel's ARP table
    (Linux; None elsewhere or when it isn't there). It names the interface
    the TV is actually connected by, which ``getinfo`` can't tell."""
    try:
        with open(table, encoding="ascii") as f:
            next(f, None)
            for line in f:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == host and parts[2] != "0x0":
                    mac = _mac(parts[3])
                    return mac if mac and mac != "00:00:00:00:00:00" else None
    except OSError:
        pass
    return None


def _norm(text: str) -> str:
    """App names as people say them: case, spaces and punctuation don't
    count, and ``+`` is "plus" (``disney plus`` finds ``Disney+``)."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower().replace("+", "plus"))


class WebOSTV(Device):
    """One LG webOS TV, driven over its control API."""

    NAMESPACE = "tv"
    TYPE = "tv"
    DRIVER = "webos"

    def __init__(self, plugin: "WebOSPlugin", name: str, host: str,
                 macs: list[str] | str | None = None) -> None:
        super().__init__(key=name.lower(), name=name, description=f"LG webOS TV at {host}")
        self.plugin = plugin
        self.host = host
        # Every MAC Wake-on-LAN goes to, the one the TV is reached by first.
        if isinstance(macs, str):
            macs = [macs]
        self.macs: list[str] = list(dict.fromkeys(m for m in map(_mac, macs or ()) if m))
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
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            # Bounded: a loop that somehow missed the cancel still ends on
            # _stopped, and stop() must never hang the plugin host.
            await asyncio.wait({task}, timeout=5)
        if self.client is not None:
            await self.client.close()
            self.client = None

    async def _run(self) -> None:
        delay = RETRY_MIN
        while not self._stopped:
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
                if self.client is not None and self.client is not client \
                        and self.client.connected:
                    # A cap already reconnected (tv.power_on, a retried
                    # request): follow that connection instead.
                    continue
                self._down(error="connection closed")
                if time.monotonic() - since >= STABLE_SECS:
                    delay = RETRY_MIN
                    continue
                # Dropped soon after connecting (standby, flaky Wi-Fi): back off
                # as for a failed connect, not a tight reconnect loop.
            self._wake.clear()
            try:
                # Not asyncio.wait_for: on 3.11 it can swallow stop()'s cancel
                # when the wake lands at the same moment.
                async with aio_timeout(delay):
                    await self._wake.wait()
            except (TimeoutError, asyncio.TimeoutError):
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
                        ("audio/getVolume", self._on_volume),
                        (ssap.SOUND_OUTPUT, self._on_sound_output)):
            try:
                await c.subscribe(uri, cb)
            except Exception:
                log.debug("%s: subscribing to %s failed", self.name, uri, exc_info=True)
        await self._list_apps(c)
        try:
            self.inputs = [
                {"id": d.get("id"), "label": d.get("label"), "app": d.get("appId"),
                 "connected": bool(d.get("connected"))}
                for d in (await c.request(ssap.INPUTS)).get("devices") or []]
        except Exception:
            self.inputs = []
        try:
            info = await c.request(ssap.CONNECTION_INFO)
        except Exception:
            info = {}
        self._learn_macs(info)
        try:
            self.model = (await c.request(ssap.SYSTEM_INFO)).get("modelName") or self.model
        except Exception:
            pass
        try:
            # The TV's own "Turn on via Wi-Fi" switch: without it tv.power_on can't work.
            got = await c.request(ssap.GET_SETTINGS, {"category": "network",
                                                      "keys": ["wolwowlOnOff"]})
            wol = (got.get("settings") or {}).get("wolwowlOnOff")
            if wol is not None:
                self.state["wake_on_lan"] = str(wol).lower() in ("true", "on", "1")
        except Exception:
            pass
        if self.state.get("app_id"):   # name it now that the catalogues are in
            self.state["app"] = self.title(self.state["app_id"])

    def _learn_macs(self, info: dict) -> None:
        """Keep every MAC the TV has for Wake-on-LAN. A TV reports its wired
        and its Wi-Fi MAC whichever it uses, and only wakes on the one it is
        connected by; the delegate's ARP table says which that is, so it goes
        first. (The first version kept only the wired one, so ``tv.power_on``
        never reached a TV on Wi-Fi.)"""
        seen = [arp_mac(self.host)]
        for k in ("wifiInfo", "wiredInfo"):
            seen.append(_mac((info.get(k) or {}).get("macAddress")))
        macs = list(dict.fromkeys([m for m in seen if m] + self.macs))
        if macs != self.macs:
            self.macs = macs
            self.plugin.save_macs(self, macs)

    async def _list_apps(self, c: ssap.SSAPClient) -> None:
        try:
            points = await c.request(ssap.LAUNCH_POINTS)
            self.apps = {p["id"]: p.get("title") or p["id"]
                         for p in points.get("launchPoints") or [] if p.get("id")}
        except Exception:
            log.debug("%s: listing apps failed", self.name, exc_info=True)

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

    def _on_sound_output(self, p: dict) -> None:
        if p.get("soundOutput"):
            self.state["sound_output"] = str(p["soundOutput"])

    async def _live(self) -> ssap.SSAPClient:
        """A connected client for a cap, or a clear failure."""
        try:
            return await self._connect()
        except ssap.NotPaired:
            fail(f"{self.name} is not paired: run tv.pair while someone is at the TV")
        except Exception as e:
            fail(f"{self.name} is off or unreachable ({type(e).__name__}: {e}); "
                 "tv.power_on switches it on")

    async def _req(self, uri: str, payload: dict | None = None) -> dict:
        for attempt in (1, 2):
            c = await self._live()
            try:
                return await c.request(uri, payload)
            except ssap.SSAPError as e:
                if attempt == 1 and not c.connected and not self._stopped:
                    # The connection died under us (standby, a Wi-Fi blip) before
                    # the TV answered: once more on a fresh one.
                    continue
                fail(f"{self.name}: {e}")
            except (TimeoutError, asyncio.TimeoutError):
                # A socket that stops answering without closing: drop it, so the
                # next call reconnects instead of waiting on it again.
                await c.close()
                fail(f"{self.name} did not answer within {c.request_timeout:g} s; "
                     "it may be going to standby. Try again, or check tv.status")
            except Exception as e:   # the socket failed under the send
                if attempt == 1 and not self._stopped:
                    await c.close()
                    continue
                fail(f"{self.name}: lost the connection ({type(e).__name__}: {e})")
        fail(f"{self.name}: lost the connection")

    # -- caps --------------------------------------------------------------
    @capability("status", risk="read", tags=("physical",), scope="multi-machine")
    async def _status(self) -> dict:
        """Live state: ``{on, power, app, app_id, volume, muted, screen,
        sound_output, reachable, paired, wake_on_lan, macs, sleep_at}``.
        A ``sound_output`` not starting ``tv_`` means the sound goes only to a
        soundbar or receiver, whose own volume the TV may not control."""
        if not self.state.get("reachable"):
            try:
                await self._connect()
            except Exception:
                pass
        s = {k: v for k, v in self.state.items() if v is not None}
        s.update(name=self.name, host=self.host)
        if self.model:
            s["model"] = self.model
        if self.macs:
            s["macs"] = list(self.macs)
        at = self.plugin.sleep_at(self)
        if at:
            s["sleep_at"] = at
            s["sleep_in_minutes"] = max(0, round((at - time.time()) / 60))
        return s

    @capability("apps", risk="read", tags=("physical",), scope="multi-machine")
    async def _apps(self) -> dict:
        """Installed apps the TV can launch: ``{items: [{id, title}]}``."""
        await self._live()
        return {"items": [{"id": k, "title": v} for k, v in sorted(self.apps.items(),
                                                                key=lambda kv: kv[1].lower())]}

    @capability("inputs", risk="read", tags=("physical",), scope="multi-machine")
    async def _inputs(self) -> dict:
        """External inputs (HDMI…): ``{items: [{id, label, connected}]}``."""
        await self._live()
        return {"items": list(self.inputs)}

    @capability("toast", risk="write", tags=("physical",), scope="multi-machine")
    async def _toast(self, message: str) -> dict:
        """Show a short notification on the TV screen."""
        message = require_str(message, "message", max_len=500)
        await self._req(ssap.TOAST, {"message": message})
        return {"ok": True}

    def _match_app(self, app: str) -> list[str]:
        """App ids for ``app``: its id, else the best tier of title matches
        (same name, then starts with it, then contains it), ignoring case,
        spaces and punctuation."""
        if app in self.apps:
            return [app]
        want = _norm(app)
        if not want:
            return []
        names = {k: (_norm(v), _norm(k)) for k, v in self.apps.items()}
        for test in (lambda t, i: want in (t, i),
                     lambda t, i: t.startswith(want),
                     lambda t, i: want in t or want in i):
            hits = [k for k, (t, i) in names.items() if test(t, i)]
            if hits:
                return hits
        return []

    async def _resolve_app(self, app: str, c: ssap.SSAPClient) -> str:
        hits = self._match_app(app)
        if not hits:
            await self._list_apps(c)   # installed since we connected?
            hits = self._match_app(app)
        if len(hits) == 1:
            return hits[0]
        if hits:
            fail(f"{app!r} matches several apps: "
                 + ", ".join(f"{self.apps[k]} ({k})" for k in hits[:8]))
        if any(_norm(app) in (_norm(d.get("label") or ""), _norm(d.get("id") or ""))
               for d in self.inputs):
            fail(f"{app!r} is an input, not an app: use tv.input")
        fail(f"no app matches {app!r}; tv.apps lists them")

    @capability("launch", risk="write", tags=("physical",), scope="multi-machine")
    async def _launch(self, app: str, params: dict | None = None) -> dict:
        """Launch an app by id or title (``"YouTube"``, ``"netflix"``,
        ``"disney plus"``), with optional launch ``params``. A YouTube video:
        ``params={"contentTarget": "https://www.youtube.com/tv?v=<id>"}``."""
        c = await self._live()
        app_id = await self._resolve_app(require_str(app, "app", max_len=200), c)
        payload: dict[str, Any] = {"id": app_id}
        if params is not None:
            if not isinstance(params, dict):
                raise ArgError("params must be an object")
            payload["params"] = params
        await self._req(ssap.LAUNCH, payload)
        return {"ok": True, "app_id": app_id, "app": self.title(app_id)}

    @capability("input", risk="write", tags=("physical",), scope="multi-machine")
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

    @capability("volume", risk="write", tags=("physical",), scope="multi-machine")
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
        out = {"ok": True, "volume": self.state.get("volume"), "muted": self.state.get("muted")}
        sound = self.state.get("sound_output")
        if sound and not sound.startswith("tv_"):   # tv_external_speaker: both play
            out["note"] = (f"sound goes to {sound}: the TV's volume may not change what "
                           "you hear; that device has its own")
        return out

    @capability("mute", risk="write", tags=("physical",), scope="multi-machine")
    async def _mute(self, on: bool = True) -> dict:
        """Mute (``on=true``) or unmute (``on=false``)."""
        await self._req(ssap.SET_MUTE, {"mute": bool(on)})
        return {"ok": True, "muted": bool(on)}

    @capability("media", risk="write", tags=("physical",), scope="multi-machine")
    async def _media(self, action: str) -> dict:
        """Playback control for the current app: play, pause, stop, rewind, fast_forward."""
        uri = ssap.MEDIA.get(str(action).lower())
        if uri is None:
            raise ArgError(f"action must be one of {', '.join(ssap.MEDIA)}")
        await self._req(uri)
        return {"ok": True, "action": action}

    @capability("button", risk="write", tags=("physical",), scope="multi-machine")
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

    @capability("type", risk="write", tags=("physical",), scope="multi-machine")
    async def _type(self, text: str = "", enter: bool = False, replace: bool = False) -> dict:
        """Type ``text`` into the text field open on the TV (a search box,
        a login form), as the on-screen keyboard would. ``replace`` clears
        the field first; ``enter`` presses Enter after it (search, submit)."""
        text = require_str(text, "text", max_len=500, strip=False, empty=True)
        if not text and not enter and not replace:
            raise ArgError("pass text, enter or replace")
        await self._live()   # off or unpaired: say so plainly, not as a text-field problem
        try:
            if text or replace:
                await self._req(ssap.INSERT_TEXT, {"text": text, "replace": bool(replace)})
            if enter:
                await self._req(ssap.ENTER_KEY)
        except CapError as e:
            fail(f"{e}. A text field must be open on the TV: open the app's search "
                 "first (tv.button, or the app's own search screen)")
        return {"ok": True, "typed": len(text), "enter": bool(enter)}

    @capability("open_url", risk="write", tags=("physical",), scope="multi-machine")
    async def _open_url(self, url: str) -> dict:
        """Open an http(s) URL in the TV's web browser."""
        url = require_str(url, "url", max_len=2000)
        if not re.match(r"^https?://", url, re.I):
            raise ArgError("url must start with http:// or https://")
        await self._req(ssap.OPEN_URL, {"target": url})
        return {"ok": True}

    @capability("screen", risk="write", tags=("physical",), scope="multi-machine")
    async def _screen(self, on: bool) -> dict:
        """Turn just the screen off (sound keeps playing) or back on."""
        await self._req(ssap.SCREEN_ON if on else ssap.SCREEN_OFF, {"standbyMode": "active"})
        return {"ok": True, "screen": "on" if on else "off"}

    @capability("power_off", risk="write", tags=("physical",), scope="multi-machine")
    async def _power_off(self) -> dict:
        """Turn the TV off. A TV that is already off is not an error."""
        try:
            c = await self._connect()
        except ssap.NotPaired:
            fail(f"{self.name} is not paired: run tv.pair while someone is at the TV")
        except Exception:
            return {"ok": True, "already_off": True}
        if self.state.get("power") is not None and not self.state.get("on"):
            # Answering from network standby: already off. Never send it then;
            # what a TV does with a second "off" in standby is its own business.
            return {"ok": True, "already_off": True}
        try:
            await c.request(ssap.POWER_OFF)
        except ssap.SSAPError as e:
            # The TV often drops the connection as it goes off, before it answers.
            if c.connected:
                fail(f"{self.name}: {e}")
        except (TimeoutError, asyncio.TimeoutError):
            await c.close()
        return {"ok": True}

    @capability("power_on", risk="write", tags=("physical",), timeout=90,
                scope="multi-machine")
    async def _power_on(self, wait: int = 20) -> dict:
        """Turn the TV on with Wake-on-LAN and wait up to ``wait`` seconds
        (0-60) for it to answer, resending meanwhile; ``wait=0`` only sends.
        Needs the TV's MAC (learned while it was on, or given in webos.tvs),
        its "Turn on via Wi-Fi" setting (tv.status ``wake_on_lan``), and the
        delegate on the TV's network."""
        if isinstance(wait, bool) or not isinstance(wait, int) or not 0 <= wait <= 60:
            raise ArgError("wait must be an integer from 0 to 60")
        if self.state.get("on") and self.state.get("reachable"):
            return {"ok": True, "on": True, "already_on": True}
        live = arp_mac(self.host)   # the kernel remembers it a while after the TV went off
        if live and live not in self.macs:
            self.macs.insert(0, live)
            self.plugin.save_macs(self, self.macs)
        if not self.macs:
            fail(f"{self.name}: MAC address unknown; turn it on once by hand while the "
                 "delegate is running, or add @<mac> to its webos.tvs entry")
        sent = await asyncio.to_thread(_send_wol, self.macs, self.host)
        self._wake.set()   # reconnect soon rather than at the next retry
        out: dict[str, Any] = {"macs": list(self.macs), "sent_to": sent}
        if not wait:
            return {"ok": bool(sent), **out}
        start = time.monotonic()
        end = start + wait
        while True:
            left = end - time.monotonic()
            if left <= 0:
                break
            try:
                async with aio_timeout(left):
                    await self._connect()
                    # The power state arrives right after the subscription.
                    while self.state.get("power") is None and self.client is not None \
                            and self.client.connected:
                        await asyncio.sleep(0.1)
            except ssap.NotPaired:
                fail(f"{self.name} came on but is not paired: run tv.pair")
            except Exception:
                pass
            if self.state.get("on") and self.state.get("reachable"):
                self._wake.set()
                return {"ok": True, "on": True,
                        "waited": round(time.monotonic() - start, 1), **out}
            await asyncio.sleep(min(WAKE_EVERY, max(0.0, end - time.monotonic())))
            if time.monotonic() < end:
                await asyncio.to_thread(_send_wol, self.macs, self.host)
        why = ("its Turn on via Wi-Fi setting is off: switch it on at the TV under "
               "Settings > General > Devices > TV Management"
               if self.state.get("wake_on_lan") is False else
               "check the TV's Turn on via Wi-Fi (Mobile TV On) setting; some TVs only "
               "wake from a short standby")
        fail(f"{self.name} did not come on within {wait} s (Wake-on-LAN sent to "
             f"{', '.join(self.macs)}): {why}")

    @capability("sleep", risk="write", tags=("physical",), scope="multi-machine")
    async def _sleep(self, minutes: int) -> dict:
        """Switch the TV off in ``minutes`` (1-720), like the remote's sleep
        timer; ``minutes=0`` cancels it. The delegate keeps the time by the
        wall clock, across its own restarts; a timer it missed by more than
        10 minutes (it was down) is dropped rather than acted on late."""
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 0 <= minutes <= 720:
            raise ArgError("minutes must be an integer from 0 (cancel) to 720")
        if not minutes:
            had = self.plugin.set_sleep(self, None)
            return {"ok": True, "cancelled": bool(had)}
        at = round(time.time() + minutes * 60)
        self.plugin.set_sleep(self, at)
        return {"ok": True, "sleep_at": at, "in_minutes": minutes}

    @capability("pair", risk="admin", tags=("physical",), timeout=120, scope="multi-machine")
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
        except (TimeoutError, asyncio.TimeoutError):
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


def _send_wol(macs: list[str], host: str) -> list[str]:
    """One magic packet per MAC, broadcast and to the TV's address; the
    addresses at least one packet left for."""
    sent = []
    for addr in ("255.255.255.255", host):
        for mac in macs:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                    s.sendto(magic_packet(mac), (addr, 9))
                if addr not in sent:
                    sent.append(addr)
            except OSError as e:
                log.debug("wake-on-lan for %s to %s failed: %s", mac, addr, e)
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
        self._sleep_task: asyncio.Task | None = None
        self._sleep_wake = asyncio.Event()

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

    def save_macs(self, tv: WebOSTV, macs: list[str]) -> None:
        stored = self._load("macs.json")
        stored[tv.key] = list(macs)
        self._save("macs.json", stored)

    # -- sleep timers ------------------------------------------------------
    def sleep_at(self, tv: WebOSTV) -> int | None:
        at = self._load("sleep.json").get(tv.key)
        return at if isinstance(at, (int, float)) and not isinstance(at, bool) else None

    def set_sleep(self, tv: WebOSTV, at: int | None) -> bool:
        """Set (or with ``None`` clear) ``tv``'s sleep time; whether one was set."""
        timers = self._load("sleep.json")
        had = timers.pop(tv.key, None) is not None
        if at is not None:
            timers[tv.key] = at
        self._save("sleep.json", timers)
        self._sleep_wake.set()
        return had

    async def _sleeper(self) -> None:
        """Switch TVs off when their sleep time comes. Asleep on a short
        monotonic tick but judged by the wall clock, so a delegate that was
        suspended or restarted neither fires early nor late by much."""
        while True:
            self._sleep_wake.clear()
            try:
                async with aio_timeout(SLEEP_TICK):
                    await self._sleep_wake.wait()
            except (TimeoutError, asyncio.TimeoutError):
                pass
            now = time.time()
            for key, at in list(self._load("sleep.json").items()):
                tv = self._tvs.get(key)
                if tv is None or not isinstance(at, (int, float)) or at > now:
                    continue
                self.set_sleep(tv, None)
                if now - at > SLEEP_LATE:
                    log.info("%s: sleep timer missed by %d s; not acting on it", tv.name, now - at)
                    continue
                try:
                    await tv._power_off()
                    log.info("%s: switched off by its sleep timer", tv.name)
                except Exception as e:
                    log.warning("%s: sleep timer could not switch it off: %s", tv.name, e)

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        macs = self._load("macs.json")
        for name, host, mac in self._spec():
            stored = macs.get(name.lower())
            stored = [stored] if isinstance(stored, str) else list(stored or ())
            tv = WebOSTV(self, name, host, ([mac] if mac else []) + stored)
            self._tvs[tv.key] = tv
            tv.start()
        self._sleep_task = asyncio.get_running_loop().create_task(self._sleeper())

    async def stop(self) -> None:
        task, self._sleep_task = self._sleep_task, None
        if task is not None:
            task.cancel()
            await asyncio.wait({task}, timeout=5)
        tvs, self._tvs = list(self._tvs.values()), {}
        await asyncio.gather(*(tv.stop() for tv in tvs), return_exceptions=True)

    def devices(self) -> list[Device]:
        return list(self._tvs.values())

    # -- caps on the delegate -----------------------------------------------
    @capability("list", risk="read", scope="multi-machine")
    async def _list(self) -> dict:
        """Every configured TV: ``{items: [{name, host, on, app, reachable, paired,
        wake_on_lan, macs, error}]}``."""
        items = []
        for tv in self._tvs.values():
            s = tv.state
            items.append({"name": tv.name, "host": tv.host, "on": bool(s.get("on")),
                          "app": s.get("app"), "reachable": bool(s.get("reachable")),
                          "paired": bool(s.get("paired")), "wake_on_lan": s.get("wake_on_lan"),
                          "macs": list(tv.macs), "error": s.get("error")})
        return {"items": items}

    @capability("discover", risk="read", timeout=20, scope="multi-machine")
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
