"""``rook <command>`` for the person sitting at a worker machine.

The worker bundle's ``rook`` launcher (rook.worker.desktop) and the package's
``rook`` entry point (rook.__main__) both send these commands here:

    rook status                 this worker, its band and hub, plugins, last update
    rook ask "<question>"       ask the home agent; rook chat for a conversation
    rook tasks [query]          the band's task deck, or a task search
    rook rooms [room]           chat rooms, or recent messages in one
    rook knowledge search <q>   search the knowledge pages; knowledge get <slug>
    rook plugins [list|enable|disable <module>]
    rook logs [-f] [--audit]    this worker's recent log lines (or its call audit)
    rook doctor                 check the setup; says how to fix what is wrong
    rook pair <code>            join a band with a pairing code
    rook login [--token]        save an API token (or dashboard login) for the hub
    rook completion <shell>     bash, zsh, fish or powershell completion

How it reaches things. Local state (enrollment, plugin switches, update
results) is read from the worker's state directory. Live state (band contact,
loaded plugins, log lines) comes from the running worker over its local
control link (rook.worker.control). Hub reads and the home agent go, in order,
through:

1. a saved API token (``rook login --token``, or ROOK_TOKEN): the hub's MCP
   endpoint (rook.worker.mcp_client) runs the call as that token, so it can
   ask the home agent;
2. the saved dashboard login (``rook login``, the file ``rook band`` saves, or
   ROOK_WEB_URL/ROOK_WEB_USER/ROOK_WEB_PASS): reads, as that person; the
   dashboard's call API goes over the band, so the hub's band ceiling applies;
3. the band, through the running worker, as this worker: reads only by
   default (``hub.band_max_risk`` is ``read``).

``--via token|login|band`` picks one.

Stdlib only, ASCII output, no curses: it runs in PowerShell, cmd and any
terminal the worker runs on.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Callable

from ..core.paths import worker_install_dir, worker_state_dir, worker_state_path
from . import control

#: Commands handled here (the launchers route these names to :func:`main`).
COMMANDS = {
    "status":     "This worker: band, hub, plugins and last update",
    "ask":        "Ask the home agent a question",
    "chat":       "Talk with the home agent (type /exit to leave)",
    "tasks":      "The band's task deck, or search tasks",
    "rooms":      "Chat rooms, or the latest messages in one",
    "knowledge":  "Search or read the band's knowledge pages",
    "plugins":    "List, enable or disable this worker's plugins",
    "logs":       "This worker's recent log lines",
    "doctor":     "Check this worker's setup and say how to fix problems",
    "pair":       "Join a band with a pairing code",
    "login":      "Save an API token (--token) or dashboard login for the hub commands",
    "completion": "Print a shell completion script (bash, zsh, fish, powershell)",
}
#: Never switched off from here: the ways back in (as on the dashboard).
PROTECTED = frozenset({"selfupdate", "config", "enrollment", "info"})
SERVICE = "rook-band-worker"
WIN_TASK = "RookBandWorker"
HUB_NODE = "rook"


class CliError(Exception):
    """A plain-language failure: printed as is, exit code 1."""


def _out(text: str = "") -> None:
    print(text, flush=True)


def _err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _ago(secs: float | int | None) -> str:
    if secs is None:
        return "never"
    secs = max(0, int(secs))
    if secs < 90:
        return f"{secs} s ago"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 2 * 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} days ago"


def _span(secs: int) -> str:
    return _ago(secs).replace(" ago", "")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# -- local state ---------------------------------------------------------------------

def enrollment() -> dict:
    from .enroll import load
    try:
        return load()
    except Exception:
        return {}


def active_band(saved: dict) -> dict:
    return next((b for b in saved.get("bands", []) if b.get("id") == saved.get("active_band")), {})


def pushed_config() -> dict:
    return _read_json(worker_state_path("config.json"))


def live_status() -> dict | None:
    """The running worker's own status, or None when it is not running."""
    try:
        reply = control.request("status", wait=5)
    except (control.NotRunning, OSError, ValueError):
        return None
    return reply.get("status") if reply.get("ok") else None


def update_info() -> dict:
    return {"result": _read_json(worker_state_path("update_result.json")),
            "pending": _read_json(worker_state_path("update_state.json")),
            "hold": worker_state_path("hold").exists()}


def cert_expiry(saved: dict) -> float | None:
    device = saved.get("device") or {}
    if not device:
        return None
    try:
        from .enroll import expires_at
        return float(expires_at(device))
    except Exception:
        return None


def worker_name(live: dict | None = None) -> str:
    if live and live.get("name"):
        return str(live["name"])
    return str(pushed_config().get("name") or socket.gethostname())


# -- the hub -----------------------------------------------------------------------

def token_file() -> Path:
    return Path.home() / ".config" / "rook" / "cli-token.json"


def saved_token() -> tuple[str, str] | None:
    """(MCP URL, API token) from ROOK_TOKEN (with ROOK_MCP_URL or ROOK_WEB_URL)
    or the file `rook login --token` saves, or None."""
    data = _read_json(token_file())
    token = os.environ.get("ROOK_TOKEN") or data.get("token")
    url = (os.environ.get("ROOK_MCP_URL") or data.get("mcp_url")
           or _mcp_url(os.environ.get("ROOK_WEB_URL") or data.get("url") or ""))
    return (url, token) if url and token else None


def _mcp_url(dashboard: str) -> str:
    """The hub serves MCP at /mcp on its dashboard address (one process)."""
    return dashboard.rstrip("/") + "/mcp" if dashboard else ""


