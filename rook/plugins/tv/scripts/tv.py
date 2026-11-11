"""LG webOS TVs over their control API (SSAP, "second screen").

The TV listens on ``ws://<tv>:3000`` (newer firmware: ``wss://<tv>:3001``
with a self-signed certificate). A client registers with the permission
manifest below; the first time the TV shows an "allow this device?" prompt
and answers with a client key that later registrations present instead.
After that it is JSON request/response by ``id``. Remote buttons go over a
second socket the TV hands out.

The script runs on the hub: each call opens one WebSocket to the TV over a
TCP (or TLS) stream from the appliance's delegate (rook.kit.net), registers
with the instance's key, asks what it needs and closes. Wake-on-LAN goes out
over the delegate's UDP socket (net.udp); the MAC the delegate reaches the TV
at comes from its ARP table (file.read /proc/net/arp).
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
import time
import urllib.parse
from typing import Any

from rook.kit.ctx import KitError
from rook.kit.net import Stream, WebSocket, http, values

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
POINTER_IDLE = 60.0     # a pointer socket stays open this long after the last button press
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


class Session:
    """One registered SSAP connection to one TV, for one call."""

    def __init__(self, tv: "_TV"):
        self.tv = tv
        self.ws: WebSocket | None = None
        self.ids = itertools.count(1)

    async def _open(self) -> WebSocket:
        """``ws://host:port``; on the default port, ``wss://:3001`` when refused."""
        ctx, wid, host = self.tv.ctx, self.tv.ctx.worker["id"], self.tv.host
        try:
            return await WebSocket.connect(ctx, wid, host, self.tv.port, "/", timeout=CONNECT_TIMEOUT)
        except KitError as e:
            if self.tv.port != PORT:
                raise
            first = e
        try:
            return await WebSocket.connect(ctx, wid, host, TLS_PORT, "/", tls=True, verify=False,
                                           timeout=CONNECT_TIMEOUT)
        except KitError:
            raise first from None

    async def connect(self, client_key: str | None, *, prompt: bool = False,
                      prompt_timeout: float = 60.0) -> str:
        """Open and register; returns the client key in force. Without a key,
        ``prompt=True`` waits for a person to accept the TV's prompt."""
        if not client_key and not prompt:
            raise NotPaired("not paired")
        self.ws = await self._open()
        try:
            payload: dict[str, Any] = {"forcePairing": False, "pairingType": "PROMPT", "manifest": MANIFEST}
            if client_key:
                payload["client-key"] = client_key
            await self.ws.send(json.dumps({"type": "register", "id": "register_0", "payload": payload}))
            wait = prompt_timeout if prompt and not client_key else REQUEST_TIMEOUT
            deadline = time.monotonic() + wait
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError
                try:
                    msg = json.loads(await self.ws.recv(left))
                except KitError as e:
                    if "no answer" in e.message:
                        raise TimeoutError from None
                    raise
                if msg.get("id") != "register_0":
                    continue
                if msg.get("type") == "registered":
                    return (msg.get("payload") or {}).get("client-key") or client_key or ""
                if msg.get("type") == "error":
                    err = str(msg.get("error") or "registration failed")
                    raise NotPaired(err) if err.startswith("403") or "denied" in err else SSAPError(err)
                # {"type": "response", "payload": {"pairingType": "PROMPT"}}: waiting for a person
        except BaseException:
            await self.close()
            raise

    async def request(self, uri: str, payload: dict | None = None, timeout: float = REQUEST_TIMEOUT) -> dict:
        mid = str(next(self.ids))
        await self.ws.send(json.dumps({"type": "request", "id": mid, "uri": f"ssap://{uri}", "payload": payload or {}}))
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                raw = await self.ws.recv(left)
            except KitError as e:
                if "no answer" in e.message:
                    raise TimeoutError from None
                raise SSAPError("connection to the TV closed") from None
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if str(msg.get("id")) == mid:
                break
        body = msg.get("payload") or {}
        if msg.get("type") == "error":
            raise SSAPError(str(msg.get("error") or body.get("errorText") or "request failed"))
        if body.get("returnValue") is False:
            raise SSAPError(str(body.get("errorText") or "request failed"))
        return body

    async def pointer(self) -> WebSocket:
        """The TV's pointer-input socket, open (close it)."""
        path = (await self.request(POINTER_SOCKET)).get("socketPath")
        u = urllib.parse.urlsplit(str(path or ""))
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise SSAPError("the TV gave no input socket")
        return await WebSocket.connect(self.tv.ctx, self.tv.ctx.worker["id"], u.hostname,
                                       u.port or (443 if u.scheme == "wss" else 80), u.path or "/",
                                       tls=u.scheme == "wss", verify=False, timeout=CONNECT_TIMEOUT)

    async def close(self) -> None:
        ws, self.ws = self.ws, None
        if ws is not None:
            await ws.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()


