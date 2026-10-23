"""House engine: the house agent's persistent voice session as the voice's brain.

Opt-in on the voice host with ``VOICE_ENGINE=house`` (see the README). The
session host (``python -m rook.agents``, docs/design/persistent-agents.md 3.4)
keeps a Haiku "conductor" session per role and serves it over a LAN websocket,
``VOICE_HOUSE_URL`` with ``Authorization: Bearer VOICE_HOUSE_TOKEN``. For the
connections it serves (``VOICE_HOUSE_FOR``: ``owner``, the default, or ``all``)
a front_background turn goes to that lane instead of Front + Background:

  * client -> host ``{"t":"user","turn","text","identity":{"owner","name"}}``;
    the reply's ``delta`` text is spoken clause by clause through the same path
    Front uses (early first clause, echo guard registration, barge-in, "history
    is what was heard"); text after the conductor's tool calls is more deltas of
    the same turn, spoken as its continuation; ``end`` closes the turn.
  * barge-in or a newer turn: ``{"t":"cancel","turn"}`` for the old turn.
  * ``push`` announces an unsolicited turn (a back-end result): its deltas are
    spoken like a follow-up once nothing is being said and the user is quiet;
    one that cannot be spoken is reported (owner ``background`` event + log).
  * the lane unreachable, refused, or no first delta within FIRST_DELTA_S: the
    turn runs on the old Front/Background path, so the voice never goes silent.
  * control events from the host: ``{"t":"mode","mode","by":"voice"}`` (the voice
    was asked to switch its listening mode) switches the connection, only between
    modes.VOICE_SWITCHABLE; into dictation it remembers the mode to return to.
  * the conductor routes with hidden lines (``<<flow {...}>>``) that the host cuts
    out; should one ever arrive here, nothing from it on is spoken.

The house voice serves the assistant mode and, unless a custom prompt was set,
active listening and brainstorm (the turn says which: ``"mode"``).

The token is never logged, put in an event or an error text.
"""
import asyncio
import contextlib
import json
import logging
import os
import time
from typing import NamedTuple
from urllib.parse import urlsplit, urlunsplit

import random

from . import modes
from .front import Clauses
from .sounds import HOUSE_TAGS, keep_sounds

# No first delta this long after the turn started (connecting included): the
# old Front/Background path answers the turn instead.
FIRST_DELTA_S = float(os.environ.get('VOICE_HOUSE_FIRST_DELTA_S', '4'))
# Opening the lane (TCP + upgrade + "ready").
CONNECT_S = float(os.environ.get('VOICE_HOUSE_CONNECT_S', '2'))
# Longest quiet stretch inside a turn once it spoke (tool calls, a btw fork).
TURN_S = float(os.environ.get('VOICE_HOUSE_TURN_S', '60'))
SEND_S = 2.0
# After a failed connect the lane is not tried again for a while (turns go
# straight to the old path): 2 s doubling to 60 s; a refusal waits the most.
BACKOFF_MIN_S, BACKOFF_MAX_S = 2.0, 60.0
# A pushed result nobody could hear: how long to keep collecting its text for the report.
DROP_COLLECT_S = 10.0
FILLER = 'One moment.'
# Models overuse sounds they are merely allowed (Pianobar's DJ found the same), so the
# server offers them on about one turn in this many, and the voice may still use none.
SOUND_EVERY = int(os.environ.get('VOICE_HOUSE_SOUND_EVERY', '4'))
# Control events from the host (not part of a turn's reply): a listening-mode switch,
# the hub's answer about models, and the voice config (engine, models) when it changes.
CONTROL = ('mode', 'models', 'config')
# A routing line's start: the host strips them; this is the backstop.
ROUTE_MARK = '<<'
FAILED = "Sorry, that didn't work. Please try again."

log = logging.getLogger('voice.house')
# websockets logs every handshake header, the bearer token included, at debug
# level: its logger here never goes below WARNING, whatever the process level.
_ws_log = logging.getLogger('voice.house.websockets')
_ws_log.setLevel(logging.WARNING)
_warned = set()


