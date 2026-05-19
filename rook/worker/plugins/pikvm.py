"""pikvm.* — control a PiKVM via its REST API.

Designed to run on the PiKVM itself so the worker hits ``https://localhost``,
but works just as well over LAN if you point it at another host. Creds and
endpoint come from env:

    PIKVM_URL       default ``https://localhost``
    PIKVM_USER      default ``admin``
    PIKVM_PASS      default ``admin``
    PIKVM_INSECURE  default ``1`` (PiKVM ships with a self-signed cert)
"""

from __future__ import annotations

import asyncio
import base64
import os
import ssl
import urllib.parse
import urllib.request

from ..plugin import Plugin, capability


def _client_cfg() -> tuple[str, str, str, bool]:
    return (
        os.environ.get("PIKVM_URL", "https://localhost").rstrip("/"),
        os.environ.get("PIKVM_USER", "admin"),
        os.environ.get("PIKVM_PASS", "admin"),
        os.environ.get("PIKVM_INSECURE", "1") == "1",
    )


def _ssl_ctx(insecure: bool) -> ssl.SSLContext | None:
    if not insecure:
        return None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _request_sync(method: str, path: str,
                  query: dict | None = None,
                  body: bytes | None = None,
                  body_type: str | None = None) -> dict:
    """Blocking HTTP request. Wrapped via asyncio.to_thread for cap calls."""
    base, user, password, insecure = _client_cfg()
    if not path.startswith("/"):
        path = "/" + path
    url = base + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    req = urllib.request.Request(url, method=method, data=body)
    req.add_header("Authorization", f"Basic {auth}")
    if body_type:
        req.add_header("Content-Type", body_type)
    ctx = _ssl_ctx(insecure)
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            data = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            return {
                "ok": True,
                "status": resp.status,
                "content_type": ctype,
                "body": data,
            }
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "error": e.reason,
                "body": e.read() if e.fp else b""}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


async def _request(method: str, path: str,
                   query: dict | None = None,
                   body: bytes | None = None,
                   body_type: str | None = None) -> dict:
    return await asyncio.to_thread(_request_sync, method, path, query,
                                    body, body_type)


def _decode_body(resp: dict, max_text: int = 64_000) -> dict:
    """Best-effort decode of `body` based on content_type, capped in size."""
    if "body" not in resp:
        return resp
    body = resp["body"]
    ctype = resp.get("content_type", "").lower()
    if "json" in ctype:
        try:
            import json
            resp["json"] = json.loads(body)
            del resp["body"]
            return resp
        except Exception:
            pass
    if ctype.startswith("text/") or "xml" in ctype or "javascript" in ctype:
        try:
            text = body.decode(errors="replace")
            resp["text"] = text[:max_text]
            if len(text) > max_text:
                resp["truncated"] = True
            del resp["body"]
            return resp
        except Exception:
            pass
    # Fall back to base64 for binary (images, etc).
    resp["body_b64"] = base64.b64encode(body).decode()
    resp["body_bytes"] = len(body)
    del resp["body"]
    return resp