class _Pointer:
    """One TV's pointer-input socket (and the session it came from), kept open
    between button presses: opening costs some twenty calls to the delegate,
    a press on an open socket one write. Closed after POINTER_IDLE without a
    press; a press that finds it dead opens another."""

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.lock = asyncio.Lock()
        self.session: Session | None = None
        self.ws: WebSocket | None = None
        self.used = 0.0
        self.timer: asyncio.TimerHandle | None = None

    def fresh(self) -> bool:
        return self.ws is not None and time.monotonic() - self.used < POINTER_IDLE

    def bind(self, ctx) -> None:
        """Its stream calls ride the call in hand, not the one that opened it."""
        for ws in (self.ws, self.session.ws if self.session else None):
            if ws is not None:
                ws.stream.ctx = ctx
        if self.session is not None:
            self.session.tv.ctx = ctx

    def keep(self, session: Session | None = None, ws: WebSocket | None = None) -> None:
        if ws is not None:
            self.session, self.ws = session, ws
        self.used = time.monotonic()
        if self.timer is not None:
            self.timer.cancel()
        self.timer = self.loop.call_later(POINTER_IDLE, lambda: asyncio.ensure_future(self.expire()))

    async def expire(self) -> None:
        async with self.lock:
            if not self.fresh():
                await self.drop()

    async def drop(self) -> None:
        ws, session, self.ws, self.session = self.ws, self.session, None, None
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        for conn in (ws, session):
            if conn is not None:
                try:
                    await conn.close()
                except Exception:
                    pass


_POINTERS: dict[tuple, _Pointer] = {}


def _pointer(tv: "_TV") -> _Pointer:
    key = (tv.ctx.worker["id"], tv.host, tv.port, tv.key)
    slot = _POINTERS.get(key)
    if slot is None or slot.loop is not asyncio.get_running_loop():
        slot = _POINTERS[key] = _Pointer()
    return slot


class _TV:
    def __init__(self, ctx):
        # The instance the hub carried with the call (spec 10.8): {name, host,
        # port, settings: {client_key, mac}}, the key resolved from the vault.
        a = values(ctx.appliance)
        self.host = str(a.get("host") or "")
        if not _HOST.fullmatch(self.host):
            raise ctx.Error("tv: this call has no appliance instance (or its host is not a host name)")
        self.port = int(a.get("port") or PORT)
        self.name = str(a.get("name") or self.host)
        self.key = str(a.get("client_key") or "") or None
        self.macs = _macs(a.get("mac"))
        self.ctx = ctx


async def _connect(tv: _TV) -> Session:
    """A registered session (close it), or NotPaired / KitError."""
    c = Session(tv)
    await c.connect(tv.key)
    return c


