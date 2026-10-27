"""``routine.*``: the work behind the ready-made jobs (worker ``rook``).

Bake's roadmap (2026-10-26): "Canned jobs: 5-minute security scan (open
sessions, logs); attention aggregation (email, phone notifications, chats);
security camera review (Ring, others)". The jobs plugin ships these as
templates (``rook_jobs action=templates`` lists them, ``install`` creates
one as a normal job you own; nothing is installed by default). Each template
is one ``cap`` step on worker ``rook`` that calls a cap here; the caps also
work on their own (``dry_run`` shows what they would say without saving or
sending anything). docs/design/routines.md is the contract.

* ``routine.security_scan``: open sessions (workers, console rooms, MCP
  identities, dashboard sign-ins) and the logs (denied calls, refused keys,
  new keys and access changes) compared with the last scan; quiet unless
  something changed. Zero config; no band calls, no model.
* ``routine.attention_digest``: what came in since the last digest from the
  phones (their quiet feed), chat rooms (Telegram and Discord included) and
  email when a source is set up, as one message.
* ``routine.camera_review``: a frame per camera, a cheap change test on the
  hub, and a vision model only for changed frames; reports notable events.

Delivery is always quiet (:func:`.common.deliver`): one line in the
attention log and one alert on the phone's digest channel. Never a card or a
question; urgent only for a clearly bad finding (a burst of refused keys; a
camera only with ``may_ring``).

Every cap is for hub owners (a job runs as its creator, rechecked each run,
so a job a member installed fails until an owner owns it).
"""
from __future__ import annotations

import contextlib
import datetime
import logging
import time
from typing import Any

from ....core.plugin import Plugin, capability, place
from ....core.pluginkit import CapError
from . import cameras as cams
from .common import StateFile, caller_id, clip, deliver, plural, state_key
from .digest import FIRST_WINDOW, Digest, compose
from .security import Scan, report_text, summary_line

log = logging.getLogger("rook.hub.plugins.routines")


