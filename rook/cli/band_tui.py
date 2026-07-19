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
          "worker.apply", "worker.ota_begin"}


class UI:
    def __init__(self, band: "BandHTTP", hub_label: str) -> None:
        self.band = band
        self.hub_label = hub_label
        self.rows: list[dict] = []
        self.sel = 0
        self.top = 0
        self.filter = ""
        self.schema_cache: dict[str, dict] = {}
        self.status = "connected"
        self.chats: list = []      # aggregated band chats, for the side panel
        # detail-pane (cap tree) focus + navigation
        self.focus = "list"        # "list" or "detail"
        self.cap_sel = 0
        self.cap_top = 0
        self.cap_expanded: set[str] = set()
        self._detail_wid = None    # reset tree state when the selected worker changes

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

    # -- framed (btop-style) rendering --------------------------------------

    def _put(self, scr, y, x, text, attr=0) -> None:
        """Guarded write — clamps to width and never touches the last cell
        (which raises curses.error and would crash the whole TUI)."""
        h, w = scr.getmaxyx()
        n = w - 1 - x
        if y < 0 or y >= h or x < 0 or n <= 0:
            return
        try:
            scr.addnstr(y, x, text[:n], n, attr)
        except curses.error:
            pass

    def _box(self, scr, y, x, height, width, title="") -> None:
        if height < 2 or width < 2:
            return
        c = curses.color_pair(4)     # hermes-cyan border
        self._put(scr, y, x, "╭" + "─" * (width - 2) + "╮", c)
        for i in range(1, height - 1):
            self._put(scr, y + i, x, "│", c)
            self._put(scr, y + i, x + width - 1, "│", c)
        self._put(scr, y + height - 1, x, "╰" + "─" * (width - 2) + "╯", c)
        if title:
            self._put(scr, y, x + 2, f" {title} ", c | curses.A_BOLD)

    def draw(self, scr) -> None:
        scr.erase()
        h, w = scr.getmaxyx()
        if h < 6 or w < 34:
            self._put(scr, 0, 0, "terminal too small", curses.A_BOLD)
            scr.refresh()
            return
        online = sum(1 for r in self.rows if r.get("age", 999) < 90)
        gold, cyan = curses.color_pair(2), curses.color_pair(4)
        # header / brand (hermes flair)
        self._put(scr, 0, 0, " ☤ ROOK BAND ", cyan | curses.A_BOLD | curses.A_REVERSE)
        self._put(scr, 0, 14, f"hermes · {self.hub_label}", gold)
        right = (self.status if self.status and self.status != "connected"
                 else f"{len(self.rows)}w · {online} online")
        self._put(scr, 0, w - len(right) - 1, right, curses.A_BOLD)

        top, box_h = 1, h - 2
        two_col = w >= 76
        left_w = min(max(38, int(w * 0.44)), w - 30) if two_col else w

        # workers list (left)
        self._box(scr, top, 0, box_h, left_w, f"workers ({len(self.rows)})")
        inner_x, inner_w, inner_h = 2, left_w - 4, box_h - 2
        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + inner_h:
            self.top = self.sel - inner_h + 1
        y = top + 1
        for idx in range(self.top, min(self.top + inner_h, len(self.rows))):
            self._draw_line(scr, y, inner_x, inner_w, ("w", self.rows[idx], idx))
            y += 1

        # right column: selected-worker detail (top) + chats (bottom)
        if two_col:
            rx, rw = left_w, w - left_w
            chats_h = max(4, min(box_h // 2, len(self.chats) * 2 + 2)) if self.chats else 0
            self._panel_detail(scr, top, rx, box_h - chats_h, rw, self.cur())
            if chats_h:
                self._panel_chats(scr, top + box_h - chats_h, rx, chats_h, rw)

        keys = ("↑↓ select · → caps · c call · e plugins · n cap · "
                "t notify · m chat · x deauth · / filter · q quit")
        foot = f" filter: {self.filter}▏  {keys}" if self.filter else " " + keys
        self._put(scr, h - 1, 0, foot, curses.A_DIM)
        scr.refresh()

    def _panel_detail(self, scr, y, x, height, width, w) -> None:
        self._box(scr, y, x, height, width, (w.get("name") if w else None) or "no selection")
        if not w or width < 10 or height < 3:
            return
        ix, iw, bottom = x + 2, width - 4, y + height - 1
        ln = y + 1

        def line(txt, attr=0):
            nonlocal ln
            if ln < bottom:
                self._put(scr, ln, ix, txt[:iw], attr)
                ln += 1

        age = w.get("age", 999)
        fresh = curses.color_pair(1 if age < 20 else 2 if age < 55 else 3)
        line(f"{'● online' if age < 90 else '○ stale'} · {age:.0f}s ago", fresh)
        line(f"version  v{w.get('version','?')}", curses.color_pair(2))
        line(f"id       {w.get('worker_id','')}", curses.A_DIM)
        if w.get("banned"):
            line("⛔ DEAUTHED (banned)", curses.color_pair(3))
        line("plugins  " + " ".join(w.get("plugins") or []), curses.A_DIM)
        ln += 1
        # capabilities tree — navigable when this pane is focused
        focused = self.focus == "detail"
        items = self._detail_items(w)
        hint = "↑↓ · → expand/call · ← back" if focused else "→ to browse"
        line(f"{len(w.get('caps') or [])} caps   {hint}", curses.A_BOLD | (curses.color_pair(4) if focused else 0))
        avail = bottom - ln
        if items and avail > 0:
            self.cap_sel = max(0, min(self.cap_sel, len(items) - 1))
            if self.cap_sel < self.cap_top:
                self.cap_top = self.cap_sel
            elif self.cap_sel >= self.cap_top + avail:
                self.cap_top = self.cap_sel - avail + 1
            for idx in range(self.cap_top, min(self.cap_top + avail, len(items))):
                it = items[idx]
                sel = focused and idx == self.cap_sel
                if it[0] == "group":
                    car = "▾" if it[1] in self.cap_expanded else "▸"
                    txt = f"{car} {it[1]} ({len(it[2])})"
                    self._put(scr, ln, ix, txt.ljust(iw) if sel else txt,
                              curses.A_REVERSE if sel else curses.color_pair(4) | curses.A_BOLD)
                else:
                    sub = it[1].split(".", 1)[1] if "." in it[1] else it[1]
                    self._put(scr, ln, ix, (f"    {sub}").ljust(iw) if sel else f"    {sub}",
                              curses.A_REVERSE if sel else curses.A_DIM)
                ln += 1

    def _detail_items(self, w) -> list:
        """Flat tree for the detail pane: ('group', prefix, [full caps]) plus,
        for expanded prefixes, ('func', full_cap, prefix)."""
        groups: dict[str, list[str]] = {}
        for c in sorted(w.get("caps") or []):
            p, _, _r = c.partition(".")
            groups.setdefault(p, []).append(c)
        items: list = []
        for p, caps in groups.items():
            items.append(("group", p, caps))
            if p in self.cap_expanded:
                for full in caps:
                    items.append(("func", full, p))
        return items

    def _detail_keys(self, scr, w, k) -> None:
        """Navigate the cap tree in the focused detail pane."""
        items = self._detail_items(w)
        if not items:
            self.focus = "list"
            return
        self.cap_sel = max(0, min(self.cap_sel, len(items) - 1))
        it = items[self.cap_sel]
        if k == 27:                                    # esc → back to list
            self.focus = "list"
        elif k in (curses.KEY_DOWN, ord("j")):
            self.cap_sel = min(len(items) - 1, self.cap_sel + 1)
        elif k in (curses.KEY_UP, ord("k")):
            self.cap_sel = max(0, self.cap_sel - 1)
        elif k in (curses.KEY_LEFT, ord("h")):
            if it[0] == "func":                        # collapse group, select it
                self.cap_expanded.discard(it[2])
                ni = self._detail_items(w)
                self.cap_sel = next((i for i, x in enumerate(ni)
                                     if x[0] == "group" and x[1] == it[2]), 0)
            elif it[1] in self.cap_expanded:
                self.cap_expanded.discard(it[1])
            else:
                self.focus = "list"                    # already collapsed → exit
        elif k in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13, ord("l")):
            if it[0] == "group":
                self.cap_expanded.add(it[1])
                self.cap_sel += 1                      # step into first function
            else:
                self._run_cap(scr, w, it[1])           # → call the function

    def _panel_chats(self, scr, y, x, height, width) -> None:
        self._box(scr, y, x, height, width, f"chats ({len(self.chats)})")
        cx, cw, bottom = x + 2, width - 4, y + height - 1
        cy = y + 1
        for ch in self.chats:
            if cy >= bottom:
                break
            self._put(scr, cy, cx, f"{ch['name']}/{ch['room']}"[:cw], curses.A_BOLD)
            cy += 1
            if cy < bottom:
                who = ch.get("last_sender") or ""
                prev = f"{who}: {ch.get('last_text','')}" if who else str(ch.get("last_text", ""))
                self._put(scr, cy, cx, "  " + prev[:cw - 2], curses.A_DIM)
                cy += 1

    def _draw_line(self, scr, y, x0, width, ln) -> None:
        r = ln[1]
        i = ln[2]
        age = r.get("age", 999)
        dot = "●" if age < 90 else "○"
        col = curses.color_pair(1 if age < 20 else 2 if age < 55 else 3)
        name = (r.get("name") or r["worker_id"])[:16]
        ver = ("v" + str(r.get("version")))[:11] if r.get("version") else "—"
        ncap = len(r.get("caps") or [])
        banned = "⛔" if r.get("banned") else ""
        car = "›" if i == self.sel else " "
        bar = self._bar(age)
        plain = f"{car} {dot} {name:<16} {ver:<11} {ncap:>3}c  {bar} {banned}"
        if i == self.sel:                     # selected: full-row highlight
            self._put(scr, y, x0, plain.ljust(width), curses.A_REVERSE)
            return
        # unselected: colored segments (dot+bar = freshness, version = gold)
        x = x0
        self._put(scr, y, x, f"{car} ", curses.A_DIM); x += 2
        self._put(scr, y, x, f"{dot} ", col); x += 2
        self._put(scr, y, x, f"{name:<16} ", curses.A_BOLD); x += 17
        self._put(scr, y, x, f"{ver:<11} ", curses.color_pair(2)); x += 12
        self._put(scr, y, x, f"{ncap:>3}c  ", 0); x += 6
        self._put(scr, y, x, bar, col); x += len(bar) + 1
        if banned:
            self._put(scr, y, x, "⛔", curses.color_pair(3))

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
        if idx is not None:
            self._run_cap(scr, w, caps[idx])

    def _run_cap(self, scr, w: dict, cap: str) -> None:
        """Prompt for a cap's args (from its schema), confirm if dangerous, call
        it, and show the reply. Shared by the 'c' picker and the detail tree."""
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

    def _all_chats(self) -> list:
        """Every chat on the band: chat.rooms across chat-capable workers,
        newest-active first. Used by the main-view panel and the chat sidebar."""
        out = []
        for r in self.rows:
            if "chat.rooms" not in (r.get("caps") or []):
                continue
            res = self.band.call("chat.rooms", worker_id=r["worker_id"], timeout=6).get("result", {})
            if not isinstance(res, dict):
                continue
            for room in (res.get("rooms") or []):
                out.append({"wid": r["worker_id"], "name": r.get("name") or r["worker_id"],
                            "room": room.get("room"), "last_ts": room.get("last_ts", 0) or 0,
                            "last_text": room.get("last_text", ""),
                            "last_sender": room.get("last_sender")})
        out.sort(key=lambda e: e["last_ts"], reverse=True)
        return out

    def _chat_sidebar(self, active: tuple) -> list:
        entries = self._all_chats()
        if not any(e["wid"] == active[0] and e["room"] == active[2] for e in entries):
            entries.append({"wid": active[0], "name": active[1], "room": active[2],
                            "last_ts": 0, "last_text": "(new)", "last_sender": None})
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
        if h < 4 or w < 20:
            scr.refresh()
            return

        def put(y, x, text, attr=0):
            # Never touch the last cell of the last row (writing it raises
            # curses.error and would crash the whole TUI); clamp + swallow.
            n = w - 1 - x
            if n <= 0 or y >= h:
                return
            try:
                scr.addnstr(y, x, text[:n], n, attr)
            except curses.error:
                pass

        sw = min(30, max(16, w // 4))
        put(0, 0, f" ROOK CHAT · {active[1]}/{active[2]} · ↑↓ switch · esc leave ".ljust(w),
            curses.A_REVERSE)
        put(1, 0, " chats on band".ljust(sw), curses.A_BOLD)
        for i, e in enumerate(side[:h - 3]):
            put(2 + i, 0, f" {e['name']}/{e['room']}".ljust(sw),
                curses.A_REVERSE if i == sidesel else curses.A_NORMAL)
        for y in range(1, h - 1):
            try:
                scr.addch(y, sw, curses.ACS_VLINE)
            except curses.error:
                pass
        cx, cw = sw + 2, w - sw - 2
        rows = []
        for m in msgs:
            mine = m.get("sender") == _ME
            who = "you" if mine else str(m.get("sender"))
            for j, seg in enumerate(textwrap.wrap(str(m.get("text", "")), max(8, cw - 13)) or [""]):
                head = f"{who + ':':<11}" if j == 0 else " " * 11
                rows.append((head + " " + seg, mine))
        for i, (line, mine) in enumerate(rows[-(h - 4):]):
            put(2 + i, cx, line, curses.color_pair(1) if mine else curses.A_NORMAL)
        put(h - 1, cx, ("> " + inp).ljust(cw), curses.A_BOLD)
        try:
            scr.move(h - 1, min(cx + 2 + len(inp), w - 2))
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
        curses.init_pair(4, curses.COLOR_CYAN, -1)     # hermes-cyan borders/brand
        last = last_chat = 0.0
        while True:
            now = time.time()
            if now - last > 0.8:
                self.refresh()
                last = now
            # refresh the chats panel less often, and only when it's shown
            if now - last_chat > 5.0 and scr.getmaxyx()[1] >= 94:
                self.chats = self._all_chats()
                last_chat = now
            self.draw(scr)
            try:
                k = scr.getch()
            except KeyboardInterrupt:
                return
            if k == -1:
                continue
            if k == ord("q"):
                return
            w = self.cur()
            # reset the cap tree when the selected worker changes
            wid = w["worker_id"] if w else None
            if wid != self._detail_wid:
                self._detail_wid = wid
                self.cap_sel = self.cap_top = 0
                self.cap_expanded = set()
            # detail pane owns the keys while focused
            if self.focus == "detail" and w:
                self._detail_keys(scr, w, k)
                continue
            if k in (curses.KEY_UP, ord("k")):
                self.sel = max(0, self.sel - 1)
            elif k in (curses.KEY_DOWN, ord("j")):
                self.sel = min(len(self.rows) - 1, self.sel + 1)
            elif k in (curses.KEY_RIGHT, ord("l")) and w and (w.get("caps")):
                self.focus = "detail"                  # → move focus to detail pane
                self.cap_sel = self.cap_top = 0
            elif k in (curses.KEY_ENTER, 10, 13) and w:
                self.act_call(scr, w)
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
