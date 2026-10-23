"""The voice lane server (design 3.4): ``ws://<host>:8791/voice/<role>``.

The voice server (kaiju) connects over the LAN with the role's voice token
(checked against ``voice_token_sha256`` from ``configure``) and sends the
person's turns; the host streams the voice lane's text deltas back. Several
clients may be connected; their turns go one at a time into the one voice
session, and a new user turn while one runs interrupts it first (barge-in).
Back-end results (``voice_say`` from the hub) wait until the lane is idle and
no user turn is pending, go in as ``[back end] <text>`` and are streamed to the
client that asked (else the most recently active one) as a ``push`` turn; with
nobody connected, the latest few are kept for the next user turn.

The voice session has no tools; it routes with a line at the end of its reply
(:mod:`.routing`). Routing lines never reach the client: they are cut from the
stream and the reply text, and their ops go to the hub in ``voice_reply``.
The hub keeps a short standing brief (``voice_context``: time, what the agent is
doing, recent decisions) that goes in front of the next user turn whenever it
changed, and sends control events for the client (``voice_event``: a listening
mode change).
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import hashlib
import hmac
import itertools
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import routing
from .lane import Lane, LaneError, Turn

if TYPE_CHECKING:  # pragma: no cover
    from .host import Host

log = logging.getLogger("rook.agents.voice")

MAX_TEXT = 8000
HELD = 5
TURN_ID_MAX = 64
MAX_CONTEXT = 4000
#: The standing brief goes in again at least this often (turns), in case it was compacted away.
CONTEXT_EVERY = 20
#: Control events the hub may send a voice client (``voice_event``).
EVENTS = ("mode",)
#: Listening modes that reach the voice lane, and how a turn says so.
MODE_NOTES = {"listen": "active listening", "brainstorm": "brainstorm"}


class Client:
    _ids = itertools.count(1)

    def __init__(self, ws: web.WebSocketResponse) -> None:
        self.id = next(self._ids)
        self.ws = ws
        self.last_active = time.monotonic()
        self._out: asyncio.Queue = asyncio.Queue()
        self._writer = asyncio.create_task(self._write())

    def send(self, obj: dict) -> None:
        """Queue a message (in order) for this client."""
        if not self.ws.closed:
            self._out.put_nowait(obj)

    async def _write(self) -> None:
        with contextlib.suppress(Exception):
            while True:
                obj = await self._out.get()
                await self.ws.send_str(json.dumps(obj))

    async def flush(self, timeout: float = 2.0) -> None:
        end = time.monotonic() + timeout
        while not self._out.empty() and time.monotonic() < end and not self.ws.closed:
            await asyncio.sleep(0.01)

    def close(self) -> None:
        self._writer.cancel()


class VoiceRoom:
    """The voice lane's turns for one role."""

    def __init__(self, host: "Host", role: str) -> None:
        self.host = host
        self.role = role
        self.clients: dict[int, Client] = {}
        self.lock = asyncio.Lock()
        self.pending_user = 0
        self.quiet = asyncio.Event()
        self.quiet.set()
        self.held: collections.deque = collections.deque(maxlen=HELD)
        self.turn_client: collections.OrderedDict[str, int] = collections.OrderedDict()
        self.current: Turn | None = None
        self.current_turn_id: str | None = None
        self.context = ""
        self.tz = ""
        self._context_sent: tuple[str, str | None] | None = None
        self._since_context = 0
        self._push_n = 0
        self._tasks: set[asyncio.Task] = set()

    def lane(self) -> Lane | None:
        rs = self.host.roles.get(self.role)
        return rs.lanes.get("voice") if rs else None

    def spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    def add(self, ws: web.WebSocketResponse) -> Client:
        c = Client(ws)
        self.clients[c.id] = c
        return c

    def remove(self, c: Client) -> None:
        self.clients.pop(c.id, None)
        c.close()

    def _remember(self, turn_id: str, c: Client) -> None:
        self.turn_client[turn_id] = c.id
        self.turn_client.move_to_end(turn_id)
        while len(self.turn_client) > 200:
            self.turn_client.popitem(last=False)

    # -- the standing brief ---------------------------------------------------------
    def set_context(self, text: str, tz: str = "") -> dict:
        self.context = str(text or "").strip()[:MAX_CONTEXT]
        self.tz = tz.strip()[:64]
        return {"ok": True, "chars": len(self.context)}

    def now(self) -> str:
        """The time for a turn's header, in the hub's time zone (else the host's)."""
        import datetime as _dt
        t = None
        if self.tz:
            with contextlib.suppress(Exception):
                from zoneinfo import ZoneInfo
                t = _dt.datetime.now(ZoneInfo(self.tz))
        t = t or _dt.datetime.now().astimezone()
        return t.strftime("%a %b %-d, %-I:%M %p")

    def _context_due(self) -> str:
        """The brief, when it changed since it last went in (or the session changed)."""
        lane = self.lane()
        if not self.context:
            return ""
        key = (self.context, lane.session_id if lane else None)
        if key != self._context_sent or self._since_context >= CONTEXT_EVERY:
            return self.context
        return ""

    def _context_used(self) -> None:
        lane = self.lane()
        self._context_sent = (self.context, lane.session_id if lane else None)
        self._since_context = 0

    # -- user turns ---------------------------------------------------------------
    async def user(self, c: Client, turn_id: str, text: str, identity: dict,
                   mode: str = "") -> None:
        c.last_active = time.monotonic()
        self._remember(turn_id, c)
        self.host.emit(self.role, "voice", "voice_turn", turn=turn_id, text=text,
                       identity=identity, **({"mode": mode} if mode else {}))
        self.pending_user += 1
        self.quiet.clear()
        try:
            if self.current is not None:
                self.spawn(self._barge(self.current))     # barge-in
            async with self.lock:
                name = str(identity.get("name") or "").strip() or "guest"
                note = MODE_NOTES.get(mode)
                body = f"[voice | {name}{' | ' + note if note else ''} | {self.now()}] {text}"
                if self.held:
                    away = "\n".join(f"[back end, while you were away] {h}" for h in self.held)
                    self.held.clear()
                    body = f"{away}\n\n{body}"
                ctx = self._context_due()
                if ctx:
                    body = f"[brief]\n{ctx}\n\n{body}"
                self._since_context += 1
                if await self._run(c, turn_id, body) and ctx:
                    self._context_used()
        finally:
            self.pending_user -= 1
            if self.pending_user <= 0:
                self.pending_user = 0
                self.quiet.set()

    async def cancel(self, c: Client, turn_id: str | None) -> bool:
        cur = self.current
        if cur is None or (turn_id and turn_id != self.current_turn_id):
            return False
        await self._barge(cur)
        return True

    async def _barge(self, turn: Turn, tries: int = 5) -> None:
        """Interrupt ``turn`` until it ends: an interrupt that reaches Claude
        Code just before the turn's message is read is not applied to it."""
        lane = self.lane()
        for _ in range(tries):
            if lane is None or turn.done.done():
                return
            with contextlib.suppress(LaneError):
                await lane.interrupt(turn)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(turn.done), 1.0)

    async def _run(self, c: Client, turn_id: str, body: str, push_for: Any = None) -> bool:
        """One turn on the voice lane, streamed to ``c``; True when it started."""
        lane = self.lane()
        if lane is None:
            c.send({"t": "error", "turn": turn_id, "error": "this role has no voice lane"})
            return False
        route = routing.RoutingFilter()

        def sink(kind: str, **fields) -> None:
            if kind == "delta":
                text = route.feed(str(fields.get("text") or ""))
                if text:
                    c.send({"t": "delta", "turn": turn_id, "text": text})
                return
            c.send({"t": kind, "turn": turn_id, **fields})

        turn = Turn(None, "voice", sink)
        while True:
            try:
                await lane.ensure_running()
                await lane.wait_idle()
                self.current, self.current_turn_id = turn, turn_id
                await lane.begin(turn, body)
                if self.pending_user > (0 if push_for is not None else 1):
                    self.spawn(self._barge(turn))     # someone already spoke over it
                break
            except LaneError as e:
                self.current = self.current_turn_id = None
                if str(e) == "busy":
                    continue
                c.send({"t": "error", "turn": turn_id, "error": str(e)})
                return False
        try:
            res = await turn.done
        finally:
            self.current = self.current_turn_id = None
        total_ms = round((time.monotonic() - turn.t0) * 1000)
        if turn.deltas:
            tail = route.finish()
            if tail:
                c.send({"t": "delta", "turn": turn_id, "text": tail})
            text, ops, bad = routing.strip("".join(turn.deltas)) + (route.bad,)
        else:
            text, ops = routing.strip(str(res.get("text") or ""))
            bad = 0
        ok = bool(res.get("ok"))
        end = {"t": "end", "turn": turn_id, "text": text, "ok": ok,
               "first_ms": turn.first_ms, "total_ms": total_ms}
        if not ok:
            end["error"] = res.get("error") or "failed"
            if turn.interrupted:
                end["interrupted"] = True
        c.send(end)
        reply = {"turn": turn_id, "text": text, "first_ms": turn.first_ms, "total_ms": total_ms,
                 "tools": list(turn.tools), "thinking_tokens": res.get("thinking_tokens", 0),
                 "ok": ok}
        if ops and not turn.interrupted:
            # An interrupted reply's routing is dropped: the person spoke over it.
            reply["routing"] = ops
        if bad:
            reply["routing_bad"] = bad
        if not ok:
            reply["error"] = end["error"]
            reply["interrupted"] = turn.interrupted
        if push_for is not None:
            reply["push"] = True
            if push_for:
                reply["for_turn"] = push_for
        self.host.emit(self.role, "voice", "voice_reply", **reply)
        return True

    # -- control events for the client (a mode change) ---------------------------------
    def event(self, kind: str, fields: dict, for_turn: Any = None) -> dict:
        if kind not in EVENTS:
            raise ValueError(f"event: one of {', '.join(EVENTS)}")
        c = self._target(for_turn)
        if c is None:
            return {"ok": False, "error": "no voice client is connected"}
        c.send({"t": kind, "turn": for_turn, **fields})
        return {"ok": True, "client": c.id}

    # -- back-end results ---------------------------------------------------------
    def say(self, text: str, for_turn: str | None) -> dict:
        self.spawn(self._say(text, for_turn))
        return {"queued": True, "clients": len(self.clients)}

    def _target(self, for_turn: str | None) -> Client | None:
        if for_turn and for_turn in self.turn_client:
            c = self.clients.get(self.turn_client[for_turn])
            if c is not None:
                return c
        if not self.clients:
            return None
        return max(self.clients.values(), key=lambda c: c.last_active)

    async def _say(self, text: str, for_turn: str | None) -> None:
        while True:
            await self.quiet.wait()
            async with self.lock:
                if self.pending_user:
                    continue
                c = self._target(for_turn)
                if c is None:
                    self.held.append(text)
                    return
                self._push_n += 1
                turn_id = f"push-{self._push_n}"
                self._remember(turn_id, c)
                c.send({"t": "push", "turn": turn_id})
                await self._run(c, turn_id, f"[back end] {text}", push_for=for_turn or "")
                return

    async def close(self) -> None:
        for t in list(self._tasks):
            t.cancel()
        for c in list(self.clients.values()):
            with contextlib.suppress(Exception):
                await c.ws.close()
        self.clients.clear()


