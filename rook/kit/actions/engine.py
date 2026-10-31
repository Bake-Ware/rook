"""The config action engine: runs one cap's ``action:`` block.

``run_action`` takes the block exactly as it appears in plugin.yaml, plus the
call's (already validated) args, and returns the cap's result. It is shared by
plugins and appliances. See castling-manifest, "Built-in actions".
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import os
import platform
import re
import shutil
import signal
import ssl
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .errors import ActionError, Unavailable
from .parse import coerce, parse, pick
from .template import (Missing, Scope, render, render_argv, render_map, render_str,
                       render_text, render_tree, secret_names, text)

MAX_OUTPUT = 64 * 1024 * 1024   # bytes read from a process, response, socket or file
# worker.os values; termux is Python under Termux, android the phone app.
OSES = ("linux", "windows", "macos", "android", "termux")
ACTION_KEYS = ("parse", "returns", "fail_if")

SecretResolver = Callable[[str], Any] | Mapping[str, str]


async def run_action(action: Mapping, args: Mapping | None = None, *,
                     settings: Mapping | None = None, appliance: Mapping | None = None,
                     worker: Mapping | None = None, secrets: SecretResolver | None = None,
                     tmp: str | None = None, plugin_dir: str | None = None,
                     plugin_data: str | None = None, timeout: float = 30.0,
                     cap: Mapping | None = None, launch: Callable | None = None) -> Any:
    """Run one action and return its parsed result.

    ``settings``/``appliance`` values may be ``Secret`` (resolved vault
    values) so errors never show them. ``secrets`` resolves
    ``{{secret:NAME}}``: a mapping, or a function of the name (sync or async)
    returning the value or None. ``cap`` is the cap's spec: its args' ``map``
    and ``join`` shape how args render, and its ``result`` type is what the
    parsed output is coerced to. Raises ``ActionError`` (``Unavailable`` when
    the action can't run on this worker); messages are redacted. ``launch``
    starts an ``exec`` process (``launch(argv, **subprocess kwargs)``): on a
    worker, core's launch path with the call's lease (spec 7.1).
    """
    kinds = [k for k in action if k not in ACTION_KEYS]
    if not kinds:
        raise ActionError("action has no type")
    if len(kinds) > 1:
        raise ActionError(f"action has more than one type: {', '.join(map(str, kinds))}")
    kind = kinds[0]
    runner = RUNNERS.get(kind)
    if runner is None:
        raise Unavailable(f"action type {kind!r} isn't supported by this worker")
    spec = action[kind] or {}
    worker = dict(worker or {})
    worker.setdefault("os", _local_os())
    # A per-OS exec override may bring its own parse and returns.
    over = (spec.get(worker["os"]) or {}) if kind == "exec" else {}
    how, returns = over.get("parse", action.get("parse")), over.get("returns", action.get("returns"))
    arg_specs = (cap or {}).get("args") or {}
    scope = Scope(args=_mapped(args or {}, arg_specs), settings=settings or {}, appliance=appliance,
                  worker=worker, secrets=await _resolve(secret_names(spec), secrets),
                  tmp=tmp, plugin_dir=plugin_dir, plugin_data=plugin_data,
                  joins={n: a["join"] for n, a in arg_specs.items() if "join" in a}, launch=launch)
    try:
        async with asyncio.timeout(timeout):
            data, default, name, mime = await runner(spec, scope)
        if isinstance(data, (bytes, str)):
            _fail_if(data, action.get("fail_if"))
            data = parse(data, how or default, name=name, mime=mime)
        return coerce(pick(data, returns), (cap or {}).get("result"))
    except Missing as e:
        raise ActionError(scope.redact("{{%s}} is not set" % e.ref)) from None
    except Unavailable as e:
        raise Unavailable(scope.redact(str(e))) from None
    except ActionError as e:
        raise ActionError(scope.redact(str(e)), retry=e.retry) from None
    except TimeoutError:
        raise ActionError(f"{kind} timed out after {timeout:g}s", retry=True) from None
    except Exception as e:   # OSError, aiohttp, websockets, ssl: a clean failure, never a crash
        retry = isinstance(e, ConnectionError) or isinstance(getattr(e, "os_error", None), ConnectionError)
        raise ActionError(scope.redact(f"{kind} failed: {type(e).__name__}: {e}"), retry=retry) from None


def _mapped(args: Mapping, specs: Mapping) -> dict:
    """Args through their ``map:`` (caller value -> rendered value; values
    not in the map pass through). A list maps item by item."""
    out = dict(args)
    for name, spec in specs.items():
        m = spec.get("map")
        if m and out.get(name) is not None:
            v = out[name]
            out[name] = [m.get(text(i), i) for i in v] if isinstance(v, list) else m.get(text(v), v)
    return out


def _fail_if(data: bytes | str, rule: Mapping | None) -> None:
    """``fail_if: {regex}``: output that matches is an error, shown by its
    matching line (tools that exit 0 and print the failure)."""
    if not rule:
        return
    s = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
    try:
        m = re.search(rule["regex"], s, re.MULTILINE)
    except re.error as e:
        raise ActionError(f"fail_if regex is invalid: {e}") from None
    if m:
        start = s.rfind("\n", 0, m.start()) + 1
        end = s.find("\n", m.end())
        raise ActionError("failed: " + s[start:end if end >= 0 else None].strip()[:500])


def appliance_values(instance: Mapping, settings: Mapping) -> dict:
    """The ``appliance`` mapping for one instance: its host, port and name
    from onboarding, then its instance settings (defaults applied by the
    caller), which win, so a ``port`` setting overrides the discovered port."""
    return {**instance, **{k: v for k, v in settings.items() if v is not None}}


async def _resolve(names: set[str], secrets: SecretResolver | None) -> dict[str, str]:
    out = {}
    for name in sorted(names):
        if secrets is None:
            value = None
        elif isinstance(secrets, Mapping):
            value = secrets.get(name)
        else:
            try:
                value = secrets(name)
                if inspect.isawaitable(value):
                    value = await value
            except Exception as e:
                raise ActionError(f"vault lookup of {name} failed: {type(e).__name__}") from None
        if value is not None:
            out[name] = str(value)
    return out


def _local_os() -> str:
    if "TERMUX_VERSION" in os.environ or "com.termux" in os.environ.get("PREFIX", ""):
        return "termux"
    if "ANDROID_ROOT" in os.environ:
        return "android"
    return {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")


async def _read(stream) -> bytes:
    limit = MAX_OUTPUT
    buf = bytearray()
    while chunk := await stream.read(65536):
        buf += chunk
        if len(buf) > limit:
            raise ActionError(f"output is over {limit} bytes")
    return bytes(buf)


# --- exec ---------------------------------------------------------------------

def _for_os(spec: Mapping, os_name: str) -> dict:
    """The base exec with this OS's override on top: keys it sets replace the
    base's (argv or try replace both), the rest are inherited."""
    base = {k: v for k, v in spec.items() if k not in OSES}
    over = {k: v for k, v in (spec.get(os_name) or {}).items() if k not in ACTION_KEYS}
    if "argv" in over or "try" in over:
        base.pop("argv", None)
        base.pop("try", None)
    base.update(over)
    return base


def _kill_tree(proc) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


async def _exec(spec: Mapping, scope: Scope):
    """Run argv, or each ``try`` step in order until one is installed and
    exits ok: a missing binary or a failing exit falls through to the next."""
    spec = _for_os(spec, scope.worker["os"])
    base = {k: v for k, v in spec.items() if k != "try"}
    missing, failed = [], None
    for cand in [{**base, **t} for t in spec["try"]] if spec.get("try") else [base]:
        argv = render_argv(cand.get("argv") or [], scope)
        if not argv:
            raise ActionError("exec has no argv")
        # Never the vault stand-in's ROOK_SECRET_*: secrets reach a process only by name.
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("ROOK_SECRET_")},
               **render_map(cand.get("env"), scope, "env")}
        binary = shutil.which(argv[0], path=env.get("PATH"))
        if not binary:
            missing.append(argv[0])
            continue
        try:
            return await _run(cand, argv, binary, env, scope)
        except Unavailable:
            raise
        except ActionError as e:
            failed = e
    if failed:
        raise failed
    raise Unavailable(f"not installed on this worker: {', '.join(missing)}")


