"""Bounded stateful MCP ownership for SDK 1.27+.

Keep SDK wire handling, but own the session task lifetime: SDK 1.27 retains
terminated transports and has no default abandoned-session deadline. No event
history is kept. At capacity a new session evicts the least recently used
idle session (no request in flight); only when every session is busy is the new
one refused. A client that abandons sessions without DELETE (one per call) then
costs only its own stale sessions, never everyone else's access. An evicted
client gets 404 on its next request and re-initializes, per the MCP spec.
Each bearer token may also hold at most ``max_per_key`` sessions; past that
its own oldest idle session goes, so one leaky client competes only with
itself. ``stats()`` feeds /healthz and the watchdog.
"""
import hashlib
import logging
import time
from collections import Counter, deque
from uuid import uuid4

import anyio
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import Response

log = logging.getLogger(__name__)

SESSION_IDLE_SECONDS = 300.0
MAX_SESSIONS = 128
MAX_REQUESTS = 256
MAX_PER_KEY = 48


class BoundedSessionManager(StreamableHTTPSessionManager):
    def __init__(self, *args, idle_seconds=SESSION_IDLE_SECONDS,
                 max_sessions=MAX_SESSIONS, max_requests=MAX_REQUESTS,
                 max_per_key=MAX_PER_KEY, **kwargs):
        super().__init__(*args, **kwargs)
        self.idle_seconds = idle_seconds
        self.max_sessions = max_sessions
        self.max_requests = max_requests
        self.max_per_key = max_per_key
        self.started = time.time()
        self.created = 0
        self.refused = 0
        self.key_evicted = 0
        self._recent = deque(maxlen=5000)   # (ts, key, ip) per new session
        self._requests = 0
        self._owners = {}
        self._admission = anyio.Lock()
        self.evicted = 0
        self._evict_logged = 0.0

    @staticmethod
    def _key(request) -> str:
        auth = request.headers.get('authorization') or ''
        return hashlib.sha256(auth.encode()).hexdigest()[:8] if auth else 'anon'

    def stats(self, window: float = 600.0) -> dict:
        """Counters plus who opened sessions recently (by token hash and IP)."""
        cutoff = time.time() - window
        recent = [(k, ip) for (t, k, ip) in self._recent if t >= cutoff]
        held = Counter(o.get('key', 'anon') for o in self._owners.values())
        return {
            'sessions': len(self._server_instances), 'max_sessions': self.max_sessions,
            'max_per_key': self.max_per_key, 'requests_in_flight': self._requests,
            'created': self.created, 'evicted': self.evicted, 'key_evicted': self.key_evicted,
            'refused': self.refused, 'uptime_secs': round(time.time() - self.started),
            'window_secs': window, 'new_sessions_in_window': len(recent),
            'top_keys': Counter(k for k, _ in recent).most_common(3),
            'top_ips': Counter(ip for _, ip in recent).most_common(3),
            'held_by_key': held.most_common(3),
        }

    def _evict_idle(self, key=None) -> bool:
        """Drop the least recently used session with no request in flight
        (only among ``key``'s sessions when given)."""
        idle = [(o['last'], sid) for sid, o in self._owners.items()
                if not o['active'] and o['scope'] is not None
                and (key is None or o.get('key') == key)]
        if not idle:
            return False
        _, sid = min(idle)
        owner = self._owners.pop(sid)
        self._server_instances.pop(sid, None)
        owner['scope'].cancel()     # run_session's finally terminates the transport
        if key is not None:
            self.key_evicted += 1
            return True
        self.evicted += 1
        now = time.monotonic()
        if now - self._evict_logged > 60:
            self._evict_logged = now
            log.warning("MCP sessions at capacity (%d): evicting idle sessions "
                        "(%d so far); some client is opening sessions without closing them",
                        self.max_sessions, self.evicted)
        return True

    async def _handle_stateful_request(self, scope, receive, send):
        request = Request(scope, receive)
        sid = request.headers.get('mcp-session-id')
        if self._requests >= self.max_requests:
            self.refused += 1
            await Response('MCP request capacity reached', 503,
                           headers={'Retry-After': '2'})(scope, receive, send)
            return
        self._requests += 1
        transport = None
        owner = None
        posting = request.method == 'POST'
        try:
            if sid is None:
                # Reserve and start atomically; don't hold this across network I/O.
                key = self._key(request)
                async with self._admission:
                    while (sum(o.get('key') == key for o in self._owners.values()) >= self.max_per_key
                           and self._evict_idle(key)):
                        pass
                    while len(self._server_instances) >= self.max_sessions and self._evict_idle():
                        pass
                    if (len(self._server_instances) >= self.max_sessions
                            or sum(o.get('key') == key for o in self._owners.values()) >= self.max_per_key):
                        self.refused += 1
                        await Response('MCP session capacity reached', 503,
                                       headers={'Retry-After': '2'})(scope, receive, send)
                        return
                    sid = uuid4().hex
                    transport = StreamableHTTPServerTransport(
                        mcp_session_id=sid,
                        is_json_response_enabled=self.json_response,
                        event_store=self.event_store,
                        security_settings=self.security_settings,
                        retry_interval=self.retry_interval,
                    )
                    self._server_instances[sid] = transport
                    # Born busy: counts its own first request, so it can't be evicted before it runs.
                    owner = {'scope': None, 'posts': 0, 'active': 1, 'last': time.monotonic(), 'key': key}
                    self.created += 1
                    client = scope.get('client')
                    self._recent.append((time.time(), key, client[0] if client else '?'))
                    self._owners[sid] = owner

                    async def run_session(*, task_status=anyio.TASK_STATUS_IGNORED):
                        try:
                            with anyio.CancelScope() as lifetime:
                                owner['scope'] = lifetime
                                lifetime.deadline = anyio.current_time() + self.idle_seconds
                                async with transport.connect() as streams:
                                    task_status.started()
                                    await self.app.run(*streams, self.app.create_initialization_options(),
                                                       stateless=False)
                        except Exception as error:
                            log.warning("MCP session ended: %s", type(error).__name__)
                        finally:
                            # Always release both roots, including DELETE and crashes.
                            # (Only if still ours: an evicted id is already gone.)
                            if self._owners.get(sid) is owner:
                                self._server_instances.pop(sid, None)
                                self._owners.pop(sid, None)
                            with anyio.CancelScope(shield=True):
                                await transport.terminate()

                    await self._task_group.start(run_session)
            else:
                transport = self._server_instances.get(sid)
                owner = self._owners.get(sid)
            if transport is None or owner is None:
                await Response('Session not found', 404)(scope, receive, send)
                return
            lifetime = owner['scope']
            if transport is not None and request.headers.get('mcp-session-id'):
                owner['active'] += 1
            owner['last'] = time.monotonic()
            if posting:
                owner['posts'] += 1
                # Active calls can legitimately run longer than the idle TTL.
                lifetime.deadline = float('inf')
            elif not owner['posts']:
                lifetime.deadline = anyio.current_time() + self.idle_seconds
            async def checked_send(message):
                try:
                    await send(message)
                except BaseException:
                    # SDK SSE handling can swallow send errors; cancel here too.
                    lifetime.cancel()
                    raise

            try:
                await transport.handle_request(scope, receive, checked_send)
            except BaseException:
                # A failed HTTP send/cancel must not leave a live session task.
                lifetime.cancel()
                raise
            finally:
                owner['active'] -= 1
                owner['last'] = time.monotonic()
                if posting:
                    owner['posts'] -= 1
                if transport.is_terminated:
                    self._server_instances.pop(sid, None)
                    lifetime.cancel()
                elif not owner['posts']:
                    lifetime.deadline = anyio.current_time() + self.idle_seconds
        finally:
            self._requests -= 1