def saved_login() -> tuple[str, str, str] | None:
    """(url, user, password) from ROOK_WEB_* or the file `rook band` and
    `rook login` save, or None."""
    from rook.cli.band_tui import _load_conf
    conf = _load_conf()
    url = os.environ.get("ROOK_WEB_URL") or conf.get("url")
    password = os.environ.get("ROOK_WEB_PASS") or conf.get("pass")
    user = os.environ.get("ROOK_WEB_USER") or conf.get("user") or "admin"
    return (url, user, password) if url and password else None


BAND_CEILING = "not callable over the band"
NEEDS_TOKEN = ("{what} only answers this through an API token. Create one on the dashboard's "
               "Tokens page, then run:\n  rook login --token\n"
               "  (or an owner can raise the hub setting hub.band_max_risk to 'write').")


def hub_call(cap: str, args: dict, timeout: float = 20.0, via: str = "auto") -> tuple[dict, str]:
    """Run a hub cap; returns (reply, how): through a saved API token (MCP),
    else the saved dashboard login, else this worker's band link. Raises
    CliError with what to do when none of them can."""
    token = saved_token() if via in ("auto", "token") else None
    if via == "token" and token is None:
        raise CliError("No API token saved on this machine. Run: rook login --token")
    if token is not None:
        from .mcp_client import McpError, rook_call
        try:
            return rook_call(token[0], token[1], cap, args, timeout=timeout), "token"
        except McpError as e:
            raise CliError(f"The hub's agent endpoint ({token[0]}): {e}") from None
        except OSError as e:
            raise CliError(f"Can't reach the hub at {token[0]}: {getattr(e, 'reason', e)}") from None
    login = saved_login() if via in ("auto", "login") else None
    if via == "login" and login is None:
        raise CliError("No saved dashboard login on this machine. Run: rook login")
    if login is not None:
        from rook.cli.band_tui import BandHTTP
        reply = BandHTTP(*login).call(cap, HUB_NODE, args, timeout)
        err = str(reply.get("error") or "")
        if reply.get("ok") is False and ("HTTP 401" in err or "Unauthorized" in err):
            raise CliError("The hub refused the saved dashboard login. Run: rook login")
        if reply.get("ok") is False and BAND_CEILING in err:
            raise CliError(NEEDS_TOKEN.format(what="The hub"))
        return reply, "login"
    try:
        reply = control.request("call", wait=timeout + 5, cap=cap, args=args, timeout=timeout)
    except control.NotRunning:
        raise CliError("Can't reach the hub: the worker isn't running here and no login is "
                       "saved.\n  Start the worker (rook doctor says how), or run: rook login "
                       "--token") from None
    if reply.get("ok") is False and BAND_CEILING in str(reply.get("error") or ""):
        raise CliError(NEEDS_TOKEN.format(what="Over the band, the hub"))
    return reply, "band"


def _result(reply: dict) -> Any:
    if reply.get("ok") is False:
        msg = str(reply.get("error") or "the call failed")
        if reply.get("hint"):
            msg += f"\n  {reply['hint']}"
        raise CliError(msg)
    return reply.get("result")


# -- status ------------------------------------------------------------------------

def gather_status() -> dict:
    saved = enrollment()
    band = active_band(saved)
    cfg = pushed_config()
    live = live_status()
    expiry = cert_expiry(saved)
    from ._build_info import BUILD, VERSION
    return {
        "worker": {"name": worker_name(live), "version": VERSION, "build": BUILD,
                   "running": live is not None,
                   "uptime_s": (live or {}).get("uptime_s"),
                   "state_dir": str(worker_state_dir())},
        "band": {"name": band.get("name") or ("(set by band key)" if cfg.get("psk") else None),
                 "hub": band.get("hub") or cfg.get("hub"),
                 "enrolled": bool(saved.get("device")),
                 "server": saved.get("server"),
                 "certificate_expires": expiry,
                 **({"live": live["band"]} if live else {})},
        "plugins": ({"loaded": live.get("plugins", []), "failed": live.get("plugins_failed", [])}
                    if live else {"disabled": sorted(_plugin_state().get("disabled", []))}),
        "update": update_info(),
        "banned": worker_state_path("banned").exists(),
        "hub_link": ("token" if saved_token() else "login" if saved_login() else "band"),
    }


def _update_line(info: dict) -> str:
    pending = info.get("pending") or {}
    result = info.get("result") or {}
    if pending.get("stage") in ("swapped", "verifying"):
        return f"checking build {pending.get('target_build')} (waits for a band round trip)"
    if result:
        when = _ago(time.time() - float(result.get("at") or time.time()))
        r = result.get("result")
        if r == "ok":
            line = f"build {result.get('build')} confirmed healthy {when} ({result.get('check', 'alive')})"
        elif r in ("rolled_back", "inconclusive", "rollback_failed"):
            line = (f"build {result.get('from_build')} was rolled back {when}: "
                    f"{result.get('reason', 'unknown reason')}")
        else:
            line = f"{r} {when}"
    else:
        line = "no update since install"
    if info.get("hold"):
        line += "; updates are paused on this machine (hold file)"
    return line


