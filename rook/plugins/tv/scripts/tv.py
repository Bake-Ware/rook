"""LG webOS TVs over their control API (SSAP, "second screen").

The TV listens on ``ws://<tv>:3000`` (newer firmware: ``wss://<tv>:3001``
with a self-signed certificate). A client registers with the permission
manifest below; the first time the TV shows an "allow this device?" prompt
and answers with a client key that later registrations present instead.
After that it is JSON request/response by ``id``. Remote buttons go over a
second socket the TV hands out.

One registered connection per TV instance is kept in module state and
reused by every call; ``stop`` (the plugin's stop hook) closes them. No
background loop: each cap asks the TV what it needs.
"""

from __future__ import annotations

import asyncio
import inspect
import itertools
import json
import re
import socket
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any

PORT, TLS_PORT = 3000, 3001
CONNECT_TIMEOUT, REQUEST_TIMEOUT = 4.0, 8.0
WAKE_EVERY = 2.0   # power.on resends Wake-on-LAN and retries this often while it waits

# The permission list the open-source clients send (aiowebostv, Apache-2.0).
MANIFEST = {
    "appVersion": "1.1",
    "manifestVersion": 1,
    "permissions": [
        "APP_TO_APP", "CLOSE", "CONTROL_AUDIO", "CONTROL_DISPLAY",
        "CONTROL_INPUT_JOYSTICK", "CONTROL_INPUT_MEDIA_PLAYBACK",
        "CONTROL_INPUT_MEDIA_RECORDING", "CONTROL_INPUT_TEXT", "CONTROL_INPUT_TV",
        "CONTROL_MOUSE_AND_KEYBOARD", "CONTROL_POWER", "CONTROL_TV_SCREEN", "LAUNCH",
        "LAUNCH_WEBAPP", "READ_APP_STATUS", "READ_COUNTRY_INFO", "READ_CURRENT_CHANNEL",
        "READ_INPUT_DEVICE_LIST", "READ_INSTALLED_APPS", "READ_LGE_SDX",
        "READ_LGE_TV_INPUT_EVENTS", "READ_NETWORK_STATE", "READ_NOTIFICATIONS",
        "READ_POWER_STATE", "READ_RUNNING_APPS", "READ_SETTINGS", "READ_TV_CHANNEL_LIST",
        "READ_TV_CURRENT_TIME", "READ_UPDATE_INFO", "SEARCH", "TEST_OPEN",
        "TEST_PROTECTED", "TEST_SECURE", "UPDATE_FROM_REMOTE_APP",
        "WRITE_NOTIFICATION_ALERT", "WRITE_NOTIFICATION_TOAST", "WRITE_SETTINGS",
    ],
}

FOREGROUND_APP = "com.webos.applicationManager/getForegroundAppInfo"
LAUNCH_POINTS = "com.webos.applicationManager/listLaunchPoints"
POWER_STATE = "com.webos.service.tvpower/power/getPowerState"
SCREEN_OFF = "com.webos.service.tvpower/power/turnOffScreen"
SCREEN_ON = "com.webos.service.tvpower/power/turnOnScreen"
GET_VOLUME = "audio/getVolume"
SET_VOLUME = "audio/setVolume"
VOLUME_UP = "audio/volumeUp"
VOLUME_DOWN = "audio/volumeDown"
SET_MUTE = "audio/setMute"
INPUTS = "tv/getExternalInputList"
SWITCH_INPUT = "tv/switchInput"
LAUNCH = "system.launcher/launch"
OPEN_URL = "system.launcher/open"
TOAST = "system.notifications/createToast"
POWER_OFF = "system/turnOff"
CONNECTION_INFO = "com.webos.service.connectionmanager/getinfo"
SYSTEM_INFO = "system/getSystemInfo"
POINTER_SOCKET = "com.webos.service.networkinput/getPointerInputSocket"
SOUND_OUTPUT = "com.webos.service.apiadapter/audio/getSoundOutput"
GET_SETTINGS = "settings/getSystemSettings"
INSERT_TEXT = "com.webos.service.ime/insertText"
ENTER_KEY = "com.webos.service.ime/sendEnterKey"
CAPTURE = "tv/executeOneShot"
CAPTURE_MAX = 8 << 20   # a 960x540 JPEG is ~100 KB; refuse anything absurd
MEDIA = {"play": "media.controls/play", "pause": "media.controls/pause",
         "stop": "media.controls/stop", "rewind": "media.controls/rewind",
         "fast_forward": "media.controls/fastForward"}