async def _live(tv: _TV) -> Session:
    try:
        return await _connect(tv)
    except NotPaired:
        raise tv.ctx.Error(f"{tv.name} is not paired: run tv.pair while someone is at the TV") from None
    except Exception as e:
        raise tv.ctx.Error(f"{tv.name} is off or unreachable ({_why(e)}); tv.power.on switches it on",
                           retry=True) from None


async def _req(c: Session, uri: str, payload: dict | None = None) -> dict:
    tv = c.tv
    try:
        return await c.request(uri, payload)
    except SSAPError as e:
        raise tv.ctx.Error(f"{tv.name}: {e}") from None
    except TimeoutError:
        raise tv.ctx.Error(f"{tv.name} did not answer within {REQUEST_TIMEOUT:g} s; it may be "
                           "going to standby", retry=True) from None


async def _try(c: Session, uri: str, payload: dict | None = None) -> dict:
    try:
        return await c.request(uri, payload)
    except Exception:
        return {}


def _why(e: BaseException) -> str:
    if isinstance(e, KitError):
        return e.message
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


# -- apps and inputs ------------------------------------------------------------

def _norm(text: str) -> str:
    """App names as people say them: case, spaces and punctuation don't count,
    and ``+`` is "plus" (``disney plus`` finds ``Disney+``)."""
    return re.sub(r"[^a-z0-9]", "", str(text).lower().replace("+", "plus"))


async def _apps(c: Session) -> dict[str, str]:
    points = await _req(c, LAUNCH_POINTS)
    return {p["id"]: p.get("title") or p["id"] for p in points.get("launchPoints") or [] if p.get("id")}


async def _inputs(c: Session) -> list[dict]:
    got = await _req(c, INPUTS)
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


def arp_mac(host: str, table: str) -> str | None:
    """The MAC in an ARP table (Linux's /proc/net/arp text) for ``host``: it
    names the interface the TV is actually connected by."""
    for line in table.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 4 and parts[0] == host and parts[2] != "0x0":
            mac = _mac(parts[3])
            return mac if mac and mac != "00:00:00:00:00:00" else None
    return None


async def _arp(tv: _TV) -> str | None:
    """What the delegate reaches the TV at, from its ARP table (Linux workers)."""
    if tv.ctx.worker.get("os") == "windows":
        return None
    try:
        got = await tv.ctx.call_on(tv.ctx.worker["id"], "file.read", path="/proc/net/arp")
    except KitError:
        return None
    return arp_mac(tv.host, got.get("content") or "")


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


async def _all_macs(tv: _TV) -> list[str]:
    return list(dict.fromkeys([*filter(None, [await _arp(tv)]), *tv.macs, *(_learned(tv).get(tv.name) or [])]))


def magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "")) * 16


async def _send_wol(tv: _TV, macs: list[str]) -> list[str]:
    """One magic packet per MAC, broadcast and to the TV's address, from the
    delegate's UDP socket; the addresses at least one packet left for."""
    sent = []
    try:
        async with await Stream.udp(tv.ctx, tv.ctx.worker["id"]) as s:
            for addr in ("255.255.255.255", tv.host):
                for mac in macs:
                    try:
                        await s.send(magic_packet(mac), to=f"{addr}:9")
                    except KitError:
                        continue
                    if addr not in sent:
                        sent.append(addr)
    except KitError:
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
                **({"macs": macs} if (macs := await _all_macs(tv)) else {})}
    async with c:
        power = (await _try(c, POWER_STATE)).get("state")
        app_id = (await _try(c, FOREGROUND_APP)).get("appId") or None
        vol = await _try(c, GET_VOLUME)
        vol = vol.get("volumeStatus") or vol
        sound = (await _try(c, SOUND_OUTPUT)).get("soundOutput")
        info = await _try(c, CONNECTION_INFO)
        model = (await _try(c, SYSTEM_INFO)).get("modelName")
        wol = ((await _try(c, GET_SETTINGS, {"category": "network", "keys": ["wolwowlOnOff"]}))
               .get("settings") or {}).get("wolwowlOnOff")
        apps, inputs = {}, []
        if app_id:
            try:
                apps = await _apps(c)
                inputs = await _inputs(c)
            except Exception:
                pass
    seen = [_mac((info.get(k) or {}).get("macAddress")) for k in ("wifiInfo", "wiredInfo")]
    macs = list(dict.fromkeys([*filter(None, [await _arp(tv), *seen]), *tv.macs]))
    if macs:
        _learn(tv, macs)
    on = power in ON_STATES if power else bool(app_id)
    out.update(on=on, power=power, reachable=True, paired=True, app_id=app_id,
               app=_title(app_id, apps, inputs), volume=vol.get("volume"),
               muted=vol.get("muteStatus", vol.get("muted")), sound_output=sound, model=model,
               screen="off" if power == "Screen Off" else ("on" if power == "Active" else None),
               wake_on_lan=None if wol is None else str(wol).lower() in ("true", "on", "1"),
               macs=macs or None)
    return {k: v for k, v in out.items() if v is not None}