def token_ok(token: str, expected: str | None) -> bool:
    if not token or not expected:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, str(expected).strip().lower())


class VoiceServer:
    def __init__(self, host: "Host") -> None:
        self.host = host
        self.runner: web.AppRunner | None = None
        self.port: int | None = None

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/voice/{role}", self.handle)
        return app

    async def start(self, addr: tuple[str, int]) -> None:
        self.runner = web.AppRunner(self.app(), access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, addr[0], addr[1])
        await site.start()
        with contextlib.suppress(Exception):
            self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        role = request.match_info["role"]
        rs = self.host.roles.get(role)
        auth = request.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        token = token or request.query.get("token", "")
        if rs is None or not token_ok(token, rs.voice_hash) or rs.voice is None:
            return web.Response(status=403, text="forbidden")
        room = rs.voice
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1024 * 1024)
        await ws.prepare(request)
        c = room.add(ws)
        lane = room.lane()
        c.send({"t": "ready", "role": role, "session": lane.session_id if lane else None})
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                        break
                    continue
                try:
                    obj = json.loads(msg.data)
                except ValueError:
                    c.send({"t": "error", "turn": None, "error": "not JSON"})
                    continue
                if not isinstance(obj, dict):
                    continue
                t = obj.get("t")
                # Echoed back exactly as sent: the voice server keys its turns by
                # number, others by string; a converted id never matches.
                raw = obj.get("turn")
                if isinstance(raw, int) and not isinstance(raw, bool):
                    turn_id = raw
                else:
                    turn_id = str(raw or "")[:TURN_ID_MAX]
                if t == "user":
                    text = obj.get("text")
                    if turn_id == "" or not isinstance(text, str) or not text.strip() \
                            or len(text) > MAX_TEXT:
                        c.send({"t": "error", "turn": turn_id or None,
                                "error": "user: turn and text (up to 8000 characters)"})
                        continue
                    ident = obj.get("identity") if isinstance(obj.get("identity"), dict) else {}
                    ident = {k: ident.get(k) for k in ("owner", "name") if k in ident}
                    mode = str(obj.get("mode") or "")[:20]
                    room.spawn(room.user(c, turn_id, text.strip(), ident, mode))
                elif t == "cancel":
                    c.last_active = time.monotonic()
                    room.spawn(room.cancel(c, turn_id or None))
                else:
                    c.send({"t": "error", "turn": turn_id or None, "error": f"unknown t {t!r}"})
        finally:
            room.remove(c)
        return ws