class PiKvmPlugin(Plugin):
    NAMESPACE = "pikvm"

    # ---- screencap ------------------------------------------------------

    @capability("snap")
    async def _snap(self, preview: bool = True,
                    quality: int | None = None) -> dict:
        """Grab a still JPEG from the PiKVM streamer.

        Args:
            preview: ``True`` (default) requests the 256x144 thumbnail.
                Set ``False`` for a full-resolution capture — note that on
                ~80KB images the reply spans ~110 UDP fragments, so loss
                tolerance is low. Use full-res only when you actually need
                the pixels.
            quality: optional JPEG quality 1..100. ``None`` lets PiKVM pick.

        Returns ``{ok, status, content_type, body_b64, body_bytes}``. The
        image is base64-encoded — decode on the client side.

        If the streamer reports an error JSON body (e.g. no video source),
        the reply surfaces as ``{ok:false, error, json:{...}}`` instead of
        the bogus base64 wrapper.
        """
        query: dict = {"save": "0", "load": "0"}
        if preview:
            query["preview"] = "1"
        if quality is not None:
            query["quality"] = str(int(quality))
        resp = await _request("GET", "/api/streamer/snapshot", query=query)
        decoded = _decode_body(resp, max_text=0)
        # Catch the "200 OK + JSON error" case and re-shape it as a proper error.
        if decoded.get("content_type", "").startswith("application/json"):
            decoded["ok"] = False
            decoded["error"] = "streamer returned json error (no video?)"
        return decoded

    # ---- HID ------------------------------------------------------------

    @capability("type")
    async def _type(self, text: str, slow: bool = False) -> dict:
        """Type a string. ``slow=True`` adds inter-key delay on the PiKVM side."""
        q = {"limit": "0"}
        if slow:
            q["slow"] = "1"
        resp = await _request("POST", "/api/hid/print", query=q,
                              body=text.encode("utf-8"),
                              body_type="text/plain; charset=utf-8")
        return _decode_body(resp)

    @capability("key")
    async def _key(self, key: str, mods: list[str] | None = None) -> dict:
        """Press+release a key with optional modifier list (each is a key name)."""
        # PiKVM's /api/hid/events/send_key wants {key, state}.
        # For combos we press modifiers first, then the key, then release all.
        steps: list[tuple[str, bool]] = []
        for m in mods or []:
            steps.append((m, True))
        steps.append((key, True))
        steps.append((key, False))
        for m in reversed(mods or []):
            steps.append((m, False))
        results = []
        for k, state in steps:
            r = await _request("POST", "/api/hid/events/send_key",
                                query={"key": k, "state": "true" if state else "false"})
            results.append(_decode_body(r))
            if not r.get("ok"):
                return {"ok": False, "error": "send_key failed",
                        "step": {"key": k, "state": state}, "results": results}
        return {"ok": True, "steps": len(steps)}

    @capability("mouse.move")
    async def _mouse_move(self, x: int, y: int) -> dict:
        """Absolute mouse position. ``x`` and ``y`` are in PiKVM units
        (-32768..32767 mapped across the captured frame)."""
        import json
        body = json.dumps({"to": {"x": int(x), "y": int(y)}}).encode()
        resp = await _request("POST", "/api/hid/events/send_mouse_move",
                              body=body, body_type="application/json")
        return _decode_body(resp)

    @capability("mouse.click")
    async def _mouse_click(self, button: str = "left") -> dict:
        """Press and release a mouse button. button = left|right|middle."""
        import json
        for state in (True, False):
            body = json.dumps({"button": button, "state": state}).encode()
            r = await _request("POST", "/api/hid/events/send_mouse_button",
                                body=body, body_type="application/json")
            if not r.get("ok"):
                return {"ok": False, "error": "mouse button failed",
                        "state": state, "resp": _decode_body(r)}
        return {"ok": True, "button": button}

    # ---- ATX power ------------------------------------------------------

    @capability("power")
    async def _power(self, action: str) -> dict:
        """Trigger ATX power. action in {on, off, off_hard, reset, reset_hard}."""
        valid = {"on", "off", "off_hard", "reset", "reset_hard"}
        if action not in valid:
            return {"ok": False, "error": f"action must be one of {sorted(valid)}"}
        resp = await _request("POST", "/api/atx/power",
                              query={"action": action})
        return _decode_body(resp)

    @capability("power.status")
    async def _power_status(self) -> dict:
        """Read ATX state (powered, online, etc)."""
        resp = await _request("GET", "/api/atx")
        return _decode_body(resp)

    # ---- generic passthrough --------------------------------------------

    @capability("api.get")
    async def _api_get(self, path: str, query: dict | None = None) -> dict:
        """GET any /api/* endpoint on the PiKVM. Decodes JSON / text bodies."""
        resp = await _request("GET", path, query=query)
        return _decode_body(resp)

    @capability("api.post")
    async def _api_post(self, path: str, query: dict | None = None,
                        body: str | None = None,
                        body_b64: str | None = None,
                        body_type: str = "application/json") -> dict:
        """POST to any /api/* endpoint. Body comes from `body` (UTF-8 string) or
        `body_b64` (base64 bytes)."""
        raw: bytes | None = None
        if body is not None:
            raw = body.encode("utf-8")
        elif body_b64 is not None:
            raw = base64.b64decode(body_b64)
        resp = await _request("POST", path, query=query,
                              body=raw, body_type=body_type)
        return _decode_body(resp)


PLUGIN = PiKvmPlugin