def cmd_status(args) -> int:
    st = gather_status()
    if args.json:
        _out(json.dumps(st, indent=2, default=str))
        return 0
    w, b, p = st["worker"], st["band"], st["plugins"]
    run = (f"running for {_span(w['uptime_s'] or 0)}" if w["running"] else "not running")
    rows = [("Worker", f"{w['name']}  v{w['version']}  ({run})")]
    if st["banned"]:
        rows.append(("Band", "removed from the band by its owner (this worker stays off)"))
    elif b["name"] or b["hub"]:
        live = b.get("live") or {}
        how = ("WebSocket" if live.get("ws") else "UDP") if live else ""
        link = f"{b['name'] or '?'} via {b['hub'] or '?'}" + (f" ({how})" if how else "")
        rows.append(("Band", link))
        if live:
            if live.get("connected"):
                state = f"connected, last heard {_ago(live.get('last_heard_s'))}"
            elif live.get("last_heard_s") is None:
                state = "not connected yet (nothing heard from the band)"
            else:
                state = f"not connected (last heard {_ago(live.get('last_heard_s'))})"
            rows.append(("", state))
    else:
        rows.append(("Band", "not joined yet (rook pair <code>)"))
    if b.get("server"):
        hub = b["server"]
        if b.get("certificate_expires"):
            left = b["certificate_expires"] - time.time()
            hub += ("  (device certificate expired)" if left <= 0
                    else f"  (device certificate good for {_span(int(left))})")
        rows.append(("Hub", hub))
    if "loaded" in p:
        names = p["loaded"]
        rows.append(("Plugins", f"{len(names)} on: " + ", ".join(names[:12])
                     + (f", +{len(names) - 12} more" if len(names) > 12 else "")))
        for f in p.get("failed", []):
            rows.append(("", f"{f['module']} failed: {f.get('reason', '')}"[:100]))
    else:
        off = p.get("disabled") or []
        rows.append(("Plugins", "switched off here: " + (", ".join(off) if off else "none")
                     + "  (start the worker for the full list)"))
    rows.append(("Update", _update_line(st["update"])))
    rows.append(("Hub link", {
        "token": "your API token (ask, tasks, rooms, knowledge)",
        "login": "your dashboard login for reads; rook login --token to ask the home agent",
        "band": "this worker's band link for reads; rook login --token to ask the home agent",
    }[st["hub_link"]]))
    width = max(len(k) for k, _ in rows)
    for k, v in rows:
        _out(f"  {k:<{width}}  {v}")
    return 0


# -- the home agent ------------------------------------------------------------------

def _ask(question: str, context: str, via: str, timeout: float) -> str:
    tty = sys.stderr.isatty()
    if tty:
        print("(asking the home agent...)", end="\r", file=sys.stderr, flush=True)
    try:
        reply, _ = hub_call("home.ask", {"question": question, "context": context},
                            timeout=timeout, via=via)
    finally:
        if tty:
            print(" " * 28, end="\r", file=sys.stderr, flush=True)
    result = _result(reply)
    if isinstance(result, dict):
        return str(result.get("answer") or "").strip()
    return str(result or "").strip()


def cmd_ask(args) -> int:
    question = " ".join(args.question).strip()
    if not question or question == "-":
        question = sys.stdin.read().strip()
    if not question:
        raise CliError('Ask something: rook ask "what is on the deck today?"')
    _out(_ask(question, args.context or "", args.via, args.timeout))
    return 0


def cmd_chat(args) -> int:
    """A conversation: each question carries the last few turns as context
    (home.ask has no memory of its own between calls)."""
    turns: list[tuple[str, str]] = []
    if sys.stdin.isatty():
        _out("Talking with the home agent. Type /exit (or press Ctrl-D) to leave.")
    while True:
        try:
            line = input("you> " if sys.stdin.isatty() else "")
        except (EOFError, KeyboardInterrupt):
            _out()
            return 0
        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "/quit", "exit", "quit"):
            return 0
        context = "\n".join(f"Person: {q}\nYou: {a}" for q, a in turns[-6:])
        context = ("Earlier in this conversation:\n" + context) if context else ""
        try:
            answer = _ask(line, context, args.via, args.timeout)
        except CliError as e:
            _err(str(e))
            continue
        turns.append((line, answer))
        _out(f"home> {answer}\n")


# -- read views ----------------------------------------------------------------------

STATE_LABEL = {"in_progress": "doing", "blocked": "blocked", "paused": "paused",
               "todo": "to do", "recently_done": "done"}


def cmd_tasks(args) -> int:
    query = " ".join(args.query).strip()
    if query:
        reply, _ = hub_call("task.read", {"action": "search", "query": query,
                                           "data": {"limit": args.limit}}, via=args.via)
        result = _result(reply) or {}
        if args.json:
            _out(json.dumps(result, indent=2))
            return 0
        rows = result.get("results") or []
        if not rows:
            _out("No tasks match.")
        for r in rows:
            _out(f"  [{r.get('state', '?')}] {r.get('title', '')}  ({r.get('slug') or r.get('id')})")
        return 0
    reply, _ = hub_call("task.read", {"action": "deck"}, via=args.via)
    result = _result(reply) or {}
    if args.json:
        _out(json.dumps(result, indent=2))
        return 0
    deck = result.get("deck") or []
    shown = 0
    for group in deck:
        rows = []
        for key, label in STATE_LABEL.items():
            for t in group.get(key) or []:
                rows.append(f"    {label:<8} {t.get('title', '')}  ({t.get('slug') or t.get('id')})")
        if not rows:
            continue
        project = group.get("project") or {}
        _out(project.get("title") or "(no project)")
        for r in rows[:args.limit]:
            _out(r)
        if len(rows) > args.limit:
            _out(f"    ... and {len(rows) - args.limit} more")
        shown += 1
    if not shown:
        _out("Nothing on the deck.")
    return 0