async def app_list(ctx) -> dict:
    async with await _live(_TV(ctx)) as c:
        apps = await _apps(c)
    return {"items": [{"id": k, "title": v} for k, v in sorted(apps.items(), key=lambda kv: kv[1].lower())]}


async def inputs(ctx) -> dict:
    async with await _live(_TV(ctx)) as c:
        return {"items": await _inputs(c)}


async def notify(ctx, text: str, title: str | None = None) -> dict:
    async with await _live(_TV(ctx)) as c:
        await _req(c, TOAST, {"message": f"{title}: {text}" if title else text})
    return {"ok": True}


async def launch(ctx, app: str, params: dict | None = None) -> dict:
    tv = _TV(ctx)
    if params is not None and not isinstance(params, dict):
        raise ctx.Error("params must be an object")
    async with await _live(tv) as c:
        apps = await _apps(c)
        hits = _match(apps, app)
        if len(hits) > 1:
            raise ctx.Error(f"{app!r} matches several apps: "
                            + ", ".join(f"{apps[k]} ({k})" for k in hits[:8]))
        if not hits:
            if any(_norm(app) in (_norm(d.get("label") or ""), _norm(d.get("id") or ""))
                   for d in await _inputs(c)):
                raise ctx.Error(f"{app!r} is an input, not an app: use tv.set_input")
            raise ctx.Error(f"no app matches {app!r}; tv.app.list lists them")
        payload: dict[str, Any] = {"id": hits[0]}
        if params is not None:
            payload["params"] = params
        await _req(c, LAUNCH, payload)
    return {"ok": True, "app_id": hits[0], "app": apps[hits[0]]}


async def set_input(ctx, input: str) -> dict:
    want = input.lower().replace(" ", "").replace("_", "")
    async with await _live(_TV(ctx)) as c:
        for d in await _inputs(c):
            if any(str(d.get(k) or "").lower().replace(" ", "").replace("_", "") == want for k in ("id", "label")):
                await _req(c, SWITCH_INPUT, {"inputId": d["id"]})
                return {"ok": True, "input": d["id"], "label": d.get("label")}
    raise ctx.Error(f"no input matches {input!r}; tv.inputs lists them")


async def sound(ctx, level: int | None = None, step: int | None = None, mute: bool | None = None) -> dict:
    if level is not None and step is not None:
        raise ctx.Error("give level or step, not both")
    if level is None and step is None and mute is None:
        raise ctx.Error("give level, step or mute")
    async with await _live(_TV(ctx)) as c:
        if mute is not None:
            await _req(c, SET_MUTE, {"mute": bool(mute)})
        if level is not None:
            await _req(c, SET_VOLUME, {"volume": level})
        elif step:
            for _ in range(abs(step)):
                await _req(c, VOLUME_UP if step > 0 else VOLUME_DOWN)
        vol = await _req(c, GET_VOLUME)
        vol = vol.get("volumeStatus") or vol
        so = (await _try(c, SOUND_OUTPUT)).get("soundOutput")
    out = {"ok": True, "volume": vol.get("volume"), "muted": vol.get("muteStatus", vol.get("muted"))}
    if so and not str(so).startswith("tv_"):   # tv_external_speaker: both play
        out["note"] = (f"sound goes to {so}: the TV's volume may not change what you hear; "
                       "that device has its own")
    return out