class Routines(Plugin):
    NAMESPACE = "routine"
    NAME = "routines"
    CORE_API = ">=1.1,<2"
    PLACEMENT = place("is_hub", run="one")
    SKILL = ("### routines (ready-made jobs)\n"
             "`rook_jobs(action=\"templates\")` lists ready-made jobs: `security-scan` (every 5 "
             "minutes, zero config), `attention-digest` (phones, chats, optional email) and "
             "`camera-review` (needs cameras and a vision model provider). `rook_jobs(action="
             "\"install\", id=\"<template>\", data={\"config\": {...}})` creates one as a job you "
             "own (owners only). Each runs a `routine.*` cap on worker `rook`; call one with "
             "`dry_run: true` to see what it would report. They report quietly (the attention log "
             "and the phone's digest channel), only when something changed.\n")

    def __init__(self) -> None:
        super().__init__()
        self._node: Any = None
        self.clock = time.time
        self.http = cams.http_get           # tests swap in a fake snapshot server

    def bind_host(self, node) -> None:
        self._node = node

    # -- helpers -------------------------------------------------------------------
    @staticmethod
    def _owner(what: str) -> None:
        from ...authz import require_hub_admin
        refusal = require_hub_admin(what)
        if refusal:
            raise CapError(refusal)

    def _state(self, key: str, default: str) -> StateFile:
        try:
            return StateFile(self.data_dir, state_key(key, default))
        except ValueError as e:
            raise CapError(str(e)) from None

    def _zone(self):
        tz = ""
        for ns in ("home", "job"):
            with contextlib.suppress(Exception):
                tz = str(self._node.plugin(ns).settings.get("timezone") or "").strip()
            if tz:
                break
        if tz:
            with contextlib.suppress(Exception):
                from zoneinfo import ZoneInfo
                return ZoneInfo(tz)
        return None

    def _when(self, ts: float) -> str:
        d = datetime.datetime.fromtimestamp(ts, self._zone()) if self._zone() else \
            datetime.datetime.fromtimestamp(ts).astimezone()
        same_day = d.date() == datetime.datetime.fromtimestamp(self.clock(), d.tzinfo).date()
        return d.strftime("%-I:%M %p") if same_day else d.strftime("%b %-d %-I:%M %p")

    # -- the security scan ---------------------------------------------------------------
    @capability("security_scan", risk="write", tags=("sensitive",), scope="multi-user", timeout=120)
    async def security_scan(self, urgent_refusals: int = 10, refusal_window_min: int = 10,
                            urgent_cooldown_min: int = 60, dry_run: bool = False,
                            state: str = "") -> dict:
        """Compare what is open on the band and what the logs say with the last scan, and tell the person quietly about what changed (owners only).

        Open: band workers, live console rooms, identities seen over the MCP,
        dashboard sign-ins. Logs: denied calls, MCP calls refused for a bad
        key, unverified calls, new or removed API keys, access changes. The
        first scan is a silent baseline. urgent_refusals: this many refused
        calls within refusal_window_min (10) minutes make it an urgent alert,
        at most once per urgent_cooldown_min (60); 0: never urgent.
        dry_run: report without saving the state or sending anything. state:
        the saved-state name (default security-scan). Returns {changed,
        findings, urgent, delivered, skipped}."""
        self._owner("the security scan")
        sf = self._state(state, "security-scan")
        prev = sf.load()
        now = self.clock()
        scan = Scan(self._node, clock=self.clock)
        snap = scan.collect(float(prev.get("at") or 0), prev.get("journal"))
        findings, nxt = Scan.compare(prev, snap, urgent_refusals=max(0, int(urgent_refusals or 0)),
                                     window_s=60 * max(1, int(refusal_window_min or 10)),
                                     cooldown_s=60 * max(0, int(urgent_cooldown_min or 0)), now=now)
        nxt["at"] = now
        out: dict = {"baseline": not prev.get("at"), "changed": bool(findings), "findings": findings,
                     "urgent": any(f["level"] == "alert" for f in findings), "skipped": scan.skipped}
        if dry_run:
            out["dry_run"] = True
            return out
        sf.save(nxt)
        if findings:
            out["delivered"] = await deliver(
                self._node, source="security-scan", title="Security scan",
                text=report_text(findings), line=summary_line(findings),
                why="it changed since the last scan", urgent=out["urgent"])
        return out

    # -- the attention digest ---------------------------------------------------------------
    @capability("attention_digest", risk="write", tags=("sensitive",), scope="multi-user", timeout=180)
    async def attention_digest(self, phones: list | None = None, chat_as: str = "user:operator",
                               email: dict | None = None, max_items: int = 8,
                               dry_run: bool = False, state: str = "") -> dict:
        """Fold what came in since the last digest (phone notifications, unread chat rooms including Telegram and Discord, email when set up) into one quiet message to the person (owners only).

        phones: phone workers (default attention.devices, else every worker
        with attention.feed or notify.list). chat_as: whose unread rooms
        (default user:operator, the dashboard). email: {imap: {host, user,
        password: "{{secret:name}}", port?, folder?}} or {cap: {worker, cap,
        args?}} returning {messages: [{from, subject}]}; none: email comes
        only from mail apps' phone notifications. Nothing new: nothing is
        sent. dry_run: report without saving or sending. Returns {changed,
        count, counts, text?, delivered, errors}."""
        self._owner("the attention digest")
        if email is not None and not isinstance(email, dict):
            raise CapError("email: {imap: {...}} or {cap: {...}}")
        if phones is not None and not isinstance(phones, list):
            raise CapError("phones: a list of worker names")
        sf = self._state(state, "attention-digest")
        prev = sf.load()
        now = self.clock()
        since = float(prev.get("at") or (now - FIRST_WINDOW))
        nxt = dict(prev)
        d = Digest(self._node, clock=self.clock)
        got = await d.collect(since=since, phones=phones, chat_as=str(chat_as or "user:operator"),
                              email=email, state=nxt)
        text, line, count = compose(got, since_label=self._when(since),
                                    max_items=max(1, min(int(max_items or 8), 20)))
        out: dict = {"changed": bool(text), "count": count, "since": since, "errors": d.errors,
                     "counts": {"notifications": len(got["notifications"]),
                                "email": len(got["email"]) + len(got["phone_mail"]),
                                "chat_rooms": len(got["chats"])}}
        if dry_run:
            out.update(dry_run=True, text=text)
            return out
        nxt["at"] = now
        sf.save(nxt)
        if text:
            out["delivered"] = await deliver(self._node, source="attention-digest",
                                             title="Since " + self._when(since), text=text, line=line,
                                             why="new since the last digest")
        return out

    # -- camera review ------------------------------------------------------------------------
    @capability("camera_review", risk="write", tags=("sensitive",), scope="multi-user", timeout=600)
    async def camera_review(self, cameras: list, provider: str, model: str = "",
                            threshold: float = 0.06, note: str = "", may_ring: bool = False,
                            dry_run: bool = False, state: str = "") -> dict:
        """Grab a frame from each camera, compare it with the last one on the hub, and ask a vision model about changed frames only; tell the person quietly about notable events (owners only).

        cameras: [{name, url (JPEG snapshot, headers?)} | {name, worker, rtsp}
        | {name, worker, camera}]; credentials as {{secret:name}}. Ring: bring a
        bridge (ring-mqtt or Scrypted) that exposes RTSP or a snapshot URL.
        provider: a model provider id (Intelligence sources > Model
        providers) with a vision-capable model; model: its id (default the
        provider's). threshold: 0-1, how much a frame must change (0.06).
        note: what matters to you, passed to the model. may_ring: let an
        urgent event (fire, smoke, flood, break-in) ring the phone (off: always
        quiet). dry_run: no saving or sending. Returns {changed, events,
        checked, errors, delivered}."""
        self._owner("camera review")
        try:
            todo = cams.check_cameras(cameras)
        except ValueError as e:
            raise CapError(str(e)) from None
        if not 0 < float(threshold) < 1:
            raise CapError("threshold: between 0 and 1 (0.06 by default)")
        llm = None
        with contextlib.suppress(Exception):
            llm = self._node.plugin("llm")
        if llm is None:
            raise CapError("camera review needs the llm plugin (Intelligence sources > Model providers)")
        try:
            client = llm.client(str(provider or ""), model or None, actor=caller_id(),
                                timeout=cams.MODEL_TIMEOUT, via="camera review")
        except Exception as e:  # noqa: BLE001 - ProviderError, LLMError
            raise CapError(f"provider: {e}") from None
        if not client.model:
            raise CapError(f"provider {provider!r} has no default model: name one in model")
        sf = self._state(state, "camera-review")
        prev = sf.load()
        nxt = {"at": self.clock(), "thumbs": dict(prev.get("thumbs") or {})}
        grabber = cams.Cameras(self._node, http=self.http)
        events, errors, checked, asked = [], [], [], 0
        told = dict(prev.get("told") or {})
        nxt["told"] = {}
        problems: list[str] = []          # model failures the person should hear about
        for cam in todo:
            name = clip(cam["name"], 40)
            try:
                small, keep = cams.thumb(await grabber.grab(cam))
            except Exception as e:  # noqa: BLE001 - one camera never stops the others
                errors.append(f"{name}: {clip(cams.redact(e, cam), 160)}")
                continue
            before_path = sf.sidecar(f"{cam['key']}.jpg")
            old = prev.get("thumbs", {}).get(cam["key"])
            diff = cams.difference(bytes.fromhex(old), small) if old else None
            row = {"camera": name, "diff": None if diff is None else round(diff, 3)}
            checked.append(row)
            before = None
            with contextlib.suppress(OSError):
                before = before_path.read_bytes()
            keep_old = False
            if diff is not None and diff >= float(threshold) and before:
                asked += 1
                try:
                    reply = await client.chat(cams.messages(name, before, keep, note), max_tokens=200,
                                              temperature=0)
                    verdict = cams.parse_verdict(reply.get("content") or "")
                except Exception as e:  # noqa: BLE001 - LLMError, an unreadable answer
                    msg = f"{name}: the model failed ({clip(cams.redact(e, cam), 140)})"
                    errors.append(msg)
                    # Keep the old frame: the change is looked at again next run.
                    keep_old = True
                    nxt["told"][cam["key"]] = msg
                    if told.get(cam["key"]) != msg:
                        problems.append(msg)
                    verdict = None
                if verdict is not None:
                    row["level"] = verdict["level"]
                    if verdict["level"] != "nothing":
                        events.append({"camera": name, **verdict})
            if keep_old:
                nxt["thumbs"][cam["key"]] = old
            elif not dry_run:
                nxt["thumbs"][cam["key"]] = small.hex()
                cams.write_private(before_path, keep)
        if todo and not checked:
            raise CapError("no camera answered: " + "; ".join(errors))
        urgent = bool(may_ring) and any(e["level"] == "urgent" for e in events)
        out: dict = {"changed": bool(events), "events": events, "checked": checked, "asked": asked,
                     "errors": errors, "urgent": urgent}
        if dry_run:
            out["dry_run"] = True
            return out
        sf.save(nxt)
        if events or problems:
            text = "\n".join([f"- {e['camera']}: {e['what'] or 'something changed'}" for e in events]
                             + [f"- {p} (it will look again)" for p in problems])
            line = (f"Camera review: {plural(len(events), 'notable event')} (" +
                    ", ".join(e["camera"] for e in events[:4]) + ")") if events else \
                f"Camera review: the model could not check {plural(len(problems), 'camera')}"
            out["delivered"] = await deliver(self._node, source="camera-review", title="Cameras",
                                             text=text, line=line, urgent=urgent,
                                             why="the picture changed and the model found it notable")
        return out


PLUGIN = Routines
