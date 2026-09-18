"""Side-channel lifecycle: never consult decisions from the normal voice path."""
import asyncio
import contextlib
import json
import logging
import os
import re
import time
import uuid


class Shadow:
    def __init__(self, connection, client, feedback, conversation):
        self.conn, self.client, self.feedback = connection, client, feedback
        self.conversation = conversation
        self.tasks = set()
        self.current_id = None
        self.current_turn = None
        self.interrupted_playback = False
        self.silence_task = None
        self.last_spoke = 0
        self.last_reply = ''
        self.reply_turn = None
        self.recent_seconds = float(os.environ.get('DECISION_RECENT_SPEECH_SECONDS', '15'))
        self.silence_seconds = float(os.environ.get('DECISION_SILENCE_SECONDS', '15'))
        names = [n.strip() for n in os.environ.get('DECISION_ASSISTANT_NAMES', 'rook,assistant').split(',') if n.strip()]
        self.wake = re.compile(r'\b(?:' + '|'.join(re.escape(n) for n in names) + r')\b', re.I) if names else None
        row = connection.store.db.execute("SELECT body FROM events WHERE session=? AND kind='assistant' ORDER BY id DESC LIMIT 1",
                                           (connection.session,)).fetchone()
        if row:
            self.last_reply = json.loads(row['body']).get('text', '').removeprefix(
                '[Spoken response generated; playback may be interrupted] ')[:1000]

    def _track(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        def done(t):
            self.tasks.discard(t)
            if not t.cancelled() and t.exception():
                logging.warning('Decision shadow task failed: %s', type(t.exception()).__name__)
        task.add_done_callback(done)
        return task

    def activity(self):
        if self.silence_task:
            self.silence_task.cancel()
            self.silence_task = None

    def interrupt(self, playing, reply_active):
        self.activity()
        self.interrupted_playback |= playing
        if self.current_id and (playing or reply_active):
            self.feedback.submit('signal', self.current_id, 'reply_interrupted',
                                 {'playback': playing, 'generation': reply_active})

    def begin(self, text, source, turn):
        self.activity()
        self.current_id = uuid.uuid4().hex
        self.current_turn = turn
        state = {'text': (text or '')[:8000], 'source': source,
                 'assistant_spoke_recently': time.monotonic() - self.last_spoke <= self.recent_seconds,
                 'recent_speech_window_seconds': self.recent_seconds,
                 'contains_wake_word_or_assistant_name': bool(self.wake and self.wake.search(text or '')),
                 'previous_assistant_reply': self.last_reply[:1000],
                 'interrupted_playback': self.interrupted_playback}
        self.interrupted_playback = False
        did = self.current_id
        self.feedback.submit('begin', did, self.conn.session, self.conversation, turn, source, state, time.time())
        if len(self.tasks) >= 8:
            # Capacity applies only to telemetry, never to the normal turn.
            event = self.client.event(source, turn, 'error')
            event['error'] = 'Decision shadow capacity exceeded'
            self.feedback.submit('finish', did, event, dict(self.client.info))
            return
        self._track(self._decide(did, state, source, turn))

    async def _decide(self, did, state, source, turn):
        event = await self.client.decide(state, source, turn)
        self.feedback.submit('finish', did, event, dict(self.client.info))
        if self.conn.thinking and not self.conn.closed:
            # A disconnected/slow opt-in consumer cannot fail the foreground turn.
            with contextlib.suppress(Exception):
                await self.conn.emit('decision', **{k: v for k, v in event.items() if k != 'type'})

    def reply(self, text, turn):
        if self.reply_turn != turn:
            self.last_reply = ''
            self.reply_turn = turn
        self.last_reply = (self.last_reply + ' ' + text).strip()[:1000]
        if turn == self.current_turn:
            self.feedback.submit('reply', self.current_id, text)

    def completed(self, turn):
        if turn != self.current_turn:
            return
        self.feedback.submit('completed', self.current_id)
        if self.reply_turn == turn:
            self.activity()
            self.silence_task = self._track(self._silence(self.current_id, turn))

    async def _silence(self, did, turn):
        # Wait for playback drain AND an observation window. Disconnect, input,
        # barge-in and newer turns cancel this; silence does not imply approval.
        await asyncio.sleep(max(0, self.conn.play_until - time.monotonic()) + self.silence_seconds)
        if not self.conn.closed and not self.conn.receiving_speech and self.current_turn == turn:
            self.feedback.submit('signal', did, 'no_followup',
                                 {'window_seconds': self.silence_seconds, 'connected': True, 'approval': None})

    async def close(self):
        self.activity()
        # Let short inference finish/persist even when the caller closes after done.
        if self.tasks:
            done, pending = await asyncio.wait(self.tasks, timeout=self.client.timeout + .05)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
