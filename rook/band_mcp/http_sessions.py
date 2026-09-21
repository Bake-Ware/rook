"""Bounded stateful MCP ownership for SDK 1.27+.

Keep SDK wire handling, but own the session task lifetime: SDK 1.27 retains
terminated transports and has no default abandoned-session deadline. No event
history is kept. Saturation rejects new sessions instead of evicting live calls.
"""
import logging
from uuid import uuid4

import anyio
from mcp.server.streamable_http import StreamableHTTPServerTransport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.responses import Response

SESSION_IDLE_SECONDS = 300.0
MAX_SESSIONS = 128
MAX_REQUESTS = 256


class BoundedSessionManager(StreamableHTTPSessionManager):
    def __init__(self, *args, idle_seconds=SESSION_IDLE_SECONDS,
                 max_sessions=MAX_SESSIONS, max_requests=MAX_REQUESTS, **kwargs):
        super().__init__(*args, **kwargs)
        self.idle_seconds = idle_seconds
        self.max_sessions = max_sessions
        self.max_requests = max_requests
        self._requests = 0
        self._owners = {}
        self._admission = anyio.Lock()

    async def _handle_stateful_request(self, scope, receive, send):
        request = Request(scope, receive)
        sid = request.headers.get('mcp-session-id')
        if self._requests >= self.max_requests:
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
                async with self._admission:
                    if len(self._server_instances) >= self.max_sessions:
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
                    owner = {'scope': None, 'posts': 0}
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
                            logging.getLogger(__name__).warning("MCP session ended: %s", type(error).__name__)
                        finally:
                            # Always release both roots, including DELETE and crashes.
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
                if posting:
                    owner['posts'] -= 1
                if transport.is_terminated:
                    self._server_instances.pop(sid, None)
                    lifetime.cancel()
                elif not owner['posts']:
                    lifetime.deadline = anyio.current_time() + self.idle_seconds
        finally:
            self._requests -= 1
