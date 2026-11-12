"""The event bus (J2): one bus; an event is a name and a JSON payload.

Sources: hub state changes (worker.online, worker.offline, token.used), the
schedule, webhooks, agents (the event.emit hub cap) and workers (the `event`
protocol message). Every event is journaled (J8), except token.used for now
(see the pull request). Each event is matched against the jobs (jobs.py).

`emit` only queues the event, so any code may call it, even while holding the
hub's lock. One task, started with the hub, journals each event in order and
starts the runs of the jobs it matches. A second task wakes every second to
notice workers that have missed three heartbeats (H3) and schedules that are due.
"""

import asyncio
import collections
import json

from aiohttp import web

from . import caps, jobs
from .errors import HubError

TICK = 1.0  # seconds between looks at heartbeats and schedules


class Events:
    def __init__(self, hub):
        self.hub = hub
        self._queue = collections.deque()
        self._wake = None        # an asyncio.Event while running
        self._tasks = set()
        self._online = set()     # workers the hub has seen come online and not yet go offline
        self._last_tick = None

    def emit(self, name, payload, *, user=None, token=None, worker=None, journal=True):
        """Put an event on the bus. Never blocks and never touches the database."""
        self._queue.append({"name": name, "payload": payload, "user": user, "token": token,
                            "worker": worker, "journal": journal})
        if self._wake is not None:
            self._wake.set()

    # --------------------------------------------------------- running

    def start(self):
        self._wake = asyncio.Event()
        self._wake.set()
        self._last_tick = self.hub.clock()
        for loop in (self._dispatch(), self._watch()):
            self._spawn(loop)

    async def stop(self):
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._wake = None

    def _spawn(self, coroutine):
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _dispatch(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue:
                event = self._queue.popleft()
                if event["journal"]:
                    self._journal(event)
                for job in jobs.matching(self.hub, event):
                    self._spawn(jobs.run(self.hub, job, event))

    def _journal(self, event):
        payload, job, run = event["payload"], None, None
        if event["name"] in jobs.COMPLETIONS:     # never journal a usable token a hub cap returned
            payload = dict(payload, result=caps.for_journal(payload["result"]))
            job, run = payload.get("job"), payload.get("run")
        with self.hub._lock, self.hub._db:
            self.hub._journal("event", (event["user"], event["token"]), event["worker"],
                              {"event": event["name"], "payload": payload}, job=job, run=run)

    async def _watch(self):
        while True:
            await asyncio.sleep(TICK)
            self.tick()

    def tick(self):
        """Look at the clock: emit worker.offline for workers that missed three
        heartbeats (H3), and a schedule event for each schedule that came due."""
        now, last = self.hub.clock(), self._last_tick
        self._last_tick = now
        for guid in list(self._online):
            record = self.hub.get_worker(guid)
            if not record["online"]:
                self._online.discard(guid)
                self.emit("worker.offline", _worker(record), worker=guid)
        for spec in jobs.schedules(self.hub):
            if jobs.schedule_due(spec, last, now):
                self.emit("schedule", {"schedule": spec})

    def heartbeat(self, record):
        """A worker heartbeated: worker.online if it was not online before."""
        if record["guid"] not in self._online:
            self._online.add(record["guid"])
            self.emit("worker.online", _worker(record), worker=record["guid"])


def _worker(record):
    return {"worker": record["guid"], "worker_moniker": record["moniker"], "name": record["name"]}


# ------------------------------------------------------------------ webhooks

def add_routes(app, hub):
    async def webhook(request):
        """`POST /webhook` with a webhook token: the body becomes a `webhook` event (J2)."""
        header = request.headers.get("Authorization", "")
        try:
            if not header.startswith("Bearer ") or not header[7:].strip():
                raise HubError("token_missing", "The webhook needs a webhook token.",
                               "Send the header \"Authorization: Bearer <token>\" with a webhook token from this hub.")
            with hub._lock, hub._db:
                user, token = hub.auth.use(header[7:].strip(), "webhook")
                moniker = hub._moniker(token)
        except HubError as e:
            status = 403 if e.message["code"] == "token_scope" else 401
            return web.json_response(e.message, status=status)
        body = await request.content.read()
        try:
            body = json.loads(body) if body else None
        except ValueError:
            body = body.decode("utf-8", "replace")
        hub.events.emit("webhook", {"token": token, "token_moniker": moniker, "body": body}, user=user, token=token)
        return web.json_response({"emitted": "webhook"}, status=202)

    app.router.add_post("/webhook", webhook)