ON_STATES = ("Active", "Screen Off", "Screen Saver")
BUTTONS = ("HOME", "BACK", "UP", "DOWN", "LEFT", "RIGHT", "ENTER", "MENU", "EXIT",
           "INFO", "RED", "GREEN", "YELLOW", "BLUE", "PLAY", "PAUSE", "STOP",
           "REWIND", "FASTFORWARD", "VOLUMEUP", "VOLUMEDOWN", "MUTE", "CHANNELUP",
           "CHANNELDOWN", "DASH", "ASTERISK", "CC", "QMENU", "1", "2", "3", "4", "5",
           "6", "7", "8", "9", "0")
KEY_NAMES = {"ESCAPE": "BACK", "ESC": "BACK", "BACKSPACE": "BACK", "OK": "ENTER", "SELECT": "ENTER"}

_HOST = re.compile(r"\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+")
_MAC = re.compile(r"^[0-9a-f]{2}([:-][0-9a-f]{2}){5}$", re.I)


class SSAPError(Exception):
    """The TV refused or could not complete a request."""


class NotPaired(SSAPError):
    """The TV has no key for this client, or rejected the one presented."""


def _no_verify() -> ssl.SSLContext:
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE   # the TV's certificate is self-signed
    return tls


async def _ws_connect(url: str, **kw):
    import websockets
    if "proxy" in inspect.signature(websockets.connect).parameters:
        kw["proxy"] = None   # websockets >= 15 reads proxy env vars; a TV is on the LAN
    if url.startswith("wss:"):
        kw["ssl"] = _no_verify()
    return await websockets.connect(url, open_timeout=CONNECT_TIMEOUT, **kw)