class Settings(NamedTuple):
    url: str
    token: str
    scope: str        # 'owner' or 'all'

    def __repr__(self):
        return f'Settings(url={safe_url(self.url)!r}, token={"set" if self.token else "unset"}, scope={self.scope!r})'


def settings(env=None):
    """The engine's configuration, or None when it is off (``VOICE_ENGINE`` not ``house``)."""
    env = os.environ if env is None else env
    if env.get('VOICE_ENGINE', '').strip().lower() != 'house':
        return None
    url = env.get('VOICE_HOUSE_URL', '').strip()
    if not url:
        _warn_once('no-url', 'VOICE_ENGINE=house without VOICE_HOUSE_URL: the house engine stays off')
        return None
    token = env.get('VOICE_HOUSE_TOKEN', '').strip()
    if not token:
        _warn_once('no-token', 'VOICE_ENGINE=house without VOICE_HOUSE_TOKEN: the lane will likely refuse')
    scope = 'all' if env.get('VOICE_HOUSE_FOR', 'owner').strip().lower() == 'all' else 'owner'
    return Settings(url, token, scope)


def applies(config, identity):
    """Whether this connection's turns go to the house voice (owners only unless ``all``)."""
    return config is not None and (config.scope == 'all' or bool(identity.owner))


def _warn_once(key, message):
    if key not in _warned:
        _warned.add(key)
        log.warning(message)