async def media(ctx, action: str) -> dict:
    async with await _live(_TV(ctx)) as c:
        await _req(c, MEDIA[action])
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
    press = f"type:button\nname:{key}\n\n"
    slot = _pointer(tv)
    async with slot.lock:
        if slot.fresh():
            try:
                slot.bind(ctx)
                await slot.ws.send(press)
                slot.keep()
                return {"ok": True, "button": key}
            except Exception:
                pass        # the TV closed it (standby, a restart): open another
        await slot.drop()
        c = await _live(tv)
        ws = None
        try:
            ws = await c.pointer()
            await ws.send(press)
        except Exception as e:
            for conn in (ws, c):
                if conn is not None:
                    await conn.close()
            raise ctx.Error(f"{tv.name}: {_why(e)}") from None
        slot.keep(c, ws)
    return {"ok": True, "button": key}


async def type_text(ctx, text: str = "", enter: bool = False, replace: bool = False,
                    app: str | None = None) -> dict:
    tv = _TV(ctx)
    if not text and not enter and not replace:
        raise ctx.Error("give text, enter or replace")
    async with await _live(tv) as c:   # off or unpaired: say so plainly, not as a text-field problem
        front = (await _try(c, FOREGROUND_APP)).get("appId")
        apps = await _apps(c)
        if app is not None and front not in _match(apps, app):
            raise ctx.Error(f"{tv.name}: {_title(front, apps, []) or 'no app'} is in front, not {app!r}; "
                            "nothing typed")
        try:
            if text or replace:
                await _req(c, INSERT_TEXT, {"text": text, "replace": bool(replace)})
            if enter:
                await _req(c, ENTER_KEY)
        except ctx.Error as e:
            raise ctx.Error(f"{e.message}. A text field must be open on the TV: open the app's search "
                            "first (tv.hid.key, or the app's own search screen)") from None
    return {"ok": True, "typed": len(text), "enter": enter, "app": _title(front, apps, [])}


async def open_url(ctx, url: str) -> dict:
    async with await _live(_TV(ctx)) as c:
        await _req(c, OPEN_URL, {"target": url})
    return {"ok": True}


async def capture(ctx, quality: int | None = None) -> Any:
    """What the TV shows, as a JPEG (960x540 on a CX): the TV writes a
    capture and hands back its address on the TV itself, which is fetched
    from there (never a redirect) and nowhere else. The remote's pointer is
    not in it."""
    tv = _TV(ctx)
    async with await _live(tv) as c:
        got = await _req(c, CAPTURE, {"path": "/tmp/capture.jpg", "method": "DISPLAY", "format": "JPG"})
    uri = str(got.get("imageUri") or "")
    u = urllib.parse.urlsplit(uri)
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() != tv.host.strip("[]").lower():
        raise ctx.Error(f"{tv.name}: the TV gave no capture address of its own ({uri[:100]!r})")
    try:
        status, _, data = await http(ctx, ctx.worker["id"], "GET", u.hostname, u.port or (443 if u.scheme == "https" else 80),
                                     (u.path or "/") + (f"?{u.query}" if u.query else ""), tls=u.scheme == "https",
                                     verify=False, timeout=REQUEST_TIMEOUT, max_bytes=CAPTURE_MAX)
    except KitError as e:
        raise ctx.Error(f"{tv.name}: could not fetch the capture ({e.message})", retry=True) from None
    if status != 200:
        raise ctx.Error(f"{tv.name}: could not fetch the capture (HTTP {status})", retry=True)
    if not data.startswith(b"\xff\xd8"):
        raise ctx.Error(f"{tv.name}: the capture is not a JPEG")
    return ctx.file(data=data, name="tv.jpg", mime="image/jpeg")


