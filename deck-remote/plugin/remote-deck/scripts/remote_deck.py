"""remote.*: the Steam Deck as a Rook remote (deck-remote/ is the app).

The worker runs this; the app connects to its socket (rookmote.bridge). The
app's key presses arrive as events to emit; the caps forward to the app when
it is open and work on the shared profile file when it is not. The app's
pure modules (rookmote: keys, profiles, router, lan) come from beside this
file (the installer copies them) or, in the repository, from deck-remote/
(this plugin lives in deck-remote/plugin/: it is installed by deck-remote's
install.sh, not distributed by the hub as rook/plugins/ are).
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_here = Path(__file__).resolve().parent
for _p in (_here, _here.parents[2]):              # installed: beside this file; in the repo: deck-remote/
    if (_p / "rookmote" / "profiles.py").is_file():
        sys.path.insert(0, str(_p))
        break

from rookmote import VERSION, bridge, router  # noqa: E402
from rookmote import keys as K  # noqa: E402
from rookmote.profiles import ProfileError, Store, routes  # noqa: E402

from rook.kit.ctx import KitError  # noqa: E402

EVENTS = {"remote.button", "remote.voice", "remote.voice.part"}

_lock = threading.Lock()
_srv: bridge.Server | None = None
_ctx = None
_store: Store | None = None
_app_state: dict = {}
_app_seen = 0.0


def _on_msg(msg: dict) -> None:
    """From the app."""
    global _app_state, _app_seen
    t = msg.get("t")
    if t == "event" and msg.get("event") in EVENTS and isinstance(msg.get("payload"), dict):
        ctx = _ctx
        payload = msg["payload"]
        if msg["event"] == "remote.voice" and not payload.get("worker"):
            payload = {**payload, "worker": worker_name()}      # who the hub's remote-voice answers (remote.chat)
        if ctx is not None:
            try:
                ctx.emit(msg["event"], payload)
            except Exception:           # a bad payload is the app's mistake: it must not stop the plugin
                pass
    elif t in ("state", "hello"):
        with _lock:
            if t == "state" and isinstance(msg.get("state"), dict):
                _app_state = msg["state"]
            _app_seen = time.time()
        if _srv is not None:
            _srv.send({"t": "hub", "ok": True})
    elif t == "gone":
        with _lock:
            _app_state = {}


_name: str | None = None


def worker_name() -> str:
    """This worker's name, as the hub knows it: worker.yaml's name (the worker runs
    with --config <it>; enrolling writes the name there), else the host's name as
    enrolling would have picked it."""
    global _name
    if _name:
        return _name
    name = None
    try:
        argv = sys.argv
        cfg = Path(argv[argv.index("--config") + 1])
        for line in cfg.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].split("#", 1)[0].strip().strip("'\"") or None
                break
    except (ValueError, IndexError, OSError):
        pass
    if not name:
        import socket
        name = socket.gethostname().split(".")[0]
    _name = name
    return name


def start(ctx):
    global _srv, _ctx, _store
    _ctx = ctx
    _store = Store()
    _srv = bridge.Server(_on_msg)


def stop(ctx):
    global _srv
    if _srv is not None:
        _srv.close()
        _srv = None


def _need_store() -> Store:
    if _store is None:
        raise KitError("the remote-deck plugin has not started")
    _store.reload()
    return _store


def _app_open() -> bool:
    return _srv is not None and _srv.connected


def state(ctx):
    st = _need_store()
    p = st.current()
    with _lock:
        app = dict(_app_state)
    out = {"app": _app_open(), "version": VERSION, "profiles": len(st.profiles)}
    if p is not None:
        out["profile"] = p["name"]
        out["routes"] = [n for b, n in ((1, "ir"), (2, "rook"), (4, "wifi"), (8, "bt")) if routes(p) & b]
    out.update({k: v for k, v in app.items() if k in ("screen", "tab", "last", "game", "chat")})
    return out


def press(ctx, button):
    if not K.valid_key(button):
        raise KitError(f"bad args: {button} is not a key name")
    if _app_open():
        _srv.send({"t": "press", "button": button})
        return {"button": button, "app": True}
    # The app is closed: the picked profile's action, here.
    st = _need_store()
    via, action, p = router.route(st, button)
    if via == "ir":
        raise KitError(f"{button} is an IR key in {p['name']}, and the Deck has no IR LED")
    if via == "lan":
        done = threading.Event()
        err: list = []
        router.lan_send(p, action, button, lambda e: (err.append(e), done.set()))
        done.wait(10)
        if err and err[0]:
            raise KitError(err[0])
    ctx.emit("remote.button", router.payload(button, via, action, p))
    return {"button": K.canonical(button), "via": via, "app": False}


def _tell_app(msg: dict) -> None:
    if _srv is not None:
        _srv.send(msg)


def ir(ctx, action="status", name=None, to=None, link=None, keys=None, merge=True, icon=None, key=None, list=False,
       **_):
    st = _need_store()
    try:
        if action in ("send", "pin"):
            raise KitError("the Steam Deck has no IR LED: IR keys are kept in a profile but cannot be sent from it")
        if action == "profile_save":
            if keys is not None and not isinstance(keys, dict):
                raise KitError("bad args: keys is a map, key name -> action")
            p, created = st.save(name, keys, link=link, set_link=link is not None, merge=merge, icon=icon,
                                 set_icon=icon is not None)
            _tell_app({"t": "profiles"})
            out = {"created": created, "profile": st.listing(p)}
        elif action == "profile_delete":
            out = {"deleted": st.delete(name or "")}
            _tell_app({"t": "profiles"})
        elif action == "profile_rename":
            out = {"profile": st.listing(st.rename(name or "", to or ""))}
            _tell_app({"t": "profiles"})
        elif action == "profile_select":
            if not name:
                raise KitError("bad args: name is needed (profile_release lets the keys go)")
            st.select(name)
            _tell_app({"t": "profiles"})
            out = {"selected": st.selected}
        elif action == "profile_release":
            st.select(None)
            _tell_app({"t": "profiles"})
            out = {"selected": None}
        elif action == "profile_send":
            if not key:
                raise KitError("bad args: key is needed")
            if name:
                p = st.find(name)
                if p is None:
                    raise KitError(f"no profile called {name}")
                was = st.selected
                st.select(p["name"])
                try:
                    return press(ctx, key)
                finally:
                    st.select(was)
            return press(ctx, key)
        else:
            out = {}
    except ProfileError as e:
        raise KitError(str(e)) from None
    if action in ("profiles", "status") or list:
        out["profiles"] = [st.listing(p) for p in st.profiles]
    out["selected"] = st.selected
    out["ir_pin"] = -1                          # no IR on a Deck
    return out


def chat(ctx, text=None, who="agent", status=None, clear=False, **_):
    if not _app_open():
        raise KitError("the Rook remote app is not open on the Deck")
    _srv.send({"t": "chat", "text": text, "who": who, "status": status, "clear": clear})
    return {"shown": True}


def display(ctx, text, seconds=0, **_):
    if not _app_open():
        raise KitError("the Rook remote app is not open on the Deck")
    _srv.send({"t": "display", "text": text, "seconds": seconds})
    return {"screen": text}