def cmd_rooms(args) -> int:
    if args.room:
        reply, _ = hub_call("chat.read", {"action": "read", "room": args.room, "mark": False,
                                           "limit": args.limit}, via=args.via)
        result = _result(reply) or {}
        if args.json:
            _out(json.dumps(result, indent=2))
            return 0
        msgs = result.get("messages") or []
        if not msgs:
            _out("No messages.")
        for m in msgs[-args.limit:]:
            _out(f"  {m.get('sender', '?')}: {m.get('text', '')}")
        return 0
    reply, how = hub_call("chat.read", {"action": "rooms", "limit": args.limit}, via=args.via)
    result = _result(reply) or {}
    if args.json:
        _out(json.dumps(result, indent=2))
        return 0
    rooms = result.get("rooms") or []
    if not rooms:
        who = "you are" if how == "login" else "this worker is"
        _out(f"No rooms {who} in yet.")
    for r in rooms:
        unread = f"  {r['unread']} new" if r.get("unread") else ""
        last = r.get("last_text") or ""
        _out(f"  {r.get('title') or r.get('room')}{unread}  ({r.get('room')})")
        if last:
            _out(f"      {r.get('last_sender', '?')}: {last[:90]}")
    return 0


def cmd_knowledge(args) -> int:
    if args.action == "get":
        reply, _ = hub_call("knowledge.read", {"action": "get", "id": args.text}, via=args.via)
        result = _result(reply) or {}
        if args.json:
            _out(json.dumps(result, indent=2))
            return 0
        _out(result.get("title") or args.text)
        _out()
        _out(str(result.get("body") or ""))
        return 0
    reply, _ = hub_call("knowledge.read", {"action": "search", "query": args.text,
                                            "data": {"limit": args.limit}}, via=args.via)
    result = _result(reply) or {}
    if args.json:
        _out(json.dumps(result, indent=2))
        return 0
    rows = result.get("results") or []
    if not rows:
        _out("Nothing found.")
    for r in rows:
        _out(f"  {r.get('title', '')}  ({r.get('slug') or r.get('id')})")
        excerpt = " ".join(str(r.get("excerpt") or "").split())
        if excerpt:
            _out(f"      {excerpt[:140]}")
    return 0


# -- plugins -------------------------------------------------------------------------

def _plugin_state() -> dict:
    return _read_json(worker_state_path("plugins.json"))


def _builtin_modules() -> list[str]:
    import pkgutil
    from . import plugins as pkg
    return sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if not m.name.startswith("_"))


def _profile_off() -> set[str]:
    profile = enrollment().get("plugin_profile") or {}
    return set(profile.get("disable") or []) if isinstance(profile, dict) else set()


def cmd_plugins(args) -> int:
    action = args.action or "list"
    running = live_status() is not None
    if action == "list":
        if running:
            reply = control.request("call", cap="worker.plugin.list", args={}, local=True)
            rows = (_result(reply) or {}).get("plugins") or []
        else:
            off = set(_plugin_state().get("disabled") or [])
            rows = [{"module": m, "loaded": None, "disabled": m in off} for m in _builtin_modules()]
        if args.json:
            _out(json.dumps(rows, indent=2))
            return 0
        prof = _profile_off()
        for r in rows:
            mod = r["module"]
            if r.get("loaded"):
                state, why = "on", ""
            elif r.get("disabled"):
                state = "off"
                why = ("switched off by the band's plugin choices at pairing" if mod in prof
                       else "switched off")
            elif r.get("loaded") is None:
                state, why = "-", ""
            else:
                state, why = "unavailable", str(r.get("reason") or "")
            lock = "  (always on)" if mod in PROTECTED else ""
            _out(f"  {mod:<16} {state:<12} {why}{lock}".rstrip())
        if not running:
            _out("\nThe worker isn't running: this shows only what is switched off here.")
        return 0
    module = (args.module or "").strip()
    if not module:
        raise CliError(f"Which plugin? rook plugins {action} <module> (rook plugins list shows them)")
    if action == "disable" and module in PROTECTED:
        raise CliError(f"{module} stays on: the hub uses it to update, configure and reach this "
                       "worker (the dashboard can't switch it off either).")
    if running:
        cap = f"worker.plugin.{action}"
        reply = control.request("call", wait=60, cap=cap, args={"module": module}, local=True)
        result = _result(reply) or {}
        if isinstance(result, dict) and result.get("ok") is False:
            raise CliError(str(result.get("error") or f"could not {action} {module}"))
        _out(f"{module} is {'on' if action == 'enable' else 'off'} now and stays that way "
             "after restarts.")
    else:
        if module not in _builtin_modules():
            raise CliError(f"No plugin called {module!r} (rook plugins list shows them).")
        from . import admin
        disabled, enabled = admin.load_disabled(), admin.load_enabled()
        if action == "disable":
            disabled.add(module); enabled.discard(module)
        else:
            disabled.discard(module)
        admin._save_state(disabled=disabled, enabled=enabled)
        _out(f"{module} will be {'on' if action == 'enable' else 'off'} when the worker starts.")
    _out("A later switch on the dashboard's Plugins page replaces this one.")
    return 0


# -- logs ----------------------------------------------------------------------------

def _service_log_command(lines: int) -> list[str] | None:
    import shutil
    if sys.platform.startswith("linux") and shutil.which("journalctl"):
        return ["journalctl", "--user", "-u", SERVICE, "-n", str(lines), "--no-pager", "-o", "cat"]
    return None