def safe_url(url):
    """The lane's address for logs: no credentials, query or fragment."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.hostname + (f':{parts.port}' if parts.port else '')
                           if parts.hostname else '', parts.path, '', ''))
    except Exception:
        return '<invalid url>'


class Unavailable(Exception):
    """The lane cannot take this turn. ``code`` is short and goes in the timing line."""

    def __init__(self, code, detail=''):
        super().__init__(code)
        self.code, self.detail = code, detail


def _describe(error):
    """(code, detail, refused) for a failed connect, without anything from the request."""
    try:
        from websockets.exceptions import InvalidStatus
    except ImportError:            # pragma: no cover - websockets is a voice host requirement
        InvalidStatus = ()
    if InvalidStatus and isinstance(error, InvalidStatus):
        status = getattr(getattr(error, 'response', None), 'status_code', '?')
        return 'refused', f'HTTP {status}', True
    if isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        return 'timeout', f'no answer in {CONNECT_S:g} s', False
    if isinstance(error, OSError):
        return 'unreachable', type(error).__name__, False
    return 'failed', type(error).__name__, False


class Lane:
    """One websocket to the session host's voice lane per voice Connection: opened
    on first use, reopened after it drops (pushes keep arriving), and not retried
    during a backoff after a failure, so turns don't each pay a connect timeout."""

    def __init__(self, url, token, on_push=None, on_control=None):
        self.url, self._token = url, token
        self.on_push = on_push
        self.on_control = on_control
        self.ws = None
        self.reader = None
        self.session = None
        self.turns = {}
        self.pushes = {}
        self.lock = asyncio.Lock()
        self.retry_at = 0.0
        self.backoff = 0.0
        self.last = None
        self.closed = False
        self.reconnect_task = None
        self.tasks = set()
        # From the hub through the host: {"engine": "house"|"local", "models": {...}}.
        self.config = {}

    def __repr__(self):
        return f'Lane({safe_url(self.url)!r}, open={self.open})'

    @property
    def open(self):
        return self.ws is not None and self.reader is not None and not self.reader.done()

    def _scrub(self, text):
        text = ' '.join(str(text or '').split())[:300]
        return text.replace(self._token, '***') if self._token else text

    async def ensure(self):
        if self.closed:
            raise Unavailable('closed', 'the voice connection closed')
        if self.open:
            return
        async with self.lock:
            if self.open:
                return
            if time.monotonic() < self.retry_at:
                raise Unavailable('backoff', f'last failure: {self.last}')
            try:
                await self._connect()
            except asyncio.CancelledError:
                raise
            except Unavailable as error:
                self._failed(error.code, error.detail, error.code == 'refused')
                raise
            except Exception as error:
                code, detail, refused = _describe(error)
                self._failed(code, detail, refused)
                raise Unavailable(code, detail) from None
            self.backoff, self.retry_at, self.last = 0.0, 0.0, None

    def _failed(self, code, detail, refused=False):
        self.backoff = BACKOFF_MAX_S if refused else min(BACKOFF_MAX_S, max(BACKOFF_MIN_S, self.backoff * 2))
        self.retry_at = time.monotonic() + self.backoff
        self.last = f'{code} ({detail})' if detail else code
        log.warning('House voice lane %s unavailable: %s; not retried for %d s', safe_url(self.url), self.last,
                    self.backoff)

    async def _connect(self):
        # One budget for TCP, the upgrade and "ready": a host that accepts and then
        # says nothing costs a turn CONNECT_S once, then the backoff applies.
        ws, hello = await asyncio.wait_for(self._open(), CONNECT_S)
        self.session = hello.get('session')
        self.config = {k: hello[k] for k in ('engine', 'models') if k in hello}
        self.ws = ws
        self.reader = asyncio.create_task(self._read(ws))
        self.reader.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        log.info('House voice lane connected: %s (role %s)', safe_url(self.url), hello.get('role'))

    async def _open(self):
        from websockets.asyncio.client import connect
        headers = {'Authorization': 'Bearer ' + self._token} if self._token else {}
        ws = await connect(self.url, additional_headers=headers, open_timeout=None, logger=_ws_log, proxy=None,
                           max_size=1024 * 1024, compression=None)
        try:
            raw = await ws.recv()
            try:
                hello = json.loads(raw)
            except (TypeError, ValueError):
                hello = None
            if not isinstance(hello, dict) or hello.get('t') != 'ready':
                detail = self._scrub(hello.get('error')) if isinstance(hello, dict) and hello.get('t') == 'error' \
                    else 'no ready message'
                raise Unavailable('not_ready', detail)
        except BaseException:
            closing = asyncio.create_task(asyncio.wait_for(ws.close(), 1))
            closing.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            raise
        return ws, hello

    async def _read(self, ws):
        why = 'closed by the host'
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, dict):
                    try:
                        self._dispatch(message)
                    except Exception as error:
                        log.warning('House voice lane message ignored: %s', type(error).__name__)
        except asyncio.CancelledError:
            why = None
            raise
        except Exception as error:
            why = type(error).__name__
        finally:
            if self.ws is ws:
                self.ws = None
            for queue in [*self.turns.values(), *self.pushes.values()]:
                queue.put_nowait({'t': '_closed'})
            if why and not self.closed:
                log.warning('House voice lane %s dropped: %s', safe_url(self.url), why)
                self._schedule_reconnect()

    def _dispatch(self, message):
        kind, turn = message.get('t'), message.get('turn')
        if not isinstance(turn, (int, str, type(None))):
            return
        if isinstance(turn, str) and turn.isdigit() and turn not in self.turns \
                and turn not in self.pushes and int(turn) in self.turns:
            turn = int(turn)            # a host that echoes turn numbers as strings
        if kind == 'ready':
            self.session = message.get('session')
            self.config = {k: message[k] for k in ('engine', 'models') if k in message}
            return
        if kind == 'config':
            self.config = {k: message[k] for k in ('engine', 'models') if k in message}
        if kind in CONTROL:
            if self.on_control is not None:
                self.on_control(message)
            return
        if kind == 'push':
            if turn is None or turn in self.pushes or turn in self.turns:
                return
            self.pushes[turn] = queue = asyncio.Queue()
            if self.on_push is not None:
                self.on_push(turn, queue)
            return
        queue = self.turns.get(turn) if turn in self.turns else self.pushes.get(turn)
        if queue is not None:
            queue.put_nowait(message)
        elif kind == 'error':
            log.warning('House voice lane error: %s', self._scrub(message.get('error')))

    def _schedule_reconnect(self):
        if self.closed or (self.reconnect_task and not self.reconnect_task.done()):
            return
        self.reconnect_task = asyncio.create_task(self._reconnect())
        self.reconnect_task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def _reconnect(self):
        await asyncio.sleep(BACKOFF_MIN_S)
        while not self.closed and not self.open:
            with contextlib.suppress(Unavailable):
                await self.ensure()
            if not self.open:
                await asyncio.sleep(max(BACKOFF_MIN_S, self.retry_at - time.monotonic()))

    async def send(self, message):
        ws = self.ws
        if ws is None:
            raise Unavailable('closed', 'not connected')
        try:
            await asyncio.wait_for(ws.send(json.dumps(message)), SEND_S)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            raise Unavailable('send_failed', type(error).__name__) from None

    async def begin(self, turn, message):
        """Open the lane if needed and send a user turn; returns the turn's message queue."""
        await self.ensure()
        self.turns[turn] = queue = asyncio.Queue()
        try:
            await self.send(message)
        except BaseException:
            self.turns.pop(turn, None)
            raise
        return queue

    def finish(self, turn):
        self.turns.pop(turn, None)

    def finish_push(self, turn):
        self.pushes.pop(turn, None)

    def cancel(self, turn):
        """Barge-in / a newer turn: ``{"t":"cancel","turn"}``, without waiting (callers
        may be unwinding a cancellation)."""
        if not self.open:
            return
        task = asyncio.create_task(self.send({'t': 'cancel', 'turn': turn}))
        self.tasks.add(task)
        task.add_done_callback(lambda t: (self.tasks.discard(t), t.exception() if not t.cancelled() else None))

    async def close(self):
        self.closed = True
        for task in [self.reconnect_task, self.reader]:
            if task is not None and not task.done():
                task.cancel()
        ws, self.ws = self.ws, None
        if ws is not None:
            closing = asyncio.create_task(asyncio.wait_for(ws.close(), 2))
            closing.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)


