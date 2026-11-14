"""An in-memory link between the hub and one worker, for tests.

It stands in for the Telesthete link: it carries whole messages both ways and
tells the hub which worker key is on the other end. Here the worker simply
declares its key; on Telesthete the key would be what authenticates the link.
"""

import asyncio


class MemoryLink:
    def __init__(self, key, inbox, outbox, state):
        self.key = key
        self._inbox, self._outbox, self._state = inbox, outbox, state

    async def send(self, data):
        if self._state["closed"]:
            raise EOFError
        self._outbox.put_nowait(data)

    async def recv(self):
        data = await self._inbox.get()
        if data is None:
            raise EOFError
        return data

    def close(self):
        if not self._state["closed"]:
            self._state["closed"] = True
            self._inbox.put_nowait(None)
            self._outbox.put_nowait(None)


def link_pair(key):
    """(hub end, worker end) of one link."""
    to_hub, to_worker, state = asyncio.Queue(), asyncio.Queue(), {"closed": False}
    return MemoryLink(key, to_hub, to_worker, state), MemoryLink(key, to_worker, to_hub, state)


def connector(hub):
    """A worker's `connect(session)` that opens an in-memory link served by `hub`. It carries
    nothing under the session's key: it stands in for the carrier (build plan L5)."""
    async def connect(session):
        hub_end, worker_end = link_pair(session.key)
        asyncio.get_running_loop().create_task(hub.serve_link(hub_end))
        return worker_end
    return connect