def cmd_logs(args) -> int:
    if args.audit:
        from . import audit
        for e in audit.tail(limit=args.lines):
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(e.get("ts") or 0)))
            ok = "ok" if e.get("ok") else "error"
            _out(f"{when}  {e.get('identity', '?'):<24} {e.get('cap', '?'):<28} {ok}"
                 + (f"  {e['error']}" if e.get("error") else ""))
        return 0
    try:
        reply = control.request("logs", limit=args.lines)
    except control.NotRunning:
        reply = None
    if reply and reply.get("ok"):
        for row in reply.get("lines") or []:
            _out(row["text"])
        last = reply.get("last", 0)
        while args.follow:
            try:
                time.sleep(1.0)
                reply = control.request("logs", since=last, limit=500)
            except KeyboardInterrupt:
                return 0
            except control.NotRunning:
                _err("(the worker stopped)")
                return 1
            for row in reply.get("lines") or []:
                _out(row["text"])
            last = reply.get("last", last)
        return 0
    # Not running: the service manager's log, else the background log file.
    cmd = _service_log_command(args.lines)
    if cmd:
        import subprocess
        if args.follow:
            cmd = cmd + ["-f"]
        try:
            return subprocess.call(cmd)
        except KeyboardInterrupt:
            return 0
    for path in (worker_install_dir() / "worker.log", worker_install_dir() / "install.log"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace").splitlines()
            _out(f"(the worker isn't running; last lines of {path})")
            for line in text[-args.lines:]:
                _out(line)
            return 0
    raise CliError("The worker isn't running and keeps no log file here. Start it, then run "
                   "rook logs again (rook doctor says how).")


# -- doctor --------------------------------------------------------------------------

class Check:
    def __init__(self, name: str, status: str, detail: str, fix: str = "") -> None:
        self.name, self.status, self.detail, self.fix = name, status, detail, fix

    def as_dict(self) -> dict:
        return {"check": self.name, "status": self.status, "detail": self.detail,
                **({"fix": self.fix} if self.fix else {})}


def start_hint() -> str:
    if sys.platform == "win32":
        return f'Start it: Start-ScheduledTask -TaskName {WIN_TASK}  (or sign out and back in)'
    if os.environ.get("TERMUX_VERSION") or "com.termux" in os.environ.get("PREFIX", ""):
        return f"Start it: sv up {SERVICE}"
    if sys.platform.startswith("linux"):
        return f"Start it: systemctl --user restart {SERVICE}"
    return "Start it again by re-running the install command from the dashboard."


def _http_probe(url: str, timeout: float = 6.0) -> tuple[float | None, str | None, float]:
    """GET url; returns (server clock from the Date header or None, error or
    None, seconds it took)."""
    import email.utils
    import urllib.error
    import urllib.request
    began = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": "rook-doctor/1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            date = r.headers.get("Date")
    except urllib.error.HTTPError as e:
        date = e.headers.get("Date") if e.headers else None
        if e.code >= 500:
            return None, f"the hub answered with an error (HTTP {e.code})", time.monotonic() - began
    except urllib.error.URLError as e:
        return None, _net_reason(e.reason, url), time.monotonic() - began
    except (TimeoutError, socket.timeout):
        return None, "no answer within a few seconds", time.monotonic() - began
    except OSError as e:
        return None, _net_reason(e, url), time.monotonic() - began
    server = None
    if date:
        try:
            server = email.utils.parsedate_to_datetime(date).timestamp()
        except (TypeError, ValueError):
            server = None
    return server, None, time.monotonic() - began


def _net_reason(reason: Any, url: str) -> str:
    import ssl
    from urllib.parse import urlsplit
    host = urlsplit(url).hostname or url
    if isinstance(reason, socket.gaierror):
        return f"can't find {host} (check the address and this machine's internet)"
    if isinstance(reason, ConnectionRefusedError):
        return f"{host} refused the connection (is the hub running on that port?)"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return ("the hub's certificate was not accepted (check this machine's clock, "
                "or a proxy that intercepts HTTPS)")
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "no answer within a few seconds (firewall, or the hub is down)"
    return str(reason)


def _tcp_probe(host: str, port: int, timeout: float = 5.0) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return None
    except socket.gaierror:
        return f"can't find {host}"
    except ConnectionRefusedError:
        return f"{host}:{port} refused the connection"
    except OSError as e:
        return f"{host}:{port} did not answer ({type(e).__name__})"


def run_checks(probe: Callable[[str], tuple] = _http_probe,
               tcp: Callable[[str, int], str | None] = _tcp_probe) -> list[Check]:
    checks: list[Check] = []
    from ._build_info import VERSION
    py = sys.version_info
    if py < (3, 11):
        checks.append(Check("Python", "fail", f"Python {py.major}.{py.minor} is too old",
                            "Install Python 3.11 or newer, then re-run the install command."))
    bundle = worker_install_dir() / "band-worker.pyz"
    checks.append(Check("Worker program", "ok",
                        f"v{VERSION}, Python {py.major}.{py.minor}.{py.micro}"
                        + ("" if bundle.is_file() else " (running from source)")))

    saved = enrollment()
    band = active_band(saved)
    cfg = pushed_config()
    if worker_state_path("banned").exists():
        checks.append(Check("Band membership", "fail",
                            "the band's owner removed this worker; it stays off the band",
                            "Ask the owner to restore it on the dashboard, then delete "
                            f"{worker_state_path('banned')} and restart the worker."))
    elif band:
        checks.append(Check("Band membership", "ok", f"joined {band.get('name')} "
                            f"(hub {band.get('hub')})"))
    elif cfg.get("psk") or os.environ.get("ROOK_BAND_PSK"):
        checks.append(Check("Band membership", "ok", "joined with a band key (not paired)"))
    else:
        checks.append(Check("Band membership", "fail", "this machine hasn't joined a band",
                            "Get a pairing code on the dashboard's Install page, then run: "
                            "rook pair <code>"))

    if saved.get("device"):
        exp = cert_expiry(saved)
        if exp is None:
            checks.append(Check("Device key", "warn", "the device certificate can't be read",
                                "Pair again with a fresh code: rook pair <code>"))
        elif exp <= time.time():
            checks.append(Check("Device key", "fail", "the device certificate has expired",
                                "Pair again with a fresh code: rook pair <code>"))
        elif exp - time.time() < 7 * 86400:
            checks.append(Check("Device key", "warn",
                                f"the device certificate expires in {_span(int(exp - time.time()))}",
                                "It renews itself while the worker is online; start the worker."))
        else:
            checks.append(Check("Device key", "ok",
                                f"certificate good for {_span(int(exp - time.time()))}"))
        path = worker_state_path("enrollment.json")
        if os.environ.get("ROOK_ENROLLMENT_FILE"):
            path = Path(os.environ["ROOK_ENROLLMENT_FILE"]).expanduser()
        if os.name == "posix" and path.exists() and path.stat().st_mode & 0o077:
            checks.append(Check("Key file", "warn", f"{path} can be read by other users",
                                f"Run: chmod 600 {path}"))
        if os.environ.get("ROOK_BAND_PSK") or os.environ.get("ROOK_HUB"):
            checks.append(Check("Settings", "warn",
                                "ROOK_BAND_PSK / ROOK_HUB are set but this machine is paired: "
                                "the pairing wins and they are ignored",
                                "Remove them from the environment to avoid confusion."))

    live = live_status()
    if live is None:
        checks.append(Check("Worker running", "fail", "the worker isn't running", start_hint()))
    else:
        checks.append(Check("Worker running", "ok", f"up for {_span(live.get('uptime_s') or 0)}, "
                            f"{len(live.get('plugins') or [])} plugins"))
        for f in live.get("plugins_failed") or []:
            checks.append(Check("Plugin", "warn", f"{f['module']} failed to start: {f.get('reason', '')}",
                                f"rook logs shows why; rook plugins disable {f['module']} turns it off."))

    server = saved.get("server")
    if server:
        clock, error, took = probe(server.rstrip("/") + "/health")
        if error:
            checks.append(Check("Hub reachable", "fail", f"{server}: {error}",
                                "Check that the hub is up and this machine can reach it "
                                "(same network, VPN, firewall)."))
        else:
            checks.append(Check("Hub reachable", "ok", f"{server} answered in {took:.1f} s"))
            if clock is not None:
                skew = time.time() - clock - took / 2
                text = f"{abs(skew):.0f} s {'ahead of' if skew > 0 else 'behind'} the hub"
                fix = ("Turn on automatic time: Windows Settings > Time & language > Sync now"
                       if sys.platform == "win32" else "Turn on automatic time: timedatectl set-ntp true")
                if abs(skew) > 300:
                    checks.append(Check("Clock", "fail", text + " (sign-ins and band calls fail)", fix))
                elif abs(skew) > 60:
                    checks.append(Check("Clock", "warn", text, fix))
                else:
                    checks.append(Check("Clock", "ok", "in step with the hub"))

    hub = band.get("hub") or cfg.get("hub")
    if live is not None:
        b = live.get("band") or {}
        if b.get("connected"):
            extra = "" if b.get("hub_seen") else " (the hub itself hasn't announced yet)"
            checks.append(Check("Band link", "ok", f"connected to {b.get('hub')}, last heard "
                                f"{_ago(b.get('last_heard_s'))}{extra}"))
        else:
            via = b.get("ws_url") or b.get("hub")
            checks.append(Check("Band link", "fail",
                                f"nothing heard from the band through {via}"
                                + ("" if b.get("last_heard_s") is None
                                   else f" since {_ago(b.get('last_heard_s'))}"),
                                "Check the hub's relay is running and its port is open; "
                                "rook logs shows the connection errors."))
    elif hub and ":" in str(hub):
        host, _, port = str(hub).rpartition(":")
        try:
            err = tcp(host.strip("[]"), int(port))
        except ValueError:
            err = f"bad hub address {hub}"
        if err:
            checks.append(Check("Band link", "warn", f"{err} (TCP; a UDP relay can't be tested "
                                "from here)", "Start the worker and run rook doctor again."))
        else:
            checks.append(Check("Band link", "ok", f"{hub} accepts connections"))

    info = update_info()
    res = info["result"]
    if res.get("result") in ("rolled_back", "rollback_failed"):
        checks.append(Check("Last update", "warn", _update_line(info),
                            "Nothing to do unless it repeats; rook logs shows the details."))
    if worker_state_path("config.json.pending").exists():
        checks.append(Check("Settings", "warn", "a settings change from the hub is waiting to be "
                            "confirmed", "It confirms or undoes itself within a few minutes."))
    return checks


def cmd_doctor(args) -> int:
    checks = run_checks()
    bad = [c for c in checks if c.status == "fail"]
    if args.json:
        _out(json.dumps([c.as_dict() for c in checks], indent=2))
        return 1 if bad else 0
    _out(f"Rook doctor: {worker_name(live_status())}")
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}[c.status]
        _out(f"  {mark}  {c.name:<{width}}  {c.detail}")
        if c.fix and c.status != "ok":
            _out(f"        {'':<{width}}  Fix: {c.fix}")
    warn = sum(c.status == "warn" for c in checks)
    if bad:
        _out(f"\n{len(bad)} problem{'s' if len(bad) > 1 else ''} to fix"
             + (f", {warn} to look at." if warn else "."))
    elif warn:
        _out(f"\nNo problems; {warn} thing{'s' if warn > 1 else ''} to look at.")
    else:
        _out("\nAll good.")
    return 1 if bad else 0


