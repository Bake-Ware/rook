"""``show.*``: answer with a short spoken line and the thing itself on a screen.

Bake (2026-10-26): "Short spoken answer plus showing the subject: diagram, web
page, app, picture." ``show.present`` does both in one call: it says ``say``
on a device with ``voice.speak`` and puts the subject on a screen near the
person (``device.open_url`` / ``device.launch`` on a phone, ``tv.open_url`` /
``tv.launch`` on a webOS TV). See docs/design/show.md.

Subjects (exactly one): ``url`` (a web page, opened as is), ``image`` (an
https URL or a ``data:image/...`` URI), ``diagram`` (Mermaid text, or SVG
markup), ``markdown``, ``text``, ``knowledge`` (a knowledge page id or slug,
read as the caller) or ``app`` (an Android package or a TV app id or title).
Everything but ``url`` and ``app`` becomes a **show page**: a small standalone
page the dashboard serves at ``/show/<id>`` (rook/remote/bootstrap.py, from
:func:`web.routes` on the MCP side), so any browser can open it.

Where (``where``): ``auto`` (default) picks a screen as :meth:`Show.pick`
says; ``none`` only makes the page (and returns its link); a worker name puts
it there. A TV is never picked by ``auto`` unless ``show.screens`` lists it.

It degrades instead of failing: with no screen it still speaks and returns the
link (``shown: false``, listed by ``show.list``); with no voice it still shows
and hands the line back (``unsaid``) for the caller to say in its own reply.

Access: the device calls go through the band client's authorizer as whoever
called ``show.present`` (access v2 grants, the trust map and the hard stops
apply to them exactly as to a direct ``voice.speak`` or ``device.open_url``).
Show pages need no sign-in (a TV has none): the link is the credential (192
random bits), pages expire (``show.minutes``, default 60, at most a day),
known vault values are masked out of them, and the page is sandboxed
(render.py). A member's show is theirs: ``show.list`` and ``show.close`` see
only their own; owners see all.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import threading
import time
from typing import Any

from ....core import context
from ....core.plugin import Plugin, capability, place, setting
from . import render

log = logging.getLogger("rook.hub.plugins.show")

SUBJECTS = ("url", "image", "diagram", "markdown", "text", "knowledge", "app")
#: Screen caps, best first, and the launch cap that goes with each.
SCREEN_CAPS = {"device.open_url": "device.launch", "tv.open_url": "tv.launch"}
LAUNCH_ARG = {"device.launch": "package", "tv.launch": "app"}
VOICE_CAP = "voice.speak"
ONLINE_SECS = 65.0
SAY_MAX = 300
TITLE_MAX = 120
TEXT_MAX = 200_000
DATA_MAX = 1_500_000
URL_MAX = 2000
KEEP_MAX = 200
CALL_TIMEOUT = 20.0
ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_DATA_IMAGE = re.compile(r"^data:image/(png|jpeg|gif|webp|svg\+xml);base64,[A-Za-z0-9+/=\s]+$")
_HTTP = re.compile(r"^https?://[^\s]+$", re.I)


def _one_line(text: Any, limit: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


class Show(Plugin):
    NAMESPACE = "show"
    NAME = "show"
    CORE_API = ">=1.0,<2"
    PLACEMENT = place("is_hub", run="one")
    SETTINGS = (
        setting("screens", list, [], label="Screens to show on",
                help="Workers show.present's where=auto may use, best first (a phone, a TV). "
                     "Blank: the owner's phone (home.brief_phone), else the one online phone "
                     "that is not a protected device. A TV is used only when it is listed "
                     "here or named."),
        setting("minutes", int, 60, min=5, max=1440, label="Show pages last (minutes)",
                help="How long a show page's link works. A caller may ask for less or more "
                     "(at most a day)."),
    )
    SKILL = ("### show\n"
             "Answer with a short spoken line plus the thing itself on a screen: "
             "`rook_call(worker=\"rook\", cap=\"show.present\", args={\"say\": \"Here is the "
             "floor plan.\", \"diagram\": \"graph TD; A-->B\"})`. One subject: `url`, `image` "
             "(https or data:image URI), `diagram` (Mermaid or SVG), `markdown`, `text`, "
             "`knowledge` (page slug) or `app` (Android package / TV app); `title` names it. "
             "`where`: `auto` (the person's phone; TVs only when named or in show.screens), a "
             "worker name, or `none` (just make the link). `say` is one or two sentences; "
             "put the detail in the subject. The reply says `shown`, `spoken`, the `url`, and "
             "`unsaid` (say it yourself) when no voice took it. `show.screens` previews where "
             "auto would go; `show.list` / `show.close` your recent shows.\n")
    GUIDANCE = {
        "cap:show.": "show.present = say a short line + show one subject (url, image, diagram, "
                     "markdown, text, knowledge, app); where auto|none|<worker>. Keep say short.",
    }

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self._lock = threading.Lock()
        self._shows: dict[str, dict] | None = None

    def bind_host(self, node: Any) -> None:
        self._node = node

    # -- store ---------------------------------------------------------------
    def _dir(self):
        path = self.data_dir / "pages"
        path.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
        return path

    def _load(self) -> dict[str, dict]:
        if self._shows is None:
            shows: dict[str, dict] = {}
            for f in self._dir().glob("*.json"):
                with contextlib.suppress(OSError, ValueError):
                    rec = json.loads(f.read_text(encoding="utf-8"))
                    if isinstance(rec, dict) and ID_RE.match(str(rec.get("id") or "")):
                        shows[rec["id"]] = rec
            self._shows = shows
        return self._shows

    def _write(self, rec: dict) -> None:
        path = self._dir() / f"{rec['id']}.json"
        tmp = path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(rec, fh)
        os.replace(tmp, path)

    def _drop(self, sid: str) -> None:
        self._load().pop(sid, None)
        with contextlib.suppress(OSError):
            (self._dir() / f"{sid}.json").unlink()

    def _prune(self, now: float) -> None:
        shows = self._load()
        for sid in [k for k, r in shows.items() if float(r.get("expires") or 0) <= now]:
            self._drop(sid)
        extra = len(shows) - KEEP_MAX
        if extra > 0:
            for sid in sorted(shows, key=lambda k: shows[k].get("created") or 0)[:extra]:
                self._drop(sid)

    def page(self, sid: str, now: float | None = None) -> dict | None:
        """The live record for a show page id, or None (unknown or expired)."""
        now = time.time() if now is None else now
        if not ID_RE.match(str(sid or "")):
            return None
        with self._lock:
            rec = self._load().get(sid)
            if rec is None:
                return None
            if float(rec.get("expires") or 0) <= now:
                self._drop(sid)
                return None
            return dict(rec)

    def page_html(self, sid: str, nonce: str) -> str | None:
        rec = self.page(sid)
        if rec is None or rec.get("kind") in ("url", "app"):
            return None
        return render.page_html(rec, nonce)

    # -- who and where -------------------------------------------------------
    @staticmethod
    def _principal() -> Any:
        with contextlib.suppress(Exception):
            from ...authz import current_principal
            return current_principal.get()
        return None

    def _caller(self) -> dict:
        from ... import access
        p = self._principal()
        return {"id": str(getattr(p, "id", "") or context.current_identity() or "system:hub"),
                "user": access.user_of(p) if p is not None else None,
                "restricted": access.restricted(p)}

    def _base_url(self) -> str:
        from ...approvals import dashboard_url
        with contextlib.suppress(Exception):
            return dashboard_url(getattr(self._node, "settings_store", None), os.environ)
        return ""

    def _roster(self) -> list[dict]:
        """Online workers: ``{id, name, caps}``, most recently heard first."""
        client = getattr(self._node, "client", None)
        workers = dict(getattr(client, "workers", {}) or {}) if client is not None else {}
        now = time.time()
        hub = getattr(self._node, "worker_id", None)
        rows = []
        for wid, w in workers.items():
            if wid == hub or now - float(w.get("last_seen") or 0) > ONLINE_SECS:
                continue
            rows.append({"id": wid, "name": str(w.get("name") or wid),
                         "caps": set(w.get("caps") or ()), "seen": float(w.get("last_seen") or 0)})
        return sorted(rows, key=lambda r: -r["seen"])

    def _protected(self) -> set[str]:
        rp = self._node.plugin("role") if self._node is not None else None
        fn = getattr(rp, "protected", None)
        with contextlib.suppress(Exception):
            return {str(x).lower() for x in fn()} if fn is not None else set()
        return set()

    def _owner_phone(self) -> str:
        home = self._node.plugin("home") if self._node is not None else None
        with contextlib.suppress(Exception):
            return str(home.settings.get("brief_phone") or "").strip().lower()
        return ""

    @staticmethod
    def _find(rows: list[dict], name: str) -> dict | None:
        want = str(name or "").strip().lower()
        return next((r for r in rows if want in (r["name"].lower(), r["id"].lower())), None)

    @staticmethod
    def _screen_cap(row: dict | None) -> str:
        return next((c for c in SCREEN_CAPS if row and c in row["caps"]), "")

    def pick(self, where: str = "auto", restricted: bool = False) -> dict:
        """Where a show goes: ``{screen, voice, why}`` (rows or None).

        * a worker name: that worker (it must be online; a screen needs a
          screen cap, the voice falls back to auto's when it has none);
        * ``none``: nowhere;
        * ``auto``: the first online worker in ``show.screens``; else the
          owner's phone (``home.brief_phone``); else the one online phone that
          is not a protected device (none when there are several and nothing
          says which is the person's). TVs only from ``show.screens``. For a
          member (access v2) auto picks nothing: no setting says which screen
          is theirs, so they name one.
        The voice is the screen when it can speak, else the same rule over
        workers with ``voice.speak``."""
        rows = self._roster()
        where = str(where or "auto").strip()
        if where.lower() == "none":
            return {"screen": None, "voice": None, "why": "where=none: nothing is opened or said"}
        if where.lower() != "auto":
            row = self._find(rows, where)
            if row is None:
                return {"screen": None, "voice": None, "why": f"{where} is not online"}
            screen = row if self._screen_cap(row) else None
            voice = row if VOICE_CAP in row["caps"] else (
                None if restricted else self._auto(rows, VOICE_CAP)[0])
            why = f"{row['name']} was named" + ("" if screen else " (it has no screen)")
            return {"screen": screen, "voice": voice, "why": why}
        if restricted:
            return {"screen": None, "voice": None,
                    "why": "no screen is known to be yours: name one with where"}
        screen, why = self._auto(rows, "screen")
        voice = screen if screen and VOICE_CAP in screen["caps"] else self._auto(rows, VOICE_CAP)[0]
        return {"screen": screen, "voice": voice, "why": why}

    def _auto(self, rows: list[dict], need: str) -> tuple[dict | None, str]:
        def fits(r: dict, phones_only: bool) -> bool:
            if need == "screen":
                return bool(self._screen_cap(r)) and (not phones_only or "device.open_url" in r["caps"])
            return need in r["caps"]
        listed = [str(x).strip().lower() for x in (self.settings.get("screens") or []) if str(x).strip()]
        for name in listed:
            row = self._find(rows, name)
            if row is not None and fits(row, False):
                return row, f"{row['name']} is first online in show.screens"
        mine = self._owner_phone()
        row = self._find(rows, mine) if mine else None
        blocked = self._protected()
        if row is not None and fits(row, True) and row["name"].lower() not in blocked:
            return row, f"{row['name']} is the owner's phone (home.brief_phone)"
        cands = [r for r in rows if fits(r, True) and r["name"].lower() not in blocked]
        if len(cands) == 1:
            return cands[0], f"{cands[0]['name']} is the only phone online"
        if not cands:
            return None, "no screen is online"
        return None, ("several phones are online and none is set as the person's "
                      "(show.screens or home.brief_phone); name one with where")

    # -- device calls ----------------------------------------------------------
    async def _call(self, cap: str, args: dict, row: dict) -> dict:
        client = getattr(self._node, "client", None)
        if client is None:
            return {"ok": False, "error": "this hub has no band connection"}
        try:
            reply = await client.call(cap=cap, args=args, target=row["id"], timeout=CALL_TIMEOUT,
                                      identity=context.current_identity() or "system:hub-show")
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"{row['name']} did not answer {cap} in time"}
        except Exception as e:  # noqa: BLE001 - a device failure is a note, not a crash
            return {"ok": False, "error": f"{cap} on {row['name']}: {type(e).__name__}: {e}"[:300]}
        if not isinstance(reply, dict):
            return {"ok": False, "error": f"{cap}: no reply"}
        result = reply.get("result")
        if reply.get("ok") and isinstance(result, dict) and result.get("ok") is False:
            return {"ok": False, "error": str(result.get("error") or f"{cap} failed")[:300]}
        if not reply.get("ok"):
            err = reply.get("error") or ("held for the person" if reply.get("held") else
                                         "denied" if reply.get("denied") else f"{cap} failed")
            return {"ok": False, "error": str(err)[:300], **({"held": True} if reply.get("held")
                                                               else {})}
        return {"ok": True}

    # -- subjects ----------------------------------------------------------------
    async def _subject(self, given: dict) -> dict:
        """``{kind, body, title?}`` from the one subject argument given."""
        named = [k for k in SUBJECTS if str(given.get(k) or "").strip()]
        if len(named) != 1:
            raise ValueError("give exactly one subject: " + ", ".join(SUBJECTS)
                             + (f" (got {', '.join(named)})" if named else ""))
        kind = named[0]
        value = str(given[kind]).strip()
        if kind == "url":
            if len(value) > URL_MAX or not _HTTP.match(value):
                raise ValueError("url: an http(s):// address")
            return {"kind": "url", "body": value}
        if kind == "image":
            if _HTTP.match(value) and value.lower().startswith("https://") and len(value) <= URL_MAX:
                return {"kind": "image", "body": value}
            if len(value) <= DATA_MAX and _DATA_IMAGE.match(value):
                return {"kind": "image", "body": re.sub(r"\s+", "", value)}
            raise ValueError("image: an https:// URL or a data:image/(png|jpeg|gif|webp|svg+xml)"
                             f";base64 URI under {DATA_MAX // 1000} kB")
        if kind == "app":
            if len(value) > 200:
                raise ValueError("app: an Android package name or a TV app id or title")
            return {"kind": "app", "body": value}
        if len(value) > TEXT_MAX:
            raise ValueError(f"{kind}: at most {TEXT_MAX // 1000} kB; show less")
        if kind == "diagram":
            return {"kind": "svg" if value.lstrip().lower().startswith(("<svg", "<?xml")) else
                    "mermaid", "body": value}
        if kind == "knowledge":
            return await self._knowledge(value)
        return {"kind": kind, "body": value}

    async def _knowledge(self, ref: str) -> dict:
        """A knowledge page, read as the caller (their areas apply)."""
        kp = self._node.plugin("knowledge") if self._node is not None else None
        if kp is None or not hasattr(kp, "read"):
            raise LookupError("knowledge is off on this hub")
        got = await kp.read(action="get", id=ref)
        if isinstance(got, str):
            with contextlib.suppress(ValueError):
                got = json.loads(got)
        page = got.get("page", got) if isinstance(got, dict) else {}
        if not isinstance(page, dict) or (got.get("error") if isinstance(got, dict) else None):
            raise LookupError(f"knowledge page {ref!r}: "
                              + str((got or {}).get("error") or "not found")[:200])
        body = str(page.get("body") or "")
        if not body and not page.get("title"):
            raise LookupError(f"knowledge page {ref!r} not found")
        return {"kind": "knowledge", "body": body[:TEXT_MAX],
                "title": _one_line(page.get("title") or ref, TITLE_MAX)}

    # -- caps --------------------------------------------------------------------
    @capability("present", risk="write", tags=("physical",), scope="multi-machine",
                timeout=90)
    async def present(self, say: str = "", url: str = "", image: str = "", diagram: str = "",
                      markdown: str = "", text: str = "", knowledge: str = "", app: str = "",
                      title: str = "", where: str = "auto", minutes: int = 0) -> dict:
        """Say a short line and show one subject on a screen near the person.

        ``say``: one or two spoken sentences (optional, at most 300
        characters; the detail goes in the subject). Exactly one subject:
        ``url`` (opened as is), ``image`` (https URL or data:image URI),
        ``diagram`` (Mermaid text or SVG markup), ``markdown``, ``text``,
        ``knowledge`` (page id or slug) or ``app`` (Android package or TV app).
        ``title`` names the page. ``where``: ``auto`` (show.screens, else the
        owner's phone, else the only phone online; never a TV unless listed),
        a worker name, or ``none`` (only make the link). ``minutes``: how long
        the page lasts (default show.minutes, at most 1440).

        Returns ``{url, kind, title, expires, screen, voice, shown, spoken,
        notes}``; ``unsaid`` is the line when no device spoke it (say it in
        your reply). With no screen the link stays listed by ``show.list``."""
        say = " ".join(str(say or "").split())
        if len(say) > SAY_MAX:
            raise ValueError(f"say is one or two short sentences (at most {SAY_MAX} "
                             "characters); put the detail in the subject")
        subject = await self._subject({"url": url, "image": image, "diagram": diagram,
                                       "markdown": markdown, "text": text,
                                       "knowledge": knowledge, "app": app})
        from ....band_mcp import secret_mask
        now = time.time()
        span = int(minutes or 0) or int(self.settings.get("minutes") or 60)
        span = max(1, min(span, 1440))
        who = self._caller()
        sid = secrets.token_urlsafe(24)
        kind = subject["kind"]
        rec = {"id": sid, "kind": kind,
               "title": _one_line(secret_mask.scrub(str(title or subject.get("title") or "")),
                                  TITLE_MAX),
               "body": secret_mask.scrub(subject["body"]), "say": secret_mask.scrub(say),
               "by": who["id"], "user": who["user"], "created": now,
               "expires": now + span * 60, "shown_on": ""}
        if kind == "url":
            link = rec["body"]
        elif kind == "app":
            link = ""
        else:
            base = self._base_url().rstrip("/")
            link = f"{base}/show/{sid}" if base else f"/show/{sid}"
        rec["url"] = link
        notes: list[str] = []
        place = self.pick(where, restricted=who["restricted"])
        screen, voice = place["screen"], place["voice"]
        out: dict = {"id": sid, "url": link, "kind": kind, "title": rec["title"],
                     "expires": round(rec["expires"]), "screen": "", "voice": "",
                     "shown": False, "spoken": False}
        if kind != "url" and kind != "app" and not link.startswith("http"):
            notes.append("the hub's public address is unknown (core.domain): the link is "
                         "relative, so no device can open it")
            screen = None
        # Show first (it takes a moment to open), then speak over it.
        if screen is not None:
            scap = self._screen_cap(screen)
            if kind == "app":
                lcap = SCREEN_CAPS[scap]
                if lcap not in screen["caps"]:
                    res = {"ok": False, "error": f"{screen['name']} cannot launch apps ({lcap})"}
                else:
                    res = await self._call(lcap, {LAUNCH_ARG[lcap]: rec["body"]}, screen)
            else:
                res = await self._call(scap, {"url": link}, screen)
            if res["ok"]:
                out.update(shown=True, screen=screen["name"])
                rec["shown_on"] = screen["name"]
            else:
                notes.append(f"not shown on {screen['name']}: {res['error']}")
        else:
            notes.append(place["why"])
        if say and voice is not None and where.strip().lower() != "none":
            res = await self._call(VOICE_CAP, {"text": say, "wait": False}, voice)
            if res["ok"]:
                out.update(spoken=True, voice=voice["name"])
            else:
                notes.append(f"not spoken on {voice['name']}: {res['error']}")
        elif say and voice is None and where.strip().lower() != "none":
            notes.append("no device with a voice is online")
        if say and not out["spoken"]:
            out["unsaid"] = say
        if not out["shown"] and kind != "app" and link.startswith("http"):
            notes.append("the link is saved: show.list has it until it expires; give it in "
                         "your reply")
        if kind == "app" and not out["shown"]:
            notes.append("nothing to save for an app: launch it when a screen is online")
        with self._lock:
            self._prune(now)
            if kind != "app" or out["shown"]:
                self._load()[sid] = rec
                self._write(rec)
        out["notes"] = notes
        return out

    @capability("screens", risk="read", scope="self")
    def screens(self, where: str = "auto") -> dict:
        """Where show.present would go now, without showing anything:
        ``{screen, voice, why, online}`` (``online``: workers with a screen
        or a voice)."""
        place = self.pick(where, restricted=self._caller()["restricted"])
        online = [{"worker": r["name"],
                   "screen": self._screen_cap(r) or None,
                   "voice": VOICE_CAP in r["caps"]}
                  for r in self._roster() if self._screen_cap(r) or VOICE_CAP in r["caps"]]
        return {"screen": (place["screen"] or {}).get("name", ""),
                "voice": (place["voice"] or {}).get("name", ""), "why": place["why"],
                "online": online}

    def _mine(self, rec: dict, who: dict) -> bool:
        return not who["restricted"] or (who["user"] and rec.get("user") == who["user"])

    @capability("list", risk="read", scope="self")
    def list(self) -> dict:
        """Recent shows that have not expired, newest first: ``{shows: [{id,
        title, kind, url, created, expires, shown_on, by}]}``. A member sees
        their own; owners see everyone's."""
        who = self._caller()
        now = time.time()
        with self._lock:
            self._prune(now)
            rows = [r for r in self._load().values() if self._mine(r, who)]
        rows.sort(key=lambda r: -(r.get("created") or 0))
        return {"shows": [{k: r.get(k) for k in ("id", "title", "kind", "url", "created",
                                                  "expires", "shown_on", "by")}
                          for r in rows[:50]]}

    @capability("close", risk="write", scope="self")
    def close(self, id: str) -> dict:
        """End a show now: its link stops working. Yours (owners: any)."""
        who = self._caller()
        with self._lock:
            rec = self._load().get(str(id or ""))
            if rec is None:
                return {"id": id, "closed": False, "error": "no such show (it may have expired)"}
            if not self._mine(rec, who):
                raise PermissionError("that show is someone else's")
            self._drop(rec["id"])
        return {"id": id, "closed": True}


PLUGIN = Show