async def _run(cand: Mapping, argv: list[str], binary: str, env: dict, scope: Scope):
    cwd = render_str(cand["cwd"], scope, "path") if cand.get("cwd") else scope.tmp
    stdin = None if cand.get("stdin") is None else render_text(cand["stdin"], scope).encode()
    ok_exit = cand.get("ok_exit", [0])
    group = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
             else {"start_new_session": True})
    start = scope.launch or (lambda a, **kw: asyncio.create_subprocess_exec(*a, **kw))
    proc = await start(
        [binary, *argv[1:]], cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        stdin=asyncio.subprocess.DEVNULL if stdin is None else asyncio.subprocess.PIPE,
        **group)
    done = False
    try:
        out, err, _ = await asyncio.gather(_read(proc.stdout), _read(proc.stderr),
                                           _feed(proc.stdin, stdin))
        code = await proc.wait()
        done = True
    finally:
        if not done:   # timeout, cancel or too much output: nothing of it may linger
            _kill_tree(proc)
            await proc.wait()
    if code not in ok_exit:
        tail = err.decode("utf-8", "replace").strip()[-2000:]
        raise ActionError(f"{Path(argv[0]).name} exited {code}" + (f": {tail}" if tail else ""))
    if cand.get("output"):
        path = render_str(cand["output"], scope, "path")
        return await asyncio.to_thread(_read_file, path), "bytes", Path(path).name, None
    return out, "text", None, None


