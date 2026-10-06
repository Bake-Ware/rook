"""Cheap, opt-in lifecycle events; tools keep their originating turn across replies."""
import asyncio
import contextlib
import time


class Activity:
    heartbeat_seconds = 2.0

    def __init__(self, enqueue):
        self.enqueue = enqueue
        self.seq = 0
        self.jobs = {}
        self.heartbeat = None

    def emit(self, phase, turn, label, **fields):
        self.seq += 1
        self.enqueue({'type': 'activity', 'turn': turn, 'seq': self.seq,
                      'ts': int(time.time() * 1000), 'phase': phase, 'label': label, **fields})

    def start_job(self, jid, turn, tool, args, timeout_ms, elapsed_ms=0):
        if jid in self.jobs:
            return
        fields = {'tool': tool, 'timeout_ms': timeout_ms}
        for key in ('worker', 'cap'):
            if isinstance(args.get(key), str):
                fields[key] = args[key][:200]
        self.jobs[jid] = (turn, time.monotonic() - elapsed_ms / 1000, fields)
        self.emit('tool_start', turn, 'Starting ' + tool, **fields)
        if self.heartbeat is None or self.heartbeat.done():
            self.heartbeat = asyncio.create_task(self._wait())

    async def _wait(self):
        while self.jobs:
            await asyncio.sleep(self.heartbeat_seconds)
            for turn, started, fields in tuple(self.jobs.values()):
                self.emit('tool_wait', turn, 'Waiting for ' + fields['tool'],
                          elapsed_ms=int((time.monotonic() - started) * 1000), **fields)

    def result(self, event):
        if event.get('status') == 'running':
            return
        job = self.jobs.pop(event.get('id'), None)
        if job is None:
            return
        turn, started, fields = job
        status = {'completed': 'ok', 'cancel_requested': 'cancelled'}.get(event.get('status'), 'failed')
        detail = {} if status == 'ok' else {'detail': str(event.get('result') or 'Tool failed without an error message')[:500]}
        self.emit('tool_result', turn, fields['tool'] + (' completed' if status == 'ok' else ' ' + status),
                  status=status, elapsed_ms=int((time.monotonic() - started) * 1000), **detail, **fields)
        if not self.jobs and self.heartbeat:
            self.heartbeat.cancel()

    async def close(self):
        self.jobs.clear()
        if self.heartbeat:
            self.heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.heartbeat