class Client:
    """One registered connection to one TV."""

    def __init__(self, host: str, port: int):
        self.host, self.port = host, port
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.ids = itertools.count(1)
        self.pending: dict[str, asyncio.Future] = {}
        self.loop = asyncio.get_running_loop()
        self.key: str | None = None   # the key this connection registered with

    @property
    def connected(self) -> bool:
        return self.ws is not None and self.reader is not None and not self.reader.done()

    async def _open(self):
        """``ws://host:port``; on the default port, ``wss://:3001`` when refused."""
        try:
            return await _ws_connect(f"ws://{self.host}:{self.port}", max_size=4 << 20)
        except Exception as e:
            if self.port != PORT:
                raise
            first = e
        try:
            return await _ws_connect(f"wss://{self.host}:{TLS_PORT}", max_size=4 << 20)
        except Exception:
            raise first from None

    async def connect(self, client_key: str | None, *, prompt: bool = False,
                      prompt_timeout: float = 60.0) -> str:
        """Open and register; returns the client key in force. Without a key,
        ``prompt=True`` waits for a person to accept the TV's prompt."""
        if not client_key and not prompt:
            raise NotPaired("not paired")
        self.ws = await self._open()
        try:
            payload: dict[str, Any] = {"forcePairing": False, "pairingType": "PROMPT",
                                       "manifest": MANIFEST}
            if client_key:
                payload["client-key"] = client_key
            await self.ws.send(json.dumps({"type": "register", "id": "register_0", "payload": payload}))
            async with asyncio.timeout(prompt_timeout if prompt and not client_key else REQUEST_TIMEOUT):
                key = await self._registered()
        except BaseException:
            await self.close()
            raise
        self.reader = self.loop.create_task(self._read())
        return key or client_key or ""

    async def _registered(self) -> str:
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") != "register_0":
                continue
            if msg.get("type") == "registered":
                return (msg.get("payload") or {}).get("client-key") or ""
            if msg.get("type") == "error":
                err = str(msg.get("error") or "registration failed")
                raise NotPaired(err) if err.startswith("403") or "denied" in err else SSAPError(err)
            # {"type": "response", "payload": {"pairingType": "PROMPT"}}: waiting for a person

    async def _read(self) -> None:
        try:
            async for raw in self.ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                fut = self.pending.pop(str(msg.get("id")), None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception:
            pass
        finally:
            self._fail(SSAPError("connection to the TV closed"))

    def _fail(self, exc: Exception) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self.pending.clear()

    async def request(self, uri: str, payload: dict | None = None, timeout: float = REQUEST_TIMEOUT) -> dict:
        if not self.connected:
            raise SSAPError("not connected to the TV")
        mid = str(next(self.ids))
        fut = self.loop.create_future()
        self.pending[mid] = fut
        try:
            await self.ws.send(json.dumps({"type": "request", "id": mid, "uri": f"ssap://{uri}",
                                           "payload": payload or {}}))
            async with asyncio.timeout(timeout):
                msg = await fut
        finally:
            self.pending.pop(mid, None)
        body = msg.get("payload") or {}
        if msg.get("type") == "error":
            raise SSAPError(str(msg.get("error") or body.get("errorText") or "request failed"))
        if body.get("returnValue") is False:
            raise SSAPError(str(body.get("errorText") or "request failed"))
        return body

    async def button(self, name: str) -> None:
        """Press one remote button over the pointer-input socket."""
        path = (await self.request(POINTER_SOCKET)).get("socketPath")
        if not path:
            raise SSAPError("the TV gave no input socket")
        ws = await _ws_connect(path)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                await ws.send(f"type:button\nname:{name}\n\n")
        finally:
            await ws.close()

    async def close(self) -> None:
        ws, self.ws = self.ws, None
        if self.reader is not None and self.reader is not asyncio.current_task():
            self.reader.cancel()
        self._fail(SSAPError("connection to the TV closed"))
        if ws is not None:
            try:
                async with asyncio.timeout(2):
                    await ws.close()
            except Exception:
                pass


# -- one connection per instance ------------------------------------------------

class _TV:
    def __init__(self, ctx):
        # The instance the hub carried with the call (spec 5.7): {name, host,
        # port, client_key, mac}, the key resolved from the vault.
        a = getattr(ctx, "appliance", None) or {}
        self.host = str(a.get("host") or "")
        if not _HOST.fullmatch(self.host):
            raise ctx.Error("tv: this call has no appliance instance (or its host is not a host name)")
        self.port = int(a.get("port") or PORT)
        self.name = str(a.get("name") or self.host)
        self.key = str(a.get("client_key") or "") or None
        self.macs = _macs(a.get("mac"))
        self.ctx = ctx
        self.id = f"{self.host}:{self.port}"


_clients: dict[str, Client] = {}
_locks: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _lock(tv: _TV) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    got = _locks.get(tv.id)
    if got is None or got[0] is not loop:
        got = _locks[tv.id] = (loop, asyncio.Lock())
    return got[1]


async def _connect(tv: _TV) -> Client:
    """The live client for this TV, connecting (and registering) if needed."""
    async with _lock(tv):
        c = _clients.get(tv.id)
        if c is not None and c.connected and c.loop is asyncio.get_running_loop() and c.key == tv.key:
            return c
        if c is not None:
            _clients.pop(tv.id, None)
            if c.loop is asyncio.get_running_loop():
                await c.close()
        c = Client(tv.host, tv.port)
        await c.connect(tv.key)
        c.key = tv.key
        _clients[tv.id] = c
        return c


async def _live(tv: _TV) -> Client:
    try:
        return await _connect(tv)
    except NotPaired:
        raise tv.ctx.Error(f"{tv.name} is not paired: run tv.pair while someone is at the TV") from None
    except Exception as e:
        raise tv.ctx.Error(f"{tv.name} is off or unreachable ({_why(e)}); tv.power.on switches it on",
                           retry=True) from None


async def _req(tv: _TV, uri: str, payload: dict | None = None) -> dict:
    for attempt in (1, 2):
        c = await _live(tv)
        try:
            return await c.request(uri, payload)
        except SSAPError as e:
            if attempt == 1 and not c.connected:
                continue   # the connection died before the TV answered: once more on a fresh one
            raise tv.ctx.Error(f"{tv.name}: {e}") from None
        except TimeoutError:
            await c.close()   # stopped answering without closing: the next call reconnects
            raise tv.ctx.Error(f"{tv.name} did not answer within {REQUEST_TIMEOUT:g} s; it may be "
                               "going to standby", retry=True) from None
        except Exception as e:   # the socket failed under the send
            await c.close()
            if attempt == 1:
                continue
            raise tv.ctx.Error(f"{tv.name}: lost the connection ({_why(e)})", retry=True) from None
    raise tv.ctx.Error(f"{tv.name}: lost the connection", retry=True)


async def _try(c: Client, uri: str, payload: dict | None = None) -> dict:
    try:
        return await c.request(uri, payload)
    except Exception:
        return {}


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


async def stop(ctx) -> None:
    """Stop hook: close every connection."""
    clients = list(_clients.values())
    _clients.clear()
    _locks.clear()
    loop = asyncio.get_running_loop()
    for c in clients:
        if c.loop is loop:
            await c.close()


# -- apps and inputs ------------------------------------------------------------

def _norm(text: str) -> str:
    """App names as people say them: case, spaces and punctuation don't count,
    and ``+`` is "plus" (``disney plus`` finds ``Disney+``)."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower().replace("+", "plus"))


async def _apps(tv: _TV) -> dict[str, str]:
    points = await _req(tv, LAUNCH_POINTS)
    return {p["id"]: p.get("title") or p["id"] for p in points.get("launchPoints") or [] if p.get("id")}


async def _inputs(tv: _TV) -> list[dict]:
    got = await _req(tv, INPUTS)
    return [{"id": d.get("id"), "label": d.get("label"), "app": d.get("appId"),
             "connected": bool(d.get("connected"))} for d in got.get("devices") or []]


def _match(apps: dict[str, str], app: str) -> list[str]:
    """App ids for ``app``: its id, else the best tier of title matches
    (same name, then starts with it, then contains it)."""
    if app in apps:
        return [app]
    want = _norm(app)
    if not want:
        return []
    names = {k: (_norm(v), _norm(k)) for k, v in apps.items()}
    for test in (lambda t, i: want in (t, i), lambda t, i: t.startswith(want),
                 lambda t, i: want in t or want in i):
        hits = [k for k, (t, i) in names.items() if test(t, i)]
        if hits:
            return hits
    return []


def _title(app_id: str | None, apps: dict, inputs: list) -> str | None:
    if not app_id:
        return None
    if app_id in apps:
        return apps[app_id]
    return next((d["label"] for d in inputs if d.get("app") == app_id and d.get("label")), app_id)


# -- MACs for Wake-on-LAN -------------------------------------------------------

def _mac(value) -> str | None:
    if isinstance(value, str) and _MAC.match(value.strip()):
        return value.strip().lower().replace("-", ":")
    return None


def _macs(value) -> list[str]:
    return list(dict.fromkeys(m for m in map(_mac, str(value or "").split(",")) if m))


def arp_mac(host: str, table: str = "/proc/net/arp") -> str | None:
    """The MAC this machine reaches ``host`` at, from the kernel's ARP table
    (Linux). It names the interface the TV is actually connected by."""
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


def _learned(tv: _TV) -> dict:
    try:
        return json.loads((tv.ctx.data / "macs.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _learn(tv: _TV, macs: list[str]) -> None:
    """Keep MACs seen while the TV was on, so power.on can wake it later
    without an instance ``mac`` (a TV only wakes on the interface it uses)."""
    try:
        known = _learned(tv)
        if known.get(tv.name) != macs:
            known[tv.name] = macs
            (tv.ctx.data / "macs.json").write_text(json.dumps(known, indent=1), encoding="utf-8")
    except Exception:
        pass   # no data dir here: nothing learned


def _all_macs(tv: _TV) -> list[str]:
    return list(dict.fromkeys([*filter(None, [arp_mac(tv.host)]), *tv.macs,
                               *(_learned(tv).get(tv.name) or [])]))


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


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
            except OSError:
                pass
    return sent


# -- caps -----------------------------------------------------------------------

async def status(ctx) -> dict:
    tv = _TV(ctx)
    out: dict[str, Any] = {"name": tv.name, "host": tv.host}
    try:
        c = await _connect(tv)
    except NotPaired:
        return {**out, "on": False, "reachable": True, "paired": False}
    except Exception as e:
        return {**out, "on": False, "reachable": False, "paired": bool(tv.key), "error": _why(e),
                **({"macs": macs} if (macs := _all_macs(tv)) else {})}
    power = (await _try(c, POWER_STATE)).get("state")
    app_id = (await _try(c, FOREGROUND_APP)).get("appId") or None
    vol = await _try(c, GET_VOLUME)
    vol = vol.get("volumeStatus") or vol
    sound = (await _try(c, SOUND_OUTPUT)).get("soundOutput")
    info = await _try(c, CONNECTION_INFO)
    model = (await _try(c, SYSTEM_INFO)).get("modelName")
    wol = ((await _try(c, GET_SETTINGS, {"category": "network", "keys": ["wolwowlOnOff"]}))
           .get("settings") or {}).get("wolwowlOnOff")
    seen = [_mac((info.get(k) or {}).get("macAddress")) for k in ("wifiInfo", "wiredInfo")]
    macs = list(dict.fromkeys([*filter(None, [arp_mac(tv.host), *seen]), *tv.macs]))
    if macs:
        _learn(tv, macs)
    apps, inputs = {}, []
    if app_id:
        try:
            apps = await _apps(tv)
            inputs = await _inputs(tv)
        except Exception:
            pass
    on = power in ON_STATES if power else bool(app_id)
    out.update(on=on, power=power, reachable=True, paired=True, app_id=app_id,
               app=_title(app_id, apps, inputs), volume=vol.get("volume"),
               muted=vol.get("muteStatus", vol.get("muted")), sound_output=sound, model=model,
               screen="off" if power == "Screen Off" else ("on" if power == "Active" else None),
               wake_on_lan=None if wol is None else str(wol).lower() in ("true", "on", "1"),
               macs=macs or None)
    return {k: v for k, v in out.items() if v is not None}


async def app_list(ctx) -> dict:
    apps = await _apps(_TV(ctx))
    return {"items": [{"id": k, "title": v} for k, v in sorted(apps.items(), key=lambda kv: kv[1].lower())]}


async def inputs(ctx) -> dict:
    return {"items": await _inputs(_TV(ctx))}


async def notify(ctx, text: str, title: str | None = None) -> dict:
    tv = _TV(ctx)
    await _req(tv, TOAST, {"message": f"{title}: {text}" if title else text})
    return {"ok": True}


async def launch(ctx, app: str, params: dict | None = None) -> dict:
    tv = _TV(ctx)
    if params is not None and not isinstance(params, dict):
        raise ctx.Error("params must be an object")
    apps = await _apps(tv)
    hits = _match(apps, app)
    if len(hits) > 1:
        raise ctx.Error(f"{app!r} matches several apps: "
                        + ", ".join(f"{apps[k]} ({k})" for k in hits[:8]))
    if not hits:
        if any(_norm(app) in (_norm(d.get("label") or ""), _norm(d.get("id") or ""))
               for d in await _inputs(tv)):
            raise ctx.Error(f"{app!r} is an input, not an app: use tv.set_input")
        raise ctx.Error(f"no app matches {app!r}; tv.app.list lists them")
    payload: dict[str, Any] = {"id": hits[0]}
    if params is not None:
        payload["params"] = params
    await _req(tv, LAUNCH, payload)
    return {"ok": True, "app_id": hits[0], "app": apps[hits[0]]}


async def set_input(ctx, input: str) -> dict:
    tv = _TV(ctx)
    want = input.lower().replace(" ", "").replace("_", "")
    for d in await _inputs(tv):
        if any(str(d.get(k) or "").lower().replace(" ", "").replace("_", "") == want for k in ("id", "label")):
            await _req(tv, SWITCH_INPUT, {"inputId": d["id"]})
            return {"ok": True, "input": d["id"], "label": d.get("label")}
    raise ctx.Error(f"no input matches {input!r}; tv.inputs lists them")


async def sound(ctx, level: int | None = None, step: int | None = None, mute: bool | None = None) -> dict:
    tv = _TV(ctx)
    if level is not None and step is not None:
        raise ctx.Error("give level or step, not both")
    if level is None and step is None and mute is None:
        raise ctx.Error("give level, step or mute")
    if mute is not None:
        await _req(tv, SET_MUTE, {"mute": bool(mute)})
    if level is not None:
        await _req(tv, SET_VOLUME, {"volume": level})
    elif step:
        for _ in range(abs(step)):
            await _req(tv, VOLUME_UP if step > 0 else VOLUME_DOWN)
    vol = await _req(tv, GET_VOLUME)
    vol = vol.get("volumeStatus") or vol
    out = {"ok": True, "volume": vol.get("volume"), "muted": vol.get("muteStatus", vol.get("muted"))}
    so = (await _try(await _live(tv), SOUND_OUTPUT)).get("soundOutput")
    if so and not str(so).startswith("tv_"):   # tv_external_speaker: both play
        out["note"] = (f"sound goes to {so}: the TV's volume may not change what you hear; "
                       "that device has its own")
    return out


async def media(ctx, action: str) -> dict:
    await _req(_TV(ctx), MEDIA[action])
    return {"ok": True, "action": action}


async def seek(ctx, by: int | None = None) -> dict:
    """webOS has no position seek: forward (or blank) is fast-forward, back is rewind."""
    return await media(ctx, "rewind" if by is not None and by < 0 else "fast_forward")


async def key(ctx, key: str, mods: list | None = None) -> dict:
    tv = _TV(ctx)
    if mods:
        raise ctx.Error("a TV remote has no modifier keys")
    key = key.upper()
    key = KEY_NAMES.get(key, key)
    if key not in BUTTONS:
        raise ctx.Error(f"unknown button {key!r}; one of {', '.join(BUTTONS)}")
    c = await _live(tv)
    try:
        await c.button(key)
    except Exception as e:
        raise ctx.Error(f"{tv.name}: {_why(e)}") from None
    return {"ok": True, "button": key}


async def type_text(ctx, text: str = "", enter: bool = False, replace: bool = False,
                    app: str | None = None) -> dict:
    tv = _TV(ctx)
    if not text and not enter and not replace:
        raise ctx.Error("give text, enter or replace")
    c = await _live(tv)   # off or unpaired: say so plainly, not as a text-field problem
    front = (await _try(c, FOREGROUND_APP)).get("appId")
    apps = await _apps(tv)
    if app is not None and front not in _match(apps, app):
        raise ctx.Error(f"{tv.name}: {_title(front, apps, []) or 'no app'} is in front, not {app!r}; "
                        "nothing typed")
    try:
        if text or replace:
            await _req(tv, INSERT_TEXT, {"text": text, "replace": bool(replace)})
        if enter:
            await _req(tv, ENTER_KEY)
    except ctx.Error as e:
        raise ctx.Error(f"{e.message}. A text field must be open on the TV: open the app's search "
                        "first (tv.hid.key, or the app's own search screen)") from None
    return {"ok": True, "typed": len(text), "enter": enter, "app": _title(front, apps, [])}


async def open_url(ctx, url: str) -> dict:
    await _req(_TV(ctx), OPEN_URL, {"target": url})
    return {"ok": True}


def _fetch(url: str) -> bytes:
    """GET the capture the TV just wrote (its own self-signed HTTPS, or HTTP)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),   # a TV is on the LAN
                                         urllib.request.HTTPSHandler(context=_no_verify()))
    with opener.open(url, timeout=REQUEST_TIMEOUT) as r:
        data = r.read(CAPTURE_MAX + 1)
    if len(data) > CAPTURE_MAX:
        raise SSAPError("the capture is too large")
    return data


