"""Roku players and Roku TVs over ECP (External Control Protocol).

ECP is plain HTTP on port 8060 with no pairing: ``GET /query/...`` answers
XML, ``POST /keypress/<key>`` presses a remote button, ``POST /launch/<id>``
opens a channel. Newer Roku OS answers 403 while "Control by mobile apps"
is limited or off (Settings > System > Advanced system settings).

Every call is a few short requests; nothing is kept between calls.
"""

from __future__ import annotations

import asyncio
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

TIMEOUT = 5.0
MAX_REPLY = 1 << 20     # an app list is a few KB
KEY_GAP = 0.05          # between key presses, so the Roku doesn't drop any
_HOST = re.compile(r"\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9._~-]+")
ON_MODES = ("PowerOn",)


class _Roku:
    def __init__(self, ctx):
        a = getattr(ctx, "appliance", None) or {}
        self.host = str(a.get("host") or "")
        if not _HOST.fullmatch(self.host):
            raise ctx.Error("roku: this call has no appliance instance (or its host is not a host name)")
        self.port = int(a.get("port") or 8060)
        self.name = str(a.get("name") or self.host)
        self.base = f"http://{self.host}:{self.port}"
        self.ctx = ctx


def _http(method: str, url: str) -> tuple[int, bytes]:
    # No proxies (a Roku is on the LAN) and no redirects followed anywhere else.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with opener.open(req, timeout=TIMEOUT) as r:
            return r.status, r.read(MAX_REPLY)
    except urllib.error.HTTPError as e:
        return e.code, e.read(2000) if e.fp else b""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **kw):
        return None


async def _call(r: _Roku, method: str, path: str) -> bytes:
    try:
        code, body = await asyncio.to_thread(_http, method, r.base + path)
    except Exception as e:
        raise r.ctx.Error(f"{r.name} is off or unreachable ({_why(e)})", retry=True) from None
    if code == 403:
        raise r.ctx.Error(f"{r.name} refused ECP (403): set Settings > System > Advanced system settings > "
                          "Control by mobile apps > Network access to Default or Permissive")
    if code >= 400:
        raise r.ctx.Error(f"{r.name}: {method} {path.split('?')[0]} answered {code}")
    return body


async def _xml(r: _Roku, path: str) -> ET.Element:
    body = await _call(r, "GET", path)
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise r.ctx.Error(f"{r.name}: unexpected reply to {path}")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise r.ctx.Error(f"{r.name}: unreadable reply to {path}") from None


async def _key(r: _Roku, key: str) -> None:
    await _call(r, "POST", "/keypress/" + urllib.parse.quote(key, safe=""))


def _why(e: BaseException) -> str:
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def _text(el: ET.Element | None, tag: str) -> str | None:
    found = el.find(tag) if el is not None else None
    return (found.text or "").strip() or None if found is not None else None


# -- apps -----------------------------------------------------------------------

def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower().replace("+", "plus"))


async def _apps(r: _Roku) -> dict[str, str]:
    root = await _xml(r, "/query/apps")
    return {a.get("id"): (a.text or "").strip() or a.get("id") for a in root.findall("app") if a.get("id")}


def _match(apps: dict[str, str], app: str) -> list[str]:
    """Channel ids for ``app``: its id, else the best tier of name matches."""
    if app in apps:
        return [app]
    want = _norm(app)
    if not want:
        return []
    names = {k: _norm(v) for k, v in apps.items()}
    for test in (lambda t: t == want, lambda t: t.startswith(want), lambda t: want in t):
        hits = [k for k, t in names.items() if test(t)]
        if hits:
            return hits
    return []


# -- caps -----------------------------------------------------------------------

async def status(ctx) -> dict:
    r = _Roku(ctx)
    out: dict[str, Any] = {"name": r.name, "host": r.host}
    try:
        info = await _xml(r, "/query/device-info")
    except ctx.Error as e:
        return {**out, "on": False, "reachable": False, "error": e.message}
    power = _text(info, "power-mode")
    is_tv = _text(info, "is-tv") == "true"
    app = app_id = player = None
    try:
        active = (await _xml(r, "/query/active-app")).find("app")
        if active is not None:
            app, app_id = (active.text or "").strip() or None, active.get("id")
        player = (await _xml(r, "/query/media-player")).get("state")
    except ctx.Error:
        pass
    out.update(reachable=True, power=power, on=power in ON_MODES if power else True, is_tv=is_tv,
               app=app, app_id=app_id, player=player,
               device_name=_text(info, "user-device-name") or _text(info, "friendly-device-name"),
               model=_text(info, "friendly-model-name") or _text(info, "model-name"))
    return {k: v for k, v in out.items() if v is not None}