# -- pair / login --------------------------------------------------------------------

def cmd_pair(args) -> int:
    server = (args.hub or os.environ.get("ROOK_SERVER") or enrollment().get("server") or "").strip()
    if not server:
        raise CliError("Which hub? Add its address: rook pair <code> --hub http://<hub address>:7005\n"
                       "  (the address you open the dashboard at)")
    if "://" not in server:
        server = "http://" + server
    if args.yes:
        os.environ["ROOK_YES"] = "1"
    from .enroll import enroll, plain_error
    try:
        enroll(server, args.code, name=args.name, band=args.band)
    except (ValueError, OSError) as e:
        raise CliError(f"Could not join the band: {plain_error(e)}") from None
    if live_status() is not None:
        _out("The running worker switches to the new band within a minute.")
    elif not (worker_install_dir() / "band-worker.pyz").is_file():
        _out("Start the worker: rook worker --enrolled --ws")
    else:
        _out(start_hint())
    return 0


def cmd_login(args) -> int:
    """Save how this machine reaches the hub as a person: an API token
    (--token; the agent endpoint, which may ask the home agent) or the
    dashboard password (reads; the same file `rook band` uses)."""
    import getpass
    url = (args.url or os.environ.get("ROOK_WEB_URL") or enrollment().get("server") or "").strip()
    if not url:
        raise CliError("Which hub? rook login --url http://<hub address>:7005")
    if "://" not in url:
        url = "http://" + url
    if args.token:
        token = os.environ.get("ROOK_TOKEN", "").strip()
        if not token:
            if not sys.stdin.isatty():
                raise CliError("No terminal to paste the token in: set ROOK_TOKEN.")
            token = getpass.getpass("API token (from the dashboard's Tokens page): ").strip()
        mcp = args.mcp_url or _mcp_url(url)
        from .mcp_client import McpError, rook_call
        try:
            reply = rook_call(mcp, token, "home.status", {}, timeout=15)
        except (McpError, OSError) as e:
            raise CliError(f"The hub at {mcp} didn't accept that: {getattr(e, 'reason', e)}") from None
        if reply.get("ok") is False and "no live worker" not in str(reply.get("error")):
            raise CliError(f"The hub at {mcp} answered: {reply.get('error')}")
        from ..core.db import atomic_write_text
        try:
            atomic_write_text(token_file(), json.dumps({"url": url, "mcp_url": mcp,
                                                        "token": token}) + "\n", 0o600)
        except OSError as e:
            raise CliError(f"Could not save the token ({e}).") from None
        _out(f"Saved to {token_file()}. rook ask, chat, tasks, rooms and knowledge now use it.")
        return 0
    from rook.cli.band_tui import BandHTTP, _save_conf
    user = args.user or os.environ.get("ROOK_WEB_USER") or "admin"
    password = os.environ.get("ROOK_WEB_PASS")
    if not password:
        if not sys.stdin.isatty():
            raise CliError("No terminal to type the password in: set ROOK_WEB_PASS.")
        password = getpass.getpass(f"Dashboard password for {url}: ")
    error = BandHTTP(url, user, password).check()
    if error:
        raise CliError(f"The dashboard at {url} didn't accept that: {error}")
    if not _save_conf(url, user, password):
        raise CliError("Could not save the login (check your home folder is writable).")
    _out("Saved. rook tasks, rooms and knowledge now read as you (rook band uses it too).\n"
         "To ask the home agent from here, also save an API token: rook login --token")
    return 0