async def capture(ctx, quality: int | None = None) -> Any:
    """What the TV shows, as a JPEG (960x540 on a CX): the TV writes a
    capture and hands back its address on the TV itself, which is fetched
    from there and nowhere else. The remote's pointer is not in it."""
    tv = _TV(ctx)
    got = await _req(tv, CAPTURE, {"path": "/tmp/capture.jpg", "method": "DISPLAY", "format": "JPG"})
    uri = str(got.get("imageUri") or "")
    u = urllib.parse.urlsplit(uri)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() != tv.host.strip("[]").lower():
        raise ctx.Error(f"{tv.name}: the TV gave no capture address of its own ({uri[:100]!r})")
    try:
        data = await asyncio.to_thread(_fetch, uri)
    except Exception as e:
        raise ctx.Error(f"{tv.name}: could not fetch the capture ({_why(e)})", retry=True) from None
    if not data.startswith(b"\xff\xd8"):
        raise ctx.Error(f"{tv.name}: the capture is not a JPEG")
    return ctx.file(data=data, name="tv.jpg", mime="image/jpeg")


async def screen_on(ctx) -> dict:
    await _req(_TV(ctx), SCREEN_ON, {"standbyMode": "active"})
    return {"ok": True, "screen": "on"}


async def screen_off(ctx) -> dict:
    await _req(_TV(ctx), SCREEN_OFF, {"standbyMode": "active"})
    return {"ok": True, "screen": "off"}