async def screen_on(ctx) -> dict:
    async with await _live(_TV(ctx)) as c:
        await _req(c, SCREEN_ON, {"standbyMode": "active"})
    return {"ok": True, "screen": "on"}


async def screen_off(ctx) -> dict:
    async with await _live(_TV(ctx)) as c:
        await _req(c, SCREEN_OFF, {"standbyMode": "active"})
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
    async with c:
        power = (await _try(c, POWER_STATE)).get("state")
        if power and power not in ON_STATES:
            # Answering from network standby: already off. Never send "off" then.
            return {"ok": True, "already_off": True}
        try:
            await c.request(POWER_OFF)
        except SSAPError as e:
            if "closed" not in str(e):   # the TV often drops the connection as it goes off, before it answers
                raise ctx.Error(f"{tv.name}: {e}") from None
        except TimeoutError:
            pass
    return {"ok": True}


async def _on(tv: _TV, timeout: float = REQUEST_TIMEOUT) -> bool:
    c = await _connect(tv)
    async with c:
        return (await c.request(POWER_STATE, timeout=timeout)).get("state") in ON_STATES


async def power_on(ctx, wait: int = 20) -> dict:
    tv = _TV(ctx)
    try:
        if await _on(tv, timeout=3):
            return {"ok": True, "on": True, "already_on": True}
    except NotPaired:
        raise ctx.Error(f"{tv.name} is not paired: run tv.pair while someone is at the TV") from None
    except Exception:
        pass
    macs = await _all_macs(tv)
    if not macs:
        raise ctx.Error(f"{tv.name}: MAC address unknown; set the instance's mac, or run tv.status "
                        "once while the TV is on")
    sent = await _send_wol(tv, macs)
    out: dict[str, Any] = {"macs": macs, "sent_to": sent}
    if not wait:
        return {"ok": bool(sent), **out}
    start = time.monotonic()
    end = start + wait
    while time.monotonic() < end:
        try:
            if await asyncio.wait_for(_on(tv), max(0.1, end - time.monotonic())):
                return {"ok": True, "on": True, "waited": round(time.monotonic() - start, 1), **out}
        except NotPaired:
            raise ctx.Error(f"{tv.name} came on but is not paired: run tv.pair") from None
        except Exception:
            pass
        await asyncio.sleep(min(WAKE_EVERY, max(0.0, end - time.monotonic())))
        if time.monotonic() < end:
            await _send_wol(tv, macs)
    raise ctx.Error(f"{tv.name} did not come on within {wait} s (Wake-on-LAN sent to {', '.join(macs)}): "
                    "check the TV's Turn on via Wi-Fi (Mobile TV On) setting under Settings > General > "
                    "Devices > TV Management; some TVs only wake from a short standby")


async def pair(ctx, key: str | None = None, timeout: int = 60) -> dict:
    """Without ``key`` the TV shows its prompt; with one, check a key paired
    elsewhere. Either way the result's ``settings.client_key`` is what
    onboarding stores on the instance (in the vault)."""
    tv = _TV(ctx)
    c = Session(tv)
    try:
        got = await c.connect(key, prompt=key is None, prompt_timeout=timeout)
    except NotPaired as e:
        raise ctx.Error(f"{tv.name}: pairing refused ({e})") from None
    except TimeoutError:
        raise ctx.Error(f"{tv.name}: nobody accepted the prompt in time") from None
    except Exception as e:
        raise ctx.Error(f"{tv.name} is off or unreachable ({_why(e)})", retry=True) from None
    await c.close()
    return {"paired": True, "settings": {"client_key": got}}