# -- completion ----------------------------------------------------------------------

SUBS = {"plugins": ["list", "enable", "disable"], "knowledge": ["search", "get"],
        "completion": ["bash", "zsh", "fish", "powershell"]}


def completion_script(shell: str) -> str:
    cmds = list(COMMANDS) + ["band", "worker"]
    mods = _builtin_modules()
    words = " ".join(cmds)
    if shell in ("bash", "zsh"):
        cases = "\n".join(f'    {k}) [ "$COMP_CWORD" -eq 2 ] && COMPREPLY=($(compgen -W "{" ".join(v)}" -- "$cur"));;'
                          for k, v in SUBS.items())
        script = f"""# rook shell completion ({shell}). Add to your shell profile:
#   eval "$(rook completion {shell})"
_rook() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  if [ "$COMP_CWORD" -eq 1 ]; then
    COMPREPLY=($(compgen -W "{words}" -- "$cur")); return
  fi
  if [ "${{COMP_WORDS[1]}}" = plugins ] && [ "$COMP_CWORD" -eq 3 ]; then
    COMPREPLY=($(compgen -W "{" ".join(mods)}" -- "$cur")); return
  fi
  case "${{COMP_WORDS[1]}}" in
{cases}
  esac
}}
complete -F _rook rook
"""
        if shell == "zsh":
            script = "autoload -U +X bashcompinit && bashcompinit\n" + script
        return script
    if shell == "fish":
        lines = ["# rook shell completion (fish): rook completion fish > ~/.config/fish/completions/rook.fish",
                 "complete -c rook -f"]
        for k, v in COMMANDS.items():
            lines.append(f"complete -c rook -n __fish_use_subcommand -a {k} -d '{v}'")
        for k, v in SUBS.items():
            lines.append(f"complete -c rook -n '__fish_seen_subcommand_from {k}' -a '{' '.join(v)}'")
        lines.append("complete -c rook -n '__fish_seen_subcommand_from enable disable' "
                     f"-a '{' '.join(mods)}'")
        return "\n".join(lines) + "\n"
    if shell == "powershell":
        subs = "; ".join(f"'{k}' = @({', '.join(repr(x) for x in v)})" for k, v in SUBS.items())
        return f"""# rook shell completion (PowerShell). Add to your $PROFILE:
#   rook completion powershell | Out-String | Invoke-Expression
Register-ArgumentCompleter -Native -CommandName rook, rook.cmd -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $commands = @({', '.join(repr(c) for c in cmds)})
    $subs = @{{ {subs} }}
    $modules = @({', '.join(repr(m) for m in mods)})
    $words = @($commandAst.CommandElements | ForEach-Object {{ $_.ToString() }})
    $n = $words.Count
    if ($wordToComplete) {{ $n = $n - 1 }}
    $candidates = @()
    if ($n -le 1) {{ $candidates = $commands }}
    elseif ($n -eq 2 -and $subs.ContainsKey($words[1])) {{ $candidates = $subs[$words[1]] }}
    elseif ($n -eq 3 -and $words[1] -eq 'plugins') {{ $candidates = $modules }}
    $candidates | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }}
}}
"""
    raise CliError("Shells: bash, zsh, fish, powershell")