async def _feed(pipe, data: bytes | None) -> None:
    if data is None:
        return
    try:
        pipe.write(data)
        await pipe.drain()
        pipe.close()
    except (BrokenPipeError, ConnectionResetError):
        pass


# --- http ---------------------------------------------------------------------

def _verify(conf: Any, scope: Scope) -> bool:
    """``tls: {verify: ...}``: true, false, or one setting's value."""
    v = render(conf.get("verify", True), scope, native=True) if isinstance(conf, Mapping) else True
    if isinstance(v, str) and v.strip().lower() in ("true", "false"):
        v = v.strip().lower() == "true"
    if not isinstance(v, bool):
        raise ActionError(f"tls.verify must be true or false, not {v!r}")
    return v


def _tls(conf: Any, scope: Scope) -> ssl.SSLContext | None:
    """``tls: true`` or ``tls: {verify: false}``; None = no TLS asked for."""
    if not conf:
        return None
    ctx = ssl.create_default_context()
    if not _verify(conf, scope):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _http(spec: Mapping, scope: Scope):
    import aiohttp
    from yarl import URL

    if not spec.get("url"):
        raise ActionError("http has no url")
    url = render(spec["url"], scope, "url")
    method = str(spec.get("method") or "GET").upper()
    headers = render_map(spec.get("headers"), scope, "header")
    kw: dict[str, Any] = {"params": render_map(spec.get("query"), scope, "raw") or None}
    if "json" in spec:
        kw["json"] = render_tree(spec["json"], scope)
    elif "form" in spec:
        kw["data"] = render_map(spec["form"], scope, "raw")
    elif "body" in spec:
        kw["data"] = render_text(spec["body"], scope).encode()
    auth = spec.get("auth") or {}
    if "basic" in auth:
        pair = ":".join(render_str(v, scope, "header") for v in auth["basic"])
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    elif "bearer" in auth:
        headers["Authorization"] = "Bearer " + render_str(auth["bearer"], scope, "header")
    if not _verify(spec.get("tls"), scope):
        kw["ssl"] = False
    ok = spec.get("ok")
    async with aiohttp.ClientSession() as session:
        async with session.request(method, URL(url), headers=headers, **kw) as resp:
            body = await _read(resp.content)
            where = f"{method} {resp.url.host}{resp.url.path}"
            if (resp.status not in ok) if ok else not 200 <= resp.status < 300:
                tail = body.decode("utf-8", "replace").strip()[:500]
                raise ActionError(f"HTTP {resp.status} from {where}" + (f": {tail}" if tail else ""),
                                  retry=resp.status >= 500 or resp.status == 429)
            return body, "json", Path(resp.url.path).name or None, resp.content_type