class HouseVoice:
    """The house engine inside one front_background connection (pipeline.FrontBackground)."""

    def __init__(self, fb, config):
        self.fb, self.conn = fb, fb.conn
        self.lane = Lane(config.url, config.token, on_push=self._on_push, on_control=self._on_control)
        self.control_tasks = set()
        self.push_lock = asyncio.Lock()
        self.push_tasks = set()
        self.speaking_task = None
        self.stops = 0

    def sounds(self):
        """The sound tags this connection's voice performs, when the app wants them
        (hello "expressive", on unless the app says false): Chatterbox voices only."""
        if getattr(self.conn, 'expressive', True) is False:
            return ()
        tts = getattr(getattr(self.conn, 'provider', None), 'tts', None)
        try:
            return HOUSE_TAGS if tts is not None and tts.supports_tags(self.conn.voice) else ()
        except Exception:
            return ()

    def serves(self, mode):
        """The assistant mode, and active listening and brainstorm with their built-in
        prompts; conversation (children), roleplay and custom prompts keep their own path."""
        return mode.agent or (mode.id in modes.HOUSE_SERVED and not mode.custom)

    # --- control events ------------------------------------------------------------
    def _on_control(self, message):
        if message.get('t') in ('models', 'config'):
            # The phone's Voice models screen: the hub's answer, or a change made elsewhere.
            fields = {k: v for k, v in message.items() if k not in ('t', 'turn')}
            task = asyncio.create_task(self.conn.emit('voice_models', **fields))
            self.control_tasks.add(task)
            task.add_done_callback(lambda t: (self.control_tasks.discard(t),
                                              t.exception() if not t.cancelled() else None))
            return
        if message.get('t') == 'mode':
            task = asyncio.create_task(self._switch(message.get('mode'), message.get('by')))
            self.control_tasks.add(task)
            task.add_done_callback(lambda t: (self.control_tasks.discard(t),
                                              t.exception() if not t.cancelled() else None))

    async def _switch(self, target, by):
        """The voice was asked to change how it listens ("switch to dictation")."""
        conn = self.conn
        if conn.closed:
            return
        try:
            name = modes.normalize(target)
        except modes.UnknownMode:
            log.warning('House voice: unknown mode %r ignored', str(target)[:20])
            return
        here = conn.mode
        if by != 'voice' or name not in modes.VOICE_SWITCHABLE or here.id not in modes.VOICE_SWITCHABLE \
                or not self.serves(here):
            log.warning('House voice: mode switch %s -> %s refused', here.id, name)
            return
        if name == here.id:
            return
        await conn.set_mode(modes.resolve(name), by='voice')
        if name == 'dictate':
            conn.mode_return = here

    def speaking(self):
        return self.speaking_task is not None and not self.speaking_task.done()

    def identity(self, timing):
        """``{"owner", "name"}``: the speaker label when speaker ID named someone, else the key's name."""
        ident = self.conn.identity
        speaker = (timing.extra.get('speaker') if timing is not None else None)
        if isinstance(speaker, str) and speaker not in ('guest', 'assistant', ''):
            name = speaker
        elif ident.owner or ident.principal != 'unmapped':
            name = ident.principal
        else:
            name = 'guest'
        return {'owner': bool(ident.owner), 'name': name}

    # --- a user turn ------------------------------------------------------------
    async def turn(self, epoch, text, timing, merged_from=None, past=()):
        """Answer the turn through the lane. False: it could not (nothing was said
        and nothing routed), and the caller runs the old path for this turn."""
        fb, conn, lane = self.fb, self.conn, self.lane
        deadline = time.monotonic() + FIRST_DELTA_S
        try:
            await lane.ensure()
        except Unavailable as error:
            return self._fallback(epoch, timing, error.code, error.detail)
        if lane.config.get('engine') == 'local':
            # The hub chose this server's own models for the voice (home.voice_engine):
            # not a failure, so no error event; the old path answers.
            timing.extra['engine'] = 'front_background'
            timing.extra['house_engine'] = 'local'
            return False
        timing.extra['engine'] = 'house'
        message = {'t': 'user', 'turn': epoch, 'text': text, 'identity': self.identity(timing)}
        if conn.mode.id != 'assistant':
            message['mode'] = conn.mode.id
        sounds = self.sounds()
        if sounds and SOUND_EVERY > 0 and random.SystemRandom().randrange(SOUND_EVERY) == 0:
            message['sound_tags'] = list(sounds)
        if merged_from is not None:
            # This text includes the earlier, cancelled turn's words.
            message['merged_from'] = merged_from
        try:
            queue = await lane.begin(epoch, message)
        except Unavailable as error:
            return self._fallback(epoch, timing, error.code, error.detail)
        spoken, clauses = [], Clauses()
        got = ended = cancelled = failed = routed = False
        tools, reason = 0, None

        async def speak(parts):
            for clause in parts:
                clause = keep_sounds(clause, bool(sounds))
                if not clause.strip():
                    continue
                spoken.append(clause)
                await conn.say(clause, epoch, record=False)

        try:
            while True:
                wait = deadline - time.monotonic() if not got else TURN_S
                try:
                    message = await asyncio.wait_for(queue.get(), max(wait, .001))
                except asyncio.TimeoutError:
                    if got:
                        log.warning('House voice turn %d: nothing more for %g s; closing it', epoch, TURN_S)
                        break
                    if tools and not spoken:
                        # The conductor already routed something: never run the old path as
                        # well (it could act twice). Say so and keep waiting for its words.
                        await speak([FILLER])
                        deadline = time.monotonic() + TURN_S
                        continue
                    if tools:
                        log.warning('House voice turn %d: no words after its tool calls; closing it', epoch)
                        break
                    reason = ('no_first_delta', f'no words in {FIRST_DELTA_S:g} s')
                    break
                kind = message.get('t')
                if kind == 'delta':
                    piece = message.get('text')
                    if not isinstance(piece, str) or not piece or routed:
                        continue
                    if ROUTE_MARK in piece:
                        # A routing line the host should have cut: say nothing from it on.
                        routed = True
                        piece = piece.split(ROUTE_MARK, 1)[0]
                        if not piece.strip():
                            continue
                    if not got:
                        got = True
                        timing.mark('front_first_token_ms')
                    await speak(clauses.feed(piece))
                elif kind == 'tool':
                    tools += 1
                    name = str(message.get('name') or 'tool')[:80]
                    fb.emit('tool_call', epoch, 'House voice: ' + name, tool=name)
                    # The words before a tool call are a whole thought; the next ones start fresh.
                    await speak(clauses.flush(restart=True))
                elif kind == 'end':
                    ended = True
                    final = message.get('text')
                    if message.get('ok') is False:
                        failed = True
                        detail = lane._scrub(message.get('error')) or 'turn failed'
                        if not got and not tools and not spoken:
                            reason = ('error', detail)
                        else:
                            log.warning('House voice turn %d ended with an error: %s', epoch, detail)
                    elif not got and isinstance(final, str) and final.strip():
                        # A host that does not stream partial text: the whole reply at once.
                        got = True
                        timing.mark('front_first_token_ms')
                        await speak(clauses.feed(final))
                    break
                elif kind in ('error', '_closed'):
                    ended = True
                    failed = True
                    detail = 'the lane closed' if kind == '_closed' else lane._scrub(message.get('error'))
                    if not got and not tools and not spoken:
                        reason = ('closed' if kind == '_closed' else 'error', detail)
                    else:
                        log.warning('House voice turn %d stopped: %s', epoch, detail)
                    break
            if reason is None:
                await speak(clauses.flush())
                if failed and not spoken:
                    await speak([FAILED])
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            lane.finish(epoch)
            if not ended:
                # Barge-in, a newer turn, a timeout, or giving up for the old path.
                lane.cancel(epoch)
            if reason is None:
                self._record(epoch, text, spoken, cancelled, merged_from, past, timing)
        if reason is None:
            return True
        return self._fallback(epoch, timing, *reason)

    def _fallback(self, epoch, timing, code, detail=''):
        timing.extra['engine'] = 'front_background'
        timing.extra['house_fallback'] = code
        log.warning('House voice turn %d answered by the local voice: %s%s', epoch, code,
                    f' ({detail})' if detail else '')
        self.fb.emit('error', epoch, 'House voice unavailable (' + code + '); the local voice answers this turn.',
                     status='failed')
        return False

    def _record(self, epoch, text, spoken, cancelled, merged_from, past, timing):
        """History holds what the user heard (as for Front); Front's transcript gets the
        same exchange, so a later fallback turn extends it instead of starting cold."""
        fb, conn = self.fb, self.conn
        if 'first_audio_ms' in timing.marks:
            timing.marks.setdefault('front_first_audio_ms', timing.marks['first_audio_ms'])
        heard = conn.heard_text(epoch, spoken) if cancelled and spoken else ' '.join(spoken)
        if heard:
            conn.store.append(conn.session, 'assistant', {'text': heard})
        with contextlib.suppress(Exception):
            if merged_from is not None:
                fb.transcript.replace_last_user(text)
            fb.transcript.request(fb.fixed(), list(past), fb.board.facts(), text, fb.house_context)
            fb.transcript.commit(heard)

    # --- pushed turns (back-end results) ------------------------------------------
    def _on_push(self, turn, queue):
        task = asyncio.create_task(self._push(turn, queue))
        self.push_tasks.add(task)
        task.add_done_callback(lambda t: (self.push_tasks.discard(t), t.exception() if not t.cancelled() else None,
                                          self.fb._poke()))

    async def control(self, op, args):
        """The phone's Voice models screen (models_get, models_set): to the hub through
        the host, as this connection's identity; the answer comes back as voice_models."""
        message = {'t': 'control', 'op': op, 'args': dict(args or {}),
                   'identity': {'owner': bool(self.conn.identity.owner),
                                'name': self.conn.identity.principal}}
        try:
            await self.lane.ensure()
            await self.lane.send(message)
        except Unavailable as error:
            await self.conn.emit('error', code='models_unavailable',
                                 msg=f'The voice models can\'t be reached right now ({error.code}).')

    def stop(self):
        """The client said stop: pushed results waiting to be spoken stay silent (reported)."""
        self.stops += 1
        self.fb._poke()

    async def _push(self, turn, queue):
        fb, conn = self.fb, self.conn
        stops = self.stops
        deadline = time.monotonic() + float(os.environ.get('VOICE_FOLLOWUP_WAIT_S', '90'))
        try:
            async with self.push_lock:
                # Like a follow-up: wait until nothing is being said and the user is quiet.
                while not conn.closed and self.stops == stops and not fb.front_idle():
                    now = time.monotonic()
                    if now > deadline:
                        break
                    changed = fb._changed
                    wait = min(1.0, deadline - now, conn.play_until - now + .02 if conn.play_until > now else 1.0)
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(changed.wait(), max(wait, .001))
                if conn.closed or self.stops != stops or not fb.front_idle():
                    why = 'the connection closed' if conn.closed else 'stopped' if self.stops != stops \
                        else 'the conversation stayed busy'
                    await self._drop_push(turn, queue, why)
                    return
                task = self.speaking_task = asyncio.create_task(self._speak_push(turn, queue))
                task.add_done_callback(lambda t: (t.exception() if not t.cancelled() else None, fb._poke()))
                # A barge-in (Connection.interrupt) cancels it like any reply.
                conn.task = task
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if asyncio.current_task().cancelling():
                        task.cancel()
                        raise
        finally:
            self.lane.finish_push(turn)

    async def _drop_push(self, turn, queue, why):
        """Not spoken: report it (owner event with the text, a log line without it)."""
        parts, end = [], time.monotonic() + DROP_COLLECT_S
        while time.monotonic() < end:
            try:
                message = await asyncio.wait_for(queue.get(), end - time.monotonic())
            except asyncio.TimeoutError:
                break
            if message.get('t') == 'delta' and isinstance(message.get('text'), str):
                parts.append(message['text'])
            elif message.get('t') in ('end', 'error', '_closed'):
                if not parts and isinstance(message.get('text'), str):
                    parts.append(message['text'])
                break
        text = ''.join(parts).strip()
        log.warning('House voice result %s not spoken (%s); %d characters', turn, why, len(text))
        self.fb.emit('dropped', self.conn.epoch, 'Back-end result not spoken (' + why + ')', result=text or None)

    async def _speak_push(self, turn, queue):
        fb, conn = self.fb, self.conn
        epoch = conn.epoch
        spoken, clauses, said = [], Clauses(), ''
        ended = cancelled = False
        fb.emit('followup', epoch, 'House voice: telling you a back-end result')
        sounds = self.sounds()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(queue.get(), TURN_S)
                except asyncio.TimeoutError:
                    break
                kind = message.get('t')
                parts = []
                if kind == 'delta' and isinstance(message.get('text'), str):
                    said += message['text']
                    parts = clauses.feed(message['text'])
                elif kind == 'tool':
                    fb.emit('tool_call', epoch, 'House voice: ' + str(message.get('name') or 'tool')[:80],
                            tool=str(message.get('name') or 'tool')[:80])
                    parts = clauses.flush(restart=True)
                elif kind in ('end', 'error', '_closed'):
                    ended = True
                    if kind == 'end' and not said and isinstance(message.get('text'), str):
                        parts = clauses.feed(message['text'])
                for clause in parts:
                    clause = keep_sounds(clause, bool(sounds))
                    if clause.strip():
                        spoken.append(clause)
                        await conn.say(clause, epoch, record=False)
                if ended:
                    break
            for clause in clauses.flush():
                clause = keep_sounds(clause, bool(sounds))
                if clause.strip():
                    spoken.append(clause)
                    await conn.say(clause, epoch, record=False)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not ended:
                self.lane.cancel(turn)
            heard = conn.heard_text(epoch, spoken) if cancelled and spoken else ' '.join(spoken)
            if heard:
                conn.store.append(conn.session, 'assistant', {'text': heard})
                fb.transcript.add_assistant(heard)
            if epoch == conn.epoch and not conn.closed:
                with contextlib.suppress(Exception):
                    await conn.emit('assistant_done', turn=epoch)
                    await conn.emit('state', state='listening', turn=epoch)

    async def close(self):
        for task in [*self.push_tasks, *self.control_tasks, self.speaking_task]:
            if task is not None and not task.done():
                task.cancel()
        await self.lane.close()


def engine_for(fb):
    """The house engine for this connection, or None (off, or not for this identity)."""
    config = settings()
    return HouseVoice(fb, config) if applies(config, fb.conn.identity) else None