async def power_off(ctx, force: bool = False) -> dict:
    """``force`` is accepted for the contract; a TV has only one way off."""
    tv = _TV(ctx)
    try:
        c = await _connect(tv)
    except NotPaired:
        raise ctx.Error(f"{tv.name} is not paired: run tv.pair while someone is at the TV") from None
    except Exception:
        return {"ok": True, "already_off": True}
    power = (await _try(c, POWER_STATE)).get("state")
    if power and power not in ON_STATES:
        # Answering from network standby: already off. Never send "off" then.
        return {"ok": True, "already_off": True}
    try:
        await c.request(POWER_OFF)
    except SSAPError as e:
        if c.connected:   # the TV often drops the connection as it goes off, before it answers
            raise ctx.Error(f"{tv.name}: {e}") from None
    except TimeoutError:
        await c.close()
    return {"ok": True}


async def power_on(ctx, wait: int = 20) -> dict:
    tv = _TV(ctx)
    try:
        c = await _connect(tv)
        if (await c.request(POWER_STATE, timeout=3)).get("state") in ON_STATES:
            return {"ok": True, "on": True, "already_on": True}
    except NotPaired:
        raise ctx.Error(f"{tv.name} is not paired: run tv.pair while someone is at the TV") from None
    except Exception:
        pass
    macs = _all_macs(tv)
    if not macs:
        raise ctx.Error(f"{tv.name}: MAC address unknown; set the instance's mac, or run tv.status "
                        "once while the TV is on")
    sent = await asyncio.to_thread(_send_wol, macs, tv.host)
    out: dict[str, Any] = {"macs": macs, "sent_to": sent}
    if not wait:
        return {"ok": bool(sent), **out}
    loop = asyncio.get_running_loop()
    start = loop.time()
    end = start + wait
    while loop.time() < end:
        try:
            async with asyncio.timeout(max(0.1, end - loop.time())):
                c = await _connect(tv)
                if (await c.request(POWER_STATE)).get("state") in ON_STATES:
                    return {"ok": True, "on": True, "waited": round(loop.time() - start, 1), **out}
        except NotPaired:
            raise ctx.Error(f"{tv.name} came on but is not paired: run tv.pair") from None
        except Exception:
            pass
        await asyncio.sleep(min(WAKE_EVERY, max(0.0, end - loop.time())))
        if loop.time() < end:
            await asyncio.to_thread(_send_wol, macs, tv.host)
    raise ctx.Error(f"{tv.name} did not come on within {wait} s (Wake-on-LAN sent to {', '.join(macs)}): "
                    "check the TV's Turn on via Wi-Fi (Mobile TV On) setting under Settings > General > "
                    "Devices > TV Management; some TVs only wake from a short standby")


async def pair(ctx, key: str | None = None, timeout: int = 60) -> dict:
    """Without ``key`` the TV shows its prompt; with one, check a key paired
    elsewhere. Either way the result's ``settings.client_key`` is what
    onboarding stores on the instance (in the vault)."""
    tv = _TV(ctx)
    # A connection of its own: the live one keeps serving while someone walks to the TV.
    c = Client(tv.host, tv.port)
    try:
        got = await c.connect(key, prompt=key is None, prompt_timeout=timeout)
    except NotPaired as e:
        raise ctx.Error(f"{tv.name}: pairing refused ({e})") from None
    except TimeoutError:
        raise ctx.Error(f"{tv.name}: nobody accepted the prompt in time") from None
    except Exception as e:
        raise ctx.Error(f"{tv.name} is off or unreachable ({_why(e)})", retry=True) from None
    async with _lock(tv):
        old = _clients.get(tv.id)
        c.key = got
        _clients[tv.id] = c
        if old is not None and old.loop is asyncio.get_running_loop():
            await old.close()
    return {"paired": True, "settings": {"client_key": got}}
