#!/usr/bin/env python3
"""`rook band` — a full-screen terminal control panel for the worker band.

Self-contained: pure Python stdlib (curses + urllib), so it runs anywhere as a
single file — install it as `rook` and just run `rook`.

Talks to the rook-remote dashboard API (the same source the web dashboard uses),
so it sees the full roster regardless of how each worker is connected. Shows a
live view and lets you run capabilities, enable/disable plugins, define custom
command-caps, message chat-capable workers, and deauth/ban — the terminal
counterpart to the web dashboard. Curses only — no third-party deps.

Connection: ``--url`` (default https://rook.bakeforge.com) + ``--user``/``--pass``
(or env ROOK_WEB_URL / ROOK_WEB_USER / ROOK_WEB_PASS). Password is prompted if
not supplied, so it never lands in shell history.
"""

from __future__ import annotations

import base64
import curses
import getpass
import json
import os
import socket
import time
import urllib.error
import urllib.request

# How this client labels its outgoing chat/notify messages: the machine name.
_ME = socket.gethostname()


# -----------------------------------------------------------------------------
# data layer: the rook-remote dashboard HTTP API
# -----------------------------------------------------------------------------

class BandHTTP:
    """Thin client over rook-remote's /api/band/* endpoints. Synchronous — the
    curses loop just makes blocking requests between frames."""

    def __init__(self, url: str, user: str, password: str) -> None:
        self.url = url.rstrip("/")
        self._auth = base64.b64encode(f"{user}:{password}".encode()).decode()

    def _req(self, path: str, method: str = "GET", body: dict | None = None,
             timeout: float = 8.0) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method)
        req.add_header("Authorization", "Basic " + self._auth)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "rook-band-cli")   # CF blocks default urllib UA
        if data is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"null")

    def check(self) -> str | None:
        """Return an error string if the API is unreachable/unauthorized, else None."""
        try:
            self._req("/api/band/workers", timeout=10)
            return None
        except urllib.error.HTTPError as e:
            return f"HTTP {e.code} ({'auth?' if e.code == 401 else e.reason})"
        except Exception as e:
            return f"{type(e).__name__}: {e}"

    def snapshot(self) -> list[dict]:
        try:
            rows = self._req("/api/band/workers", timeout=8)
        except Exception:
            return []
        if not isinstance(rows, list):
            return []
        for r in rows:
            r["age"] = r.get("last_seen_age_secs", 999)
        rows.sort(key=lambda x: (x.get("name") or "").lower())
        return rows

    def call(self, cap: str, worker_id=None, args=None, timeout: float = 15.0) -> dict:
        try:
            return self._req("/api/band/call", "POST",
                             {"cap": cap, "worker_id": worker_id,
                              "args": args or {}, "timeout": timeout},
                             timeout=timeout + 6)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def ban(self, worker_id: str, name: str, reason: str) -> dict:
        try:
            return self._req("/api/band/ban", "POST",
                             {"worker_id": worker_id, "name": name, "reason": reason},
                             timeout=25)
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read())
            except Exception:
                return {"ok": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def unban(self, worker_id: str, name: str) -> dict:
        try:
            return self._req("/api/band/unban", "POST",
                             {"worker_id": worker_id, "name": name}, timeout=15)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# -----------------------------------------------------------------------------
# curses UI
# -----------------------------------------------------------------------------

DANGER = {"worker.restart", "worker.update", "worker.reconfigure", "worker.deauth",
          "worker.apply"}


class UI:
    def __init__(self, band: "BandHTTP", hub_label: str) -> None:
        self.band = band
        self.hub_label = hub_label
        self.rows: list[dict] = []
        self.sel = 0
        self.top = 0
        self.expanded: set[str] = set()
        self.filter = ""
        self.schema_cache: dict[str, dict] = {}
        self.status = "connected"

    # -- data ----------------------------------------------------------------

    def refresh(self) -> None:
        rows = self.band.snapshot()
        if self.filter:
            f = self.filter.lower()
            rows = [r for r in rows if f in (r.get("name") or "").lower()]
        self.rows = rows
        if self.sel >= len(self.rows):
            self.sel = max(0, len(self.rows) - 1)

    def cur(self) -> dict | None:
        return self.rows[self.sel] if 0 <= self.sel < len(self.rows) else None

    def schema(self, w: dict) -> dict:
        wid = w["worker_id"]
        if wid not in self.schema_cache:
            r = self.band.call("caps.describe", worker_id=wid, timeout=12)
            self.schema_cache[wid] = (r.get("result") or {}) if r.get("ok") else {}
        return self.schema_cache[wid]

    # -- drawing -------------------------------------------------------------

    def draw(self, scr) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        online = sum(1 for r in self.rows if r.get("age", 999) < 90)
        head = f" ROOK BAND · {self.hub_label} · {len(self.rows)} workers · {online} online"
        keys = "[↑↓]sel [enter]expand [c]all [e]plugins [n]ewcap [t]notify [m]chat [x]deauth [/]filter [q]uit "
        scr.attron(curses.A_REVERSE)
        scr.addnstr(0, 0, head.ljust(w), w)
        scr.attroff(curses.A_REVERSE)
        scr.addnstr(h - 1, 0, keys[:w - 1].ljust(w - 1), w - 1, curses.A_DIM)
        if self.filter:
            scr.addnstr(h - 1, 0, f" filter: {self.filter}▏".ljust(w - 1), w - 1)

        # Build a flat display list: worker rows, with cap lines when expanded.
        y = 2
        lines = self._flatten()
        # keep selection visible
        sel_line = next((i for i, ln in enumerate(lines)
                         if ln[0] == "w" and ln[2] == self.sel), 0)
        vis = h - 3
        if sel_line < self.top:
            self.top = sel_line
        elif sel_line >= self.top + vis:
            self.top = sel_line - vis + 1
        for ln in lines[self.top:self.top + vis]:
            self._draw_line(scr, y, w, ln)
            y += 1
        if self.status:
            scr.addnstr(h - 2, 0, (" " + self.status)[:w - 1].ljust(w - 1), w - 1,
                        curses.A_BOLD)
        scr.refresh()

    def _flatten(self):
        out = []
        for i, r in enumerate(self.rows):
            out.append(("w", r, i))
            if r["worker_id"] in self.expanded:
                caps = sorted(r.get("caps") or [])
                groups: dict[str, list[str]] = {}
                for c in caps:
                    p, _, rest = c.partition(".")
                    groups.setdefault(p, []).append(rest or "*")
                for g, subs in groups.items():
                    out.append(("c", f"{g}: " + "  ".join(subs), i))
        return out

    def _draw_line(self, scr, y, w, ln) -> None:
        kind = ln[0]
        if kind == "c":
            scr.addnstr(y, 4, ln[1][:w - 5], w - 5, curses.A_DIM)
            return
        r = ln[1]
        i = ln[2]
        age = r.get("age", 999)
        dot = "●" if age < 90 else "○"
        col = curses.color_pair(1 if age < 20 else 2 if age < 55 else 3)
        name = (r.get("name") or r["worker_id"])[:18]
        ver = ("v" + str(r.get("version")))[:12] if r.get("version") else "—"
        ncap = len(r.get("caps") or [])
        banned = " BANNED" if r.get("banned") else ""
        car = "▾" if r["worker_id"] in self.expanded else "▸"
        bar = self._bar(age)
        line = f" {car} {dot} {name:<18} {ver:<12} {ncap:>3} caps  {bar}{banned}"
        attr = curses.A_REVERSE if i == self.sel else curses.A_NORMAL
        scr.addnstr(y, 0, line[:w - 1].ljust(w - 1), w - 1, attr | (col if i != self.sel else 0))

    @staticmethod
    def _bar(age: float) -> str:
        f = max(0.0, 1.0 - age / 60.0)
        n = int(round(f * 8))
        return "▓" * n + "░" * (8 - n)

    # -- input popups --------------------------------------------------------

    def prompt(self, scr, label: str, default: str = "") -> str | None:
        h, w = scr.getmaxyx()
        curses.echo()
        curses.curs_set(1)
        win = curses.newwin(3, w - 4, h // 2 - 1, 2)
        win.box()
        win.addnstr(0, 2, f" {label} ", w - 8)
        win.addstr(1, 2, default)
        win.refresh()
        try:
            s = win.getstr(1, 2 + len(default), w - 8).decode(errors="replace")
        except Exception:
            s = ""
        curses.noecho()
        curses.curs_set(0)
        val = (default + s).strip()
        return val if val or default else None

    def picker(self, scr, title: str, items: list[str]) -> int | None:
        if not items:
            return None
        h, w = scr.getmaxyx()
        ph, pw = min(len(items) + 2, h - 4), min(max(len(title) + 4,
                    max((len(x) for x in items), default=10) + 4), w - 4)
        win = curses.newwin(ph, pw, 2, 2)
        win.keypad(True)
        sel, top = 0, 0
        while True:
            win.erase()
            win.box()
            win.addnstr(0, 2, f" {title} ", pw - 4, curses.A_BOLD)
            vis = ph - 2
            if sel < top:
                top = sel
            elif sel >= top + vis:
                top = sel - vis + 1
            for idx, it in enumerate(items[top:top + vis]):
                a = curses.A_REVERSE if top + idx == sel else curses.A_NORMAL
                win.addnstr(1 + idx, 1, (" " + it).ljust(pw - 2), pw - 2, a)
            win.refresh()
            k = win.getch()
            if k in (curses.KEY_UP, ord("k")):
                sel = max(0, sel - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                sel = min(len(items) - 1, sel + 1)
            elif k in (curses.KEY_ENTER, 10, 13):
                return sel
            elif k in (27, ord("q")):
                return None

    def popup(self, scr, title: str, text: str) -> None:
        h, w = scr.getmaxyx()
        lines: list[str] = []
        for raw in text.splitlines() or [""]:
            while len(raw) > w - 6:
                lines.append(raw[:w - 6])
                raw = raw[w - 6:]
            lines.append(raw)
        ph = min(len(lines) + 2, h - 4)
        win = curses.newwin(ph, w - 4, 2, 2)
        win.keypad(True)
        top = 0
        while True:
            win.erase()
            win.box()
            win.addnstr(0, 2, f" {title}  (↑↓ scroll · any key close) ", w - 8, curses.A_BOLD)
            for idx, ln in enumerate(lines[top:top + ph - 2]):
                win.addnstr(1 + idx, 2, ln, w - 8)
            win.refresh()
            k = win.getch()
            if k in (curses.KEY_UP, ord("k")):
                top = max(0, top - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                top = min(max(0, len(lines) - (ph - 2)), top + 1)
            else:
                return

    # -- actions -------------------------------------------------------------

    def act_call(self, scr, w: dict) -> None:
        caps = sorted(w.get("caps") or [])
        idx = self.picker(scr, f"run cap on {w.get('name')}", caps)
        if idx is None:
            return
        cap = caps[idx]
        schema = self.schema(w).get(cap, {})
        args: dict = {}
        for p in schema.get("params", []):
            d = "" if p.get("default") is None else str(p.get("default"))
            req = " *" if p.get("required") else ""
            v = self.prompt(scr, f"{cap} · {p['name']}{req} ({p.get('type')})", d)
            if v is None and p.get("required"):
                self.status = "cancelled"
                return
            if v:
                args[p["name"]] = v
        if cap in DANGER:
            if self.picker(scr, f"⚠ {cap} — confirm?", ["no", "yes"]) != 1:
                return
        self.status = f"calling {cap}…"
        self.draw(scr)
        r = self.band.call(cap, worker_id=w["worker_id"], args=args, timeout=40)
        self.popup(scr, f"{cap} @ {w.get('name')}", _fmt(r))
        self.status = "connected"

    def act_plugins(self, scr, w: dict) -> None:
        r = self.band.call("worker.plugin.list", worker_id=w["worker_id"], timeout=15)
        pls = (r.get("result") or {}).get("plugins") if r.get("ok") else None
        if not pls:
            self.popup(scr, "plugins", _fmt(r))
            return
        items = [f"[{'x' if p['loaded'] else ' '}] {p['module']:<16} "
                 f"{len(p['caps'])} caps" for p in pls]
        idx = self.picker(scr, "toggle plugin (enter)", items)
        if idx is None:
            return
        p = pls[idx]
        cap = "worker.plugin.disable" if p["loaded"] else "worker.plugin.enable"
        r = self.band.call(cap, worker_id=w["worker_id"],
                           args={"module": p["module"]}, timeout=20)
        self.schema_cache.pop(w["worker_id"], None)
        self.popup(scr, cap, _fmt(r))

    def act_newcap(self, scr, w: dict) -> None:
        name = self.prompt(scr, "custom cap name (→ cmd.<name>)")
        if not name:
            return
        command = self.prompt(scr, "command template (use {arg} placeholders)")
        if not command:
            return
        argstr = self.prompt(scr, "arg names (space-separated, blank for none)", "") or ""
        args = argstr.split()
        desc = self.prompt(scr, "description (optional)", "") or ""
        r = self.band.call("customcap.add", worker_id=w["worker_id"],
                           args={"name": name, "command": command,
                                 "args": args, "description": desc}, timeout=15)
        self.schema_cache.pop(w["worker_id"], None)
        self.popup(scr, "customcap.add", _fmt(r))

    def act_notify(self, scr, w: dict) -> None:
        """One-way toast: pop a desktop notification on the worker (msg.send)."""
        if "msg.send" not in (w.get("caps") or []):
            self.popup(scr, "notify", "this worker has no msg.send (needs build ≥34)")
            return
        msg = self.prompt(scr, f"notify → {w.get('name')}")
        if not msg:
            return
        r = self.band.call("msg.send", worker_id=w["worker_id"],
                           args={"text": msg, "sender": _ME}, timeout=15)
        res = r.get("result", r)
        if isinstance(res, dict) and res.get("ok"):
            note = "sent ✓" + (" (desktop notification shown)"
                               if res.get("notified") else " (stored in inbox)")
        else:
            note = _fmt(r)
        self.popup(scr, f"notify → {w.get('name')}", str(note))

    def act_chat(self, scr, w: dict) -> None:
        """Open the chat UI: start (or resume) a room with this worker, with a
        sidebar of every chat on the band."""
        if "chat.send" not in (w.get("caps") or []):
            self.popup(scr, "chat", "this worker has no chat.* (needs build ≥37)")
            return
        room = "op" + str(int(time.time()))
        self.status = "opening chat…"
        self.draw(scr)
        self.band.call("chat.open", worker_id=w["worker_id"],
                       args={"room": room, "me": w.get("name")}, timeout=20)
        self._chat_ui(scr, w["worker_id"], w.get("name") or w["worker_id"], room)
        self.status = "connected"

    def _chat_workers(self) -> list:
        return [r for r in self.rows if "chat.rooms" in (r.get("caps") or [])]

    def _chat_sidebar(self, active: tuple) -> list:
        """Every chat on the band: chat.rooms across chat-capable workers."""
        entries = []
        for r in self._chat_workers():
            res = self.band.call("chat.rooms", worker_id=r["worker_id"], timeout=8).get("result", {})
            if not isinstance(res, dict):
                continue
            for room in (res.get("rooms") or []):
                entries.append({"wid": r["worker_id"], "name": r.get("name") or r["worker_id"],
                                "room": room.get("room"), "last_ts": room.get("last_ts", 0) or 0,
                                "last_text": room.get("last_text", "")})
        if not any(e["wid"] == active[0] and e["room"] == active[2] for e in entries):
            entries.append({"wid": active[0], "name": active[1], "room": active[2],
                            "last_ts": 0, "last_text": "(new)"})
        entries.sort(key=lambda e: e["last_ts"], reverse=True)
        return entries

    def _chat_ui(self, scr, awid: str, aname: str, aroom: str) -> None:
        curses.curs_set(1)
        active = (awid, aname, aroom)
        side = self._chat_sidebar(active)
        sidesel = next((i for i, e in enumerate(side)
                        if e["wid"] == active[0] and e["room"] == active[2]), 0)
        msgs: list = []
        since = 0.0
        inp = ""
        last_side = last_poll = 0.0
        scr.timeout(300)
        while True:
            now = time.time()
            if now - last_side > 3.0:
                last_side = now
                side = self._chat_sidebar(active)
                sidesel = next((i for i, e in enumerate(side)
                                if e["wid"] == active[0] and e["room"] == active[2]), sidesel)
            if now - last_poll > 0.8:
                last_poll = now
                res = self.band.call("chat.poll", worker_id=active[0],
                                     args={"room": active[2], "since": since}, timeout=8).get("result", {})
                if isinstance(res, dict):
                    for m in (res.get("messages") or []):
                        msgs.append(m)
                        since = max(since, float(m.get("ts", 0)))
            self._chat_draw(scr, active, side, sidesel, msgs, inp)
            k = scr.getch()
            if k == -1:
                continue
            if k == 27:                                   # esc — leave chat
                return
            elif k in (curses.KEY_UP, curses.KEY_DOWN) and side:
                sidesel = (sidesel + (1 if k == curses.KEY_DOWN else -1)) % len(side)
                e = side[sidesel]
                active = (e["wid"], e["name"], e["room"])
                msgs, since, last_poll = [], 0.0, 0.0     # switch conversation
            elif k in (curses.KEY_ENTER, 10, 13):
                if inp.strip():
                    self.band.call("chat.send", worker_id=active[0],
                                   args={"room": active[2], "text": inp, "sender": _ME}, timeout=8)
                    inp, last_poll = "", 0.0              # echo returns via poll
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                inp = inp[:-1]
            elif 32 <= k <= 126:
                inp += chr(k)

    def _chat_draw(self, scr, active, side, sidesel, msgs, inp) -> None:
        import textwrap
        scr.erase()
        h, w = scr.getmaxyx()
        sw = min(30, max(16, w // 4))
        scr.addnstr(0, 0, (f" ROOK CHAT · {active[1]}/{active[2]} · ↑↓ switch · esc leave ")
                    .ljust(w)[:w], w, curses.A_REVERSE)
        # sidebar
        scr.addnstr(1, 0, " chats on band".ljust(sw)[:sw], sw, curses.A_BOLD)
        for i, e in enumerate(side[:h - 3]):
            sel = i == sidesel
            label = f" {e['name']}/{e['room']}"
            scr.addnstr(2 + i, 0, label.ljust(sw)[:sw], sw,
                        curses.A_REVERSE if sel else curses.A_NORMAL)
        for y in range(1, h - 1):
            try:
                scr.addch(y, sw, curses.ACS_VLINE)
            except curses.error:
                pass
        # conversation
        cx, cw = sw + 2, w - sw - 2
        rows = []
        for m in msgs:
            mine = m.get("sender") == _ME
            who = "you" if mine else str(m.get("sender"))
            wrapped = textwrap.wrap(str(m.get("text", "")), max(8, cw - 13)) or [""]
            for j, seg in enumerate(wrapped):
                head = f"{who + ':':<11}" if j == 0 else " " * 11
                rows.append((head + " " + seg, mine))
        for i, (line, mine) in enumerate(rows[-(h - 4):]):
            scr.addnstr(2 + i, cx, line[:cw], cw,
                        curses.color_pair(1) if mine else curses.A_NORMAL)
        scr.addnstr(h - 1, cx, ("> " + inp).ljust(cw)[:cw], cw, curses.A_BOLD)
        try:
            scr.move(h - 1, min(cx + 2 + len(inp), w - 1))
        except curses.error:
            pass
        scr.refresh()

    def act_deauth(self, scr, w: dict) -> None:
        name = w.get("name") or ""
        if w.get("banned"):
            if self.picker(scr, f"unban {name}?", ["no", "yes"]) != 1:
                return
            r = self.band.unban(w["worker_id"], name)
            self.popup(scr, "unban", _fmt(r) +
                       "\n\nNote: a worker already parked off-band must be revived "
                       "locally (clear ~/.rook-band-worker/banned + restart) to rejoin.")
            return
        if self.picker(scr, f"⚠ deauth / ban {name} — sure?", ["no", "yes"]) != 1:
            return
        reason = self.prompt(scr, "reason (optional)", "") or ""
        self.status = "banning…"
        self.draw(scr)
        r = self.band.ban(w["worker_id"], name, reason)
        self.popup(scr, "deauth / ban", _fmt(r))
        self.status = "connected"

    # -- main loop -----------------------------------------------------------

    def loop(self, scr) -> None:
        curses.curs_set(0)
        scr.nodelay(True)
        scr.timeout(400)
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        last = 0.0
        while True:
            now = time.time()
            if now - last > 0.8:
                self.refresh()
                last = now
            self.draw(scr)
            try:
                k = scr.getch()
            except KeyboardInterrupt:
                return
            if k == -1:
                continue
            w = self.cur()
            if k in (ord("q"),):
                return
            elif k in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                self.sel = min(len(self.rows) - 1, self.sel + 1)
            elif k in (curses.KEY_ENTER, 10, 13, ord(" ")) and w:
                wid = w["worker_id"]
                self.expanded.symmetric_difference_update({wid})
            elif k == ord("r"):
                self.refresh()
            elif k == ord("/"):
                v = self.prompt(scr, "filter name (blank clears)")
                self.filter = v or ""
            elif w and k == ord("c"):
                self.act_call(scr, w)
            elif w and k == ord("e"):
                self.act_plugins(scr, w)
            elif w and k == ord("n"):
                self.act_newcap(scr, w)
            elif w and k == ord("t"):
                self.act_notify(scr, w)
            elif w and k == ord("m"):
                self.act_chat(scr, w)
            elif w and k == ord("x"):
                self.act_deauth(scr, w)


def _fmt(r) -> str:
    import json
    try:
        return json.dumps(r, indent=2)[:8000]
    except Exception:
        return str(r)[:8000]


# -----------------------------------------------------------------------------
# entry
# -----------------------------------------------------------------------------

from pathlib import Path

_CONF = Path(os.path.expanduser("~")) / ".config" / "rook" / "band.conf"


def _load_conf() -> dict:
    d: dict = {}
    try:
        for ln in _CONF.read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception:
        pass
    return d


def _save_conf(url: str, user: str, password: str) -> bool:
    try:
        _CONF.parent.mkdir(parents=True, exist_ok=True)
        _CONF.write_text("# rook band — dashboard connection (this file is chmod 600)\n"
                         f"url={url}\nuser={user}\npass={password}\n")
        os.chmod(_CONF, 0o600)
        return True
    except Exception:
        return False


def main() -> None:
    import argparse
    import sys
    # Installed as a standalone `rook`, so accept both `rook` and `rook band`.
    if len(sys.argv) > 1 and sys.argv[1] in ("band", "tui"):
        del sys.argv[1]
    ap = argparse.ArgumentParser(prog="rook",
                                 description="Terminal control panel for the worker band")
    ap.add_argument("--url", help="dashboard URL (default: saved config or https://rook.bakeforge.com)")
    ap.add_argument("--user", help="dashboard username (default: saved config or 'bake')")
    ap.add_argument("--pass", dest="password", help="dashboard password (default: saved config, else prompt)")
    ap.add_argument("--reset", action="store_true", help="ignore saved config and re-enter connection details")
    args = ap.parse_args()

    conf = {} if args.reset else _load_conf()
    url = args.url or os.environ.get("ROOK_WEB_URL") or conf.get("url") or "https://rook.bakeforge.com"
    user = args.user or os.environ.get("ROOK_WEB_USER") or conf.get("user") or "bake"
    password = args.password or os.environ.get("ROOK_WEB_PASS") or conf.get("pass")
    had_saved = bool(conf.get("pass")) and not args.reset

    if not password:
        try:
            password = getpass.getpass(f"password for {user}@{url}: ")
        except (EOFError, KeyboardInterrupt):
            raise SystemExit("\naborted")

    band = BandHTTP(url, user, password)
    print(f"connecting to {url} …")
    err = band.check()
    if err:
        raise SystemExit(f"cannot reach band API at {url}: {err}\n"
                         "(re-run with --reset to change connection details)")
    if not had_saved and _save_conf(url, user, password):
        print(f"✓ saved connection to {_CONF} — next time just run `rook`")
    label = url.split("://", 1)[-1]
    curses.wrapper(UI(band, label).loop)


if __name__ == "__main__":
    main()
