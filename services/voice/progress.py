"""Templated, bounded spoken job updates. No planner or additional tool calls."""
import asyncio
import contextlib
import math
import time


class ProgressUpdates:
    poll_seconds = .25
    quiet_seconds = 3.0

    def __init__(self, conn, config=None):
        self.conn = conn
        config = config if isinstance(config, dict) else {}
        self.enabled = config.get('enabled') is not False
        self.first = self._seconds(config.get('first_after_s'), 25)
        self.every = self._seconds(config.get('every_s'), 45)
        self.jobs = {}
        self.scheduler = None
        self.speaker = None
        self.speaking_job = None

    @staticmethod
    def _seconds(value, default):
        return float(value) if (isinstance(value, (int, float)) and not isinstance(value, bool)
                               and math.isfinite(value) and value > 0) else default

    def start(self, jid, turn, tool, args, elapsed_ms=0):
        if not self.enabled or jid in self.jobs:
            return
        now = time.monotonic()
        self.jobs[jid] = dict(turn=turn, tool=tool, worker=args.get('worker'),
                              started=now-elapsed_ms/1000, due=now+self.first,
                              count=0, progress=None)
        if self.scheduler is None or self.scheduler.done():
            self.scheduler = asyncio.create_task(self._run())

    def spoken(self, turn):
        for job in self.jobs.values():
            if job['turn'] == turn and not job['count']:
                job['due'] = time.monotonic() + self.first

    def event(self, event):
        jid = event.get('id')
        if event.get('status') == 'running':
            if jid in self.jobs and (event.get('progress') or event.get('title')):
                self.jobs[jid]['progress'] = str(event.get('progress') or event['title'])[:240]
            return
        self.jobs.pop(jid, None)
        if jid == self.speaking_job:
            self.cancel_speech()

    def cancel_speech(self):
        if self.speaker and not self.speaker.done():
            self.speaker.cancel()

    def suppressed(self):
        c = self.conn
        now = time.monotonic()
        return (c.closed or c.sleeping or c.receiving_speech or
                now - c.last_speech < self.quiet_seconds or now < c.play_until or
                bool(c.task and not c.task.done()) or bool(c.pending_results))

    @staticmethod
    def line(job):
        if job['count'] == 3:
            return "This is taking a while - I'll tell you when it's done."
        label = job['tool'].replace('_', ' ')
        worker = job['worker']
        if job['progress']:
            return f"{label}: {job['progress']}"
        elapsed = max(0, int(time.monotonic()-job['started']))
        if worker:
            if job['count'] % 2 == 0:
                return f"Still waiting on {worker}."
            return f"Still running {label} on {worker} - {elapsed} seconds so far."
        return (f"Still waiting for {label}." if job['count'] % 2 == 0 else
                f"Waiting for {label} - {elapsed} seconds so far.")

    async def _run(self):
        while self.jobs and not self.conn.closed:
            await asyncio.sleep(self.poll_seconds)
            if self.suppressed():
                continue
            for jid, job in tuple(self.jobs.items()):
                if job['count'] >= 4 or time.monotonic() < job['due']:
                    continue
                # One update in flight per connection, no queue of deferred lines.
                self.speaking_job = jid
                self.speaker = asyncio.create_task(self.conn.say_progress(self.line(job), job))
                try:
                    spoken = await self.speaker
                    if spoken is False:
                        continue
                    job['count'] += 1
                    job['due'] = time.monotonic() + self.every
                except asyncio.CancelledError:
                    if self.conn.closed or asyncio.current_task().cancelling():
                        raise
                except Exception:
                    # A broken TTS path must not spin or affect job completion.
                    job['count'] = 4
                finally:
                    self.speaker = None
                    self.speaking_job = None
                break

    async def close(self):
        self.jobs.clear()
        self.cancel_speech()
        if self.scheduler:
            self.scheduler.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.scheduler
