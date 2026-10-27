"""The attention digest: what came in since the last digest, from the
person's phones, their chat rooms and (when set up) their email, folded into
one quiet message.

Sources:

* **Phones**: each phone's quiet feed (``attention.feed``, summaries of the
  notifications the app sorted as urgent, family or normal; never full
  content), else its notification shade (``notify.list``) on an older app.
  Phones: the ``phones`` argument, else ``attention.devices``, else every
  live worker that has one of the two caps. Mail apps' notifications count
  as email.
* **Chats**: Rook chat rooms with activity since the last digest: those
  ``chat_as`` (the dashboard's chat identity, ``user:operator``, unless told
  otherwise) is in, with their unread count, and other rooms whose newest
  message is from a person or a bridge (``telegram:``, ``discord:``,
  ``human:``, ``user:``). Telegram and Discord conversations are bridged into
  Rook rooms, so they are here too.
* **Email** (optional, ``email``): ``{"imap": {"host", "user", "password":
  "{{secret:name}}", "port": 993, "folder": "INBOX"}}`` reads new unseen
  messages' sender and subject over IMAP (read-only, nothing is marked
  read), or ``{"cap": {"worker", "cap", "args"}}`` calls a worker cap that
  returns ``{"messages": [{"from", "subject"}]}``. Rook has no mail
  connector of its own; credentials only ever go in as vault placeholders.

Nothing new: nothing is sent. The log line holds counts only; the phone
alert holds the senders and summaries (it goes to the person's own phone).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import imaplib
import logging
import re
import time
from email.header import decode_header, make_header
from email.parser import BytesHeaderParser
from typing import Any

from .common import band_call, clip, find_worker, plural, roster, worker_name

log = logging.getLogger("rook.hub.plugins.routines.digest")

FIRST_WINDOW = 2 * 3600       # the first digest looks back this far
MAIL_HINTS = ("mail", "gmail", "outlook", "proton", "fastmail", "thunderbird", "k9", "android.gm")
FETCH_LIMIT = 100
#: Senders that are people (or people on a bridge), for rooms the person is not in.
PEOPLE = ("telegram", "discord", "human", "user")
IMAP_TIMEOUT = 20.0


def _ts(v: Any) -> float:
    try:
        t = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return t / 1000.0 if t > 1e12 else t


def is_mail(app: str) -> bool:
    a = (app or "").lower()
    return any(h in a for h in MAIL_HINTS)


class Digest:
    def __init__(self, node: Any, clock=time.time) -> None:
        self.node = node
        self.clock = clock
        self.errors: list[str] = []

    # -- phones ------------------------------------------------------------------
    def phones(self, wanted: list | None) -> list[str]:
        workers = roster(self.node)
        names = list(wanted or [])
        if not names:
            att = None
            with contextlib.suppress(Exception):
                att = self.node.plugin("attention")
            if att is not None:
                with contextlib.suppress(Exception):
                    names = list(att.settings.get("devices") or [])
        if names:
            out = []
            for n in names:
                wid = find_worker(self.node, n)
                if wid is None:
                    self.errors.append(f"phone {n}: not on the band now")
                elif wid not in out:
                    out.append(wid)
            return out
        return [wid for wid, w in workers.items()
                if {"attention.feed", "notify.list"} & set(w.get("caps") or ())]

    async def phone_items(self, wid: str, since: float) -> list[dict]:
        caps = set((roster(self.node).get(wid) or {}).get("caps") or ())
        name = worker_name(self.node, wid)
        try:
            if "attention.feed" in caps:
                got = await band_call(self.node, "attention.feed", {"limit": FETCH_LIMIT}, wid,
                                      sensitive=True, why="attention digest: read the phone's quiet feed")
                return [{"app": clip(e.get("app") or "an app", 40),
                         "text": clip(e.get("summary"), 140), "priority": e.get("priority") or "normal"}
                        for e in got.get("entries") or [] if _ts(e.get("ts")) > since]
            if "notify.list" in caps:
                got = await band_call(self.node, "notify.list", {"limit": FETCH_LIMIT}, wid,
                                      sensitive=True, why="attention digest: read the phone's notifications")
                rows = got.get("notifications") or got.get("items") or got.get("value") or []
                return [{"app": clip(e.get("app") or e.get("package") or "an app", 40),
                         "text": clip(" - ".join(x for x in (e.get("title"), e.get("text")) if x), 140),
                         "priority": "normal"}
                        for e in rows if isinstance(e, dict) and _ts(e.get("ts") or e.get("posted_ms")) > since]
            self.errors.append(f"{name}: has neither attention.feed nor notify.list")
        except RuntimeError as e:
            self.errors.append(f"{name}: {clip(e, 160)}")
        return []

    # -- chats -------------------------------------------------------------------
    def chats(self, chat_as: str, since: float) -> list[dict]:
        chat = getattr(self.node, "chat", None)
        if chat is None or not hasattr(chat, "rooms_for"):
            self.errors.append("chats: no chat store on this hub")
            return []
        now = self.clock()
        got = chat.rooms_for(chat_as, limit=200, include_all=True)
        out = []
        for r in got.get("rooms") or []:
            if now - float(r.get("last_activity_age_secs") or 0) <= since:
                continue
            sender = str(r.get("last_sender") or "")
            kind = sender.split(":", 1)[0]
            if r.get("member", True):
                if not r.get("unread"):
                    continue
                unread = int(r["unread"])
            elif kind in PEOPLE and sender != chat_as:
                unread = 0      # a room they are not in: new messages from a person or a bridge
            else:
                continue
            out.append({"room": clip(r.get("name") or r.get("title") or r.get("room"), 60),
                        "unread": unread, "from": clip(sender, 40),
                        "via": kind if kind in ("telegram", "discord") else ""})
        return out

    # -- email -------------------------------------------------------------------
    async def email(self, spec: dict | None, state: dict) -> list[dict] | None:
        if not spec:
            return None
        if not isinstance(spec, dict):
            self.errors.append("email: an object, {imap: {...}} or {cap: {...}}")
            return None
        try:
            if isinstance(spec.get("imap"), dict):
                rows, last = await asyncio.wait_for(
                    asyncio.to_thread(imap_new, spec["imap"], int(state.get("imap_uid") or 0)),
                    IMAP_TIMEOUT + 5)
                state["imap_uid"] = last
                return rows
            if isinstance(spec.get("cap"), dict):
                c = spec["cap"]
                wid = find_worker(self.node, c.get("worker"))
                if wid is None:
                    raise RuntimeError(f"worker {c.get('worker')!r} is not on the band now")
                got = await band_call(self.node, str(c.get("cap") or ""), dict(c.get("args") or {}), wid,
                                      sensitive=True, why="attention digest: read new email")
                return [{"from": clip(m.get("from"), 60), "subject": clip(m.get("subject"), 100)}
                        for m in (got.get("messages") or []) if isinstance(m, dict)]
            self.errors.append("email: give imap or cap")
        except (RuntimeError, OSError, imaplib.IMAP4.error, asyncio.TimeoutError, ValueError) as e:
            self.errors.append(f"email: {clip(type(e).__name__ + ': ' + str(e), 160)}")
        return None

    # -- the digest --------------------------------------------------------------
    async def collect(self, *, since: float, phones: list | None, chat_as: str,
                      email: dict | None, state: dict) -> dict:
        items: list[dict] = []
        for wid in self.phones(phones):
            items += await self.phone_items(wid, since)
        mail = [i for i in items if is_mail(i["app"])]
        notes = [i for i in items if not is_mail(i["app"])]
        mails = await self.email(email, state)
        return {"notifications": notes, "phone_mail": mail, "email": mails or [],
                "chats": self.chats(chat_as, since)}


def compose(got: dict, *, since_label: str, max_items: int = 8) -> tuple[str, str, int]:
    """``(text, log line, item count)``; an empty text means nothing new."""
    lines: list[str] = []
    notes = got["notifications"]
    if notes:
        by_app = collections.OrderedDict()
        for n in notes:
            by_app.setdefault(n["app"], []).append(n)
        top = sorted(by_app.items(), key=lambda kv: (-any(i["priority"] == "urgent" for i in kv[1]),
                                                     -len(kv[1])))
        parts = []
        for app, rows in top[:max_items]:
            first = next((r["text"] for r in rows if r["text"]), "")
            parts.append(f"{app} {len(rows)}" + (f" ({clip(first, 70)})" if first else ""))
        lines.append("Phone: " + "; ".join(parts) + (f"; {len(top) - max_items} more apps"
                                                    if len(top) > max_items else ""))
    mail = got["email"] + [{"from": m["app"], "subject": m["text"]} for m in got["phone_mail"]]
    if mail:
        shown = [clip(f"{m['from']}: {m['subject']}", 90) for m in mail[:max_items]]
        lines.append(f"Email ({len(mail)}): " + "; ".join(shown)
                     + (f"; {len(mail) - max_items} more" if len(mail) > max_items else ""))
    chats = got["chats"]
    if chats:
        shown = [f"{c['room']} " + (str(c["unread"]) if c["unread"] else f"new from {c['from']}")
                 + (f" via {c['via']}" if c["via"] else "") for c in chats[:max_items]]
        lines.append("Chats: " + "; ".join(shown)
                     + (f"; {len(chats) - max_items} more rooms" if len(chats) > max_items else ""))
    count = len(notes) + len(mail) + sum(c["unread"] or 1 for c in chats)
    if not lines:
        return "", "", 0
    text = f"Since {since_label}:\n" + "\n".join(lines)
    bits = []
    if notes:
        bits.append(plural(len(notes), "phone notification"))
    if mail:
        bits.append(plural(len(mail), "email"))
    if chats:
        bits.append(plural(len(chats), "chat room"))
    return text, "Attention digest: " + ", ".join(bits), count


def _header(raw: Any) -> str:
    try:
        return str(make_header(decode_header(str(raw or ""))))
    except Exception:  # noqa: BLE001
        return str(raw or "")


def imap_new(cfg: dict, last_uid: int) -> tuple[list[dict], int]:
    """New unseen messages (sender, subject) after ``last_uid``, read-only.
    The first call (``last_uid`` 0) only records where the mailbox is."""
    host = str(cfg.get("host") or "").strip()
    user = str(cfg.get("user") or "").strip()
    password = str(cfg.get("password") or "")
    if not host or not user or not password:
        raise ValueError("imap needs host, user and password ({{secret:name}})")
    if "{{secret:" in password:
        raise ValueError("the password placeholder was not resolved: run it from a job, or pass "
                         "{{secret:name}} through rook_call")
    port = int(cfg.get("port") or 993)
    folder = str(cfg.get("folder") or "INBOX")
    conn = imaplib.IMAP4_SSL(host, port, timeout=IMAP_TIMEOUT)
    try:
        conn.login(user, password)
        conn.select(_quote(folder), readonly=True)
        typ, data = conn.uid("SEARCH", None, "ALL")
        uids = [int(x) for x in (data[0] or b"").split()] if typ == "OK" and data else []
        top = max(uids) if uids else last_uid
        if not last_uid:
            return [], top
        typ, data = conn.uid("SEARCH", None, "UNSEEN", "UID", f"{last_uid + 1}:*")
        new = [int(x) for x in (data[0] or b"").split()] if typ == "OK" and data else []
        new = [u for u in new if u > last_uid][-FETCH_LIMIT:]
        out = []
        parser = BytesHeaderParser()
        for uid in new:
            typ, msg = conn.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT)])")
            raw = next((part[1] for part in msg or [] if isinstance(part, tuple)), b"")
            h = parser.parsebytes(raw)
            sender = _header(h.get("From"))
            sender = re.sub(r"\s*<[^>]*>", "", sender).strip().strip('"') or sender
            out.append({"from": clip(sender, 60), "subject": clip(_header(h.get("Subject")), 100)})
        return out, max([top] + new)
    finally:
        with contextlib.suppress(Exception):
            conn.logout()


def _quote(folder: str) -> str:
    return folder if re.fullmatch(r"[A-Za-z0-9_./-]+", folder) else '"' + folder.replace('"', r'\"') + '"'