# --- tcp ----------------------------------------------------------------------

def _message(send: Any, scope: Scope) -> str:
    """A map or list is sent as JSON (safe); a string is sent exactly as
    written, its values unable to add lines."""
    if isinstance(send, (Mapping, list)):
        return text(render_tree(send, scope))
    return render_text(send, scope, "line")


async def _tcp(spec: Mapping, scope: Scope):
    host = render_str(spec.get("host") or "", scope, "line")
    port = int(render(spec.get("port"), scope, native=True) or 0)
    if not host or not port:
        raise ActionError("tcp needs host and port")
    ctx = _tls(spec.get("tls"), scope)
    reader, writer = await asyncio.open_connection(host, port, ssl=ctx, limit=MAX_OUTPUT)
    try:
        if spec.get("send") is not None:
            msg = _message(spec["send"], scope)
            if isinstance(spec["send"], (Mapping, list)):
                msg += "\n"   # one JSON line
            writer.write(msg.encode())
            await writer.drain()
        until = spec.get("until", "newline")
        if until == "newline":
            data = await reader.readline()
        elif until == "close":
            data = await _read(reader)
        elif isinstance(until, Mapping) and "bytes" in until:
            data = await reader.readexactly(int(until["bytes"]))
        else:
            raise ActionError(f"tcp until {until!r}: use newline, close or {{bytes: n}}")
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (OSError, ssl.SSLError):
            pass
    return data, "text", None, None


# --- ws -----------------------------------------------------------------------

async def _ws(spec: Mapping, scope: Scope):
    import websockets

    if not spec.get("url"):
        raise ActionError("ws has no url")
    url = render(spec["url"], scope, "url")
    kw: dict[str, Any] = {"max_size": MAX_OUTPUT, "open_timeout": None}
    headers = render_map(spec.get("headers"), scope, "header")
    if headers:
        params = inspect.signature(websockets.connect).parameters
        kw["additional_headers" if "additional_headers" in params else "extra_headers"] = headers
    if urlsplit(url).scheme == "wss":
        kw["ssl"] = _tls(spec.get("tls") or True, scope)
    async with websockets.connect(url, **kw) as ws:
        if spec.get("send") is not None:
            await ws.send(_message(spec["send"], scope))
        reply = await ws.recv()
    return reply, "text", None, None


# --- file ---------------------------------------------------------------------

def _read_file(path: str) -> bytes:
    with open(path, "rb") as f:
        data = f.read(MAX_OUTPUT + 1)
    if len(data) > MAX_OUTPUT:
        raise ActionError(f"{Path(path).name} is over {MAX_OUTPUT} bytes")
    return data


def _write_file(path: str, content: str) -> dict:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode()
    p.write_bytes(data)
    return {"path": str(p), "bytes": len(data)}


async def _file(spec: Mapping, scope: Scope):
    if spec.get("read"):
        path = render_str(spec["read"], scope, "path")
        return await asyncio.to_thread(_read_file, path), "text", Path(path).name, None
    if spec.get("write"):
        path = render_str(spec["write"], scope, "path")
        content = spec.get("content", "")
        content = render_text(content, scope) if isinstance(content, str) else render_tree(content, scope)
        return await asyncio.to_thread(_write_file, path, text(content)), "text", None, None
    raise ActionError("file needs read or write")


RUNNERS = {"exec": _exec, "http": _http, "tcp": _tcp, "ws": _ws, "file": _file}