async def app_list(ctx) -> dict:
    apps = await _apps(_Roku(ctx))
    return {"items": [{"id": k, "title": v} for k, v in sorted(apps.items(), key=lambda kv: kv[1].lower())]}


async def launch(ctx, app: str, params: dict | None = None) -> dict:
    r = _Roku(ctx)
    if params is not None and not isinstance(params, dict):
        raise ctx.Error("params must be an object")
    apps = await _apps(r)
    hits = _match(apps, app)
    if len(hits) > 1:
        raise ctx.Error(f"{app!r} matches several channels: "
                        + ", ".join(f"{apps[k]} ({k})" for k in hits[:8]))
    if not hits:
        raise ctx.Error(f"no channel matches {app!r}; roku.app.list lists them")
    query = urllib.parse.urlencode({str(k): str(v) for k, v in (params or {}).items()})
    await _call(r, "POST", f"/launch/{urllib.parse.quote(hits[0], safe='')}" + (f"?{query}" if query else ""))
    return {"ok": True, "app_id": hits[0], "app": apps[hits[0]]}


async def type_text(ctx, text: str, enter: bool = False) -> dict:
    r = _Roku(ctx)
    for ch in text:
        await _key(r, "Lit_" + ch)
        await asyncio.sleep(KEY_GAP)
    if enter:
        await _key(r, "Enter")
    return {"ok": True, "typed": len(text), "enter": enter}


async def sound(ctx, level: int | None = None, step: int | None = None, mute: bool | None = None) -> dict:
    r = _Roku(ctx)
    if level is not None:
        raise ctx.Error("a Roku has no volume level over ECP; give step (+3, -2) instead")
    if not step and mute is None:
        raise ctx.Error("give step or mute")
    for _ in range(abs(step or 0)):
        await _key(r, "VolumeUp" if step > 0 else "VolumeDown")
        await asyncio.sleep(KEY_GAP)
    out: dict[str, Any] = {"ok": True}
    if step:
        out["stepped"] = step
    if mute is not None:
        await _key(r, "VolumeMute")
        out["note"] = "mute pressed: a Roku toggles mute and doesn't report which way it went"
    return out


async def media(ctx, want: str) -> dict:
    r = _Roku(ctx)
    state = (await _xml(r, "/query/media-player")).get("state")
    if (want == "play") == (state == "play"):
        return {"ok": True, "state": state, "unchanged": True}
    if want == "play" and state not in ("pause", "buffer"):
        raise ctx.Error(f"{r.name}: nothing to play (the player is {state or 'idle'})")
    await _key(r, "Play")      # one key toggles play and pause
    return {"ok": True, "state": want}


async def seek(ctx, by: int | None = None) -> dict:
    back = by is not None and by < 0
    await _key(_Roku(ctx), "Rev" if back else "Fwd")
    return {"ok": True, "action": "rewind" if back else "fast_forward"}


async def _power(r: _Roku) -> str | None:
    return _text(await _xml(r, "/query/device-info"), "power-mode")


async def power_off(ctx, force: bool = False) -> dict:
    r = _Roku(ctx)
    try:
        mode = await _power(r)
    except ctx.Error:
        return {"ok": True, "already_off": True}
    if mode and mode not in ON_MODES:
        return {"ok": True, "already_off": True}
    await _key(r, "PowerOff")
    return {"ok": True}


async def power_on(ctx, wait: int = 15) -> dict:
    r = _Roku(ctx)
    try:
        if await _power(r) in ON_MODES:
            return {"ok": True, "on": True, "already_on": True}
    except ctx.Error as e:
        raise ctx.Error(f"{e.message}. A Roku TV answers in standby only with Fast TV start on "
                        "(Settings > System > Power)", retry=True) from None
    await _key(r, "PowerOn")
    loop = asyncio.get_running_loop()
    start = loop.time()
    while loop.time() - start < wait:
        await asyncio.sleep(1.0)
        try:
            if await _power(r) in ON_MODES:
                return {"ok": True, "on": True, "waited": round(loop.time() - start, 1)}
        except ctx.Error:
            pass
    if not wait:
        return {"ok": True}
    raise ctx.Error(f"{r.name} did not come on within {wait} s")