def cmd_completion(args) -> int:
    sys.stdout.write(completion_script(args.shell))
    return 0


# -- entry ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="rook", description="Rook on this machine.",
        epilog="ask, chat, tasks, rooms and knowledge use your saved API token "
               "(rook login --token), else your saved dashboard login (rook login), else "
               "this worker's band link; --via picks.")
    sub = ap.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, **kw) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=COMMANDS[name], description=COMMANDS[name] + ".", **kw)

    def via(p: argparse.ArgumentParser, timeout: float | None = None) -> None:
        p.add_argument("--via", choices=["auto", "token", "login", "band"], default="auto",
                       help="how to reach the hub (default: the saved API token, else the "
                            "saved dashboard login, else this worker's band link)")
        if timeout is not None:
            p.add_argument("--timeout", type=float, default=timeout,
                           help=f"seconds to wait for an answer (default {int(timeout)})")

    p = add("status"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_status)
    p = add("ask"); p.add_argument("question", nargs="*", help='the question ("-" reads stdin)')
    p.add_argument("--context", help="extra text the agent should consider")
    via(p, 180.0); p.set_defaults(fn=cmd_ask)
    p = add("chat"); via(p, 180.0); p.set_defaults(fn=cmd_chat)
    p = add("tasks"); p.add_argument("query", nargs="*", help="search instead of the deck")
    p.add_argument("--limit", type=int, default=8); p.add_argument("--json", action="store_true")
    via(p); p.set_defaults(fn=cmd_tasks)
    p = add("rooms"); p.add_argument("room", nargs="?", help="a room id: show its latest messages")
    p.add_argument("--limit", type=int, default=20); p.add_argument("--json", action="store_true")
    via(p); p.set_defaults(fn=cmd_rooms)
    p = add("knowledge"); p.add_argument("action", choices=["search", "get"])
    p.add_argument("text", help="search words, or a page slug/id for get")
    p.add_argument("--limit", type=int, default=5); p.add_argument("--json", action="store_true")
    via(p); p.set_defaults(fn=cmd_knowledge)
    p = add("plugins"); p.add_argument("action", nargs="?", choices=["list", "enable", "disable"])
    p.add_argument("module", nargs="?"); p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_plugins)
    p = add("logs"); p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    p.add_argument("--audit", action="store_true", help="the calls this worker answered, and who made them")
    p.set_defaults(fn=cmd_logs)
    p = add("doctor"); p.add_argument("--json", action="store_true"); p.set_defaults(fn=cmd_doctor)
    p = add("pair"); p.add_argument("code", help="the pairing code from the dashboard's Install page")
    p.add_argument("--hub", help="the hub's address (default: the one this machine joined before; "
                                 "env ROOK_SERVER)")
    p.add_argument("--name", help="this machine's name on the band (default: its hostname)")
    p.add_argument("--band", help="(account sign-in) the band to join")
    p.add_argument("-y", "--yes", action="store_true", help="never prompt")
    p.set_defaults(fn=cmd_pair)
    p = add("login"); p.add_argument("--url", help="the hub's dashboard address "
                                                   "(default: the hub this machine joined)")
    p.add_argument("--token", action="store_true",
                   help="save an API token from the dashboard's Tokens page (env ROOK_TOKEN); "
                        "needed to ask the home agent")
    p.add_argument("--mcp-url", help="with --token: the agent endpoint (default <url>/mcp)")
    p.add_argument("--user", help="dashboard username (default admin)"); p.set_defaults(fn=cmd_login)
    p = add("completion"); p.add_argument("shell", choices=["bash", "zsh", "fish", "powershell"])
    p.set_defaults(fn=cmd_completion)
    return ap


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if not getattr(args, "fn", None):
        build_parser().print_help()
        return 2
    try:
        return int(args.fn(args) or 0)
    except CliError as e:
        _err(str(e))
        return 1
    except KeyboardInterrupt:
        return 130


def help_lines() -> list[str]:
    """`rook --help` lines for these commands (both launchers print them)."""
    return [f"  {k:12s} {v}" for k, v in COMMANDS.items()]
