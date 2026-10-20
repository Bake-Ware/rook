"""A small client for the LG webOS TV control API (SSAP, "second screen").

The TV listens on ``ws://<tv>:3000`` (and ``wss://<tv>:3001`` with a
self-signed certificate, which newer firmware requires). A client registers
with a manifest of the permissions it wants; the first time, the TV shows an
"allow this device?" prompt and answers with a ``client-key`` that later
registrations present instead. After that it is JSON request/response by
``id``, plus subscriptions that keep answering on the same ``id``.

The manifest below is the permission list the open-source clients send
(aiowebostv, Apache-2.0); a key paired with it carries those permissions.
Buttons (HOME, BACK, arrows…) go over a second socket the TV hands out.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import ssl
import importlib.util
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.worker.webos")

PORT = 3000
TLS_PORT = 3001

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

# Endpoints this driver uses (``ssap://`` is prepended).
FOREGROUND_APP = "com.webos.applicationManager/getForegroundAppInfo"
LAUNCH_POINTS = "com.webos.applicationManager/listLaunchPoints"
POWER_STATE = "com.webos.service.tvpower/power/getPowerState"
SCREEN_OFF = "com.webos.service.tvpower/power/turnOffScreen"
SCREEN_ON = "com.webos.service.tvpower/power/turnOnScreen"
AUDIO_STATUS = "audio/getStatus"
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
MEDIA = {
    "play": "media.controls/play",
    "pause": "media.controls/pause",
    "stop": "media.controls/stop",
    "rewind": "media.controls/rewind",
    "fast_forward": "media.controls/fastForward",
}


def supported() -> bool:
    """Whether ``websockets`` is importable here. A Rook dependency, but a
    worker bundle on a bare system Python may lack it; then webos stays off
    instead of failing to import."""
    return importlib.util.find_spec("websockets") is not None


def _ws():
    import websockets   # deferred: see supported()
    return websockets


class SSAPError(Exception):
    """The TV refused or could not complete a request."""


class NotPaired(SSAPError):
    """The TV has no key for this client, or rejected the one presented."""


class SSAPClient:
    """One registered connection to one TV."""

    def __init__(self, host: str, *, connect_timeout: float = 4.0,
                 request_timeout: float = 8.0) -> None:
        self.host = host
        self.connect_timeout = connect_timeout
        self.request_timeout = request_timeout
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._ids = itertools.count(1)
        self._pending: dict[str, asyncio.Future] = {}
        self._subs: dict[str, Callable[[dict], Any]] = {}
        self.client_key: str | None = None
        self.closed = asyncio.Event()
        self.closed.set()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self.closed.is_set()

    async def _open(self):
        """``ws://:3000`` first; ``wss://:3001`` (self-signed) when refused."""
        websockets = _ws()
        try:
            return await websockets.connect(
                f"ws://{self.host}:{PORT}", open_timeout=self.connect_timeout,
                ping_interval=20, max_size=4 * 1024 * 1024)
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            first = e
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE   # the TV's certificate is self-signed
        try:
            return await websockets.connect(
                f"wss://{self.host}:{TLS_PORT}", ssl=ctx, open_timeout=self.connect_timeout,
                ping_interval=20, max_size=4 * 1024 * 1024)
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException):
            raise first from None

    async def connect(self, client_key: str | None, *, prompt: bool = False,
                      prompt_timeout: float = 60.0) -> str:
        """Open and register. Without a key, ``prompt=True`` asks the TV to
        show its pairing prompt and waits up to ``prompt_timeout`` for a
        person to accept; otherwise :class:`NotPaired` is raised at once.
        Returns the client key in force."""
        if not client_key and not prompt:
            raise NotPaired("not paired")
        self._ws = await self._open()
        self.closed.clear()
        try:
            payload: dict[str, Any] = {"forcePairing": False, "pairingType": "PROMPT",
                                       "manifest": MANIFEST}
            if client_key:
                payload["client-key"] = client_key
            await self._ws.send(json.dumps({"type": "register", "id": "register_0",
                                            "payload": payload}))
            wait = prompt_timeout if (prompt and not client_key) else self.request_timeout
            key = await asyncio.wait_for(self._await_registered(), wait)
        except BaseException:
            await self.close()
            raise
        self.client_key = key
        self._reader = asyncio.get_running_loop().create_task(self._read())
        return key

    async def _await_registered(self) -> str:
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") != "register_0":
                continue
            kind = msg.get("type")
            if kind == "registered":
                return (msg.get("payload") or {}).get("client-key") or ""
            if kind == "error":
                err = str(msg.get("error") or "registration failed")
                raise NotPaired(err) if err.startswith("403") or "denied" in err \
                    else SSAPError(err)
            # {"type": "response", "payload": {"pairingType": "PROMPT"}}: waiting for a person

    async def _read(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                mid = str(msg.get("id"))
                cb = self._subs.get(mid)
                if cb is not None:
                    if msg.get("type") == "response":
                        try:
                            cb(msg.get("payload") or {})
                        except Exception:
                            log.debug("subscription callback raised", exc_info=True)
                    continue
                fut = self._pending.pop(mid, None)
                if fut is not None and not fut.done():
                    fut.set_result(msg)
        except Exception as e:
            log.debug("ssap %s: connection ended: %s", self.host, e)
        finally:
            self._fail_pending(SSAPError("connection to the TV closed"))
            self.closed.set()

    def _fail_pending(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def request(self, uri: str, payload: dict | None = None,
                      timeout: float | None = None) -> dict:
        """One request; returns its payload, or raises :class:`SSAPError`."""
        if not self.connected:
            raise SSAPError("not connected to the TV")
        mid = str(next(self._ids))
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self._ws.send(json.dumps({"type": "request", "id": mid,
                                        "uri": f"ssap://{uri}", "payload": payload or {}}))
        try:
            msg = await asyncio.wait_for(fut, timeout or self.request_timeout)
        finally:
            self._pending.pop(mid, None)
        body = msg.get("payload") or {}
        if msg.get("type") == "error":
            raise SSAPError(str(msg.get("error") or body.get("errorText") or "request failed"))
        if body.get("returnValue") is False:
            raise SSAPError(str(body.get("errorText") or "request failed"))
        return body

    async def subscribe(self, uri: str, callback: Callable[[dict], Any],
                        payload: dict | None = None) -> None:
        """Keep receiving ``uri``'s updates; the first arrives right away."""
        if not self.connected:
            raise SSAPError("not connected to the TV")
        mid = f"sub{next(self._ids)}"
        self._subs[mid] = callback
        await self._ws.send(json.dumps({"type": "subscribe", "id": mid,
                                        "uri": f"ssap://{uri}", "payload": payload or {}}))

    async def button(self, name: str) -> None:
        """Press one remote button (HOME, BACK, UP, ENTER…) over the
        pointer-input socket the TV hands out."""
        sock = (await self.request(POINTER_SOCKET)).get("socketPath")
        if not sock:
            raise SSAPError("the TV gave no input socket")
        websockets = _ws()
        kwargs: dict[str, Any] = {"open_timeout": self.connect_timeout}
        if sock.startswith("wss:"):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            kwargs["ssl"] = ctx
        async with websockets.connect(sock, **kwargs) as ws:
            await ws.send(f"type:button\nname:{name}\n\n")

    async def close(self) -> None:
        ws, self._ws = self._ws, None
        if self._reader is not None and self._reader is not asyncio.current_task():
            self._reader.cancel()
        self._reader = None
        self._subs.clear()
        self._fail_pending(SSAPError("connection to the TV closed"))
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        self.closed.set()


async def run_with(client: SSAPClient, fn: Callable[[SSAPClient], Awaitable[Any]]) -> Any:
    """Small helper for one-off use outside the plugin (tests, scripts)."""
    try:
        return await fn(client)
    finally:
        await client.close()
