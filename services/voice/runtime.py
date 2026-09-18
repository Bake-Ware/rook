"""Conversation orchestration without model or web-framework dependencies."""
import asyncio
import contextlib
import json
import logging
import struct
import time
import uuid
import traceback


class Connection:
    def __init__(self, store, jobs, provider, session, send_json, send_bytes, protocol=2,
                 decision=None, feedback=None, thinking=False, conversation=None, activity=False, enqueue=None, progress_updates=None, identity=None):
        self.store, self.jobs, self.provider, self.session = store, jobs, provider, session
        self.send_json, self.send_bytes = send_json, send_bytes
        from .identity import Identity
        self.identity = identity or Identity()
        self.protocol = protocol
        self.epoch = 0
        self.task = None
        self.closed = False
        self.speak_out = True
        self.full_duplex = False
        self.voice = provider.default_voice
        self.play_until = 0.0
        self.pending_results = []
        self.receiving_speech = False
        self.last_speech = 0.0
        self.sleeping = False
        from .progress import ProgressUpdates
        self.progress = ProgressUpdates(self, progress_updates)
        self.audio_sent = {}
        self.audio_played = {}
        self.thinking = protocol == 2 and thinking is True
        self.enqueue = enqueue
        self.outbox = None
        self.outbox_task = None
        self.activity_enabled = protocol == 2 and activity is True
        self.activity = None
        if self.activity_enabled:
            from .activity import Activity
            self.activity = Activity(self.queue_event)
        self.decision = decision
        if self.thinking and self.decision is None:
            from .decision import DecisionClient
            self.decision = DecisionClient(url='')
        self.decision_turns = {}
        self.shadow = None
        if self.thinking and decision and decision.url and feedback:
            from .shadow import Shadow
            with contextlib.suppress(Exception):
                self.shadow = Shadow(self, decision, feedback, conversation or session)

    def queue_event(self, event):
        if self.closed:
            return
        if self.enqueue:
            self.enqueue(event)
            return
        # Standalone runtime/test consumers have the same non-blocking interface.
        if self.outbox is None:
            self.outbox = asyncio.Queue(maxsize=256)
            self.outbox_task = asyncio.create_task(self._send_outbox())
        self.outbox.put_nowait(event)

    async def _send_outbox(self):
        while True:
            event = await self.outbox.get()
            try:
                await asyncio.wait_for(self.send_json(event), 5)
            finally:
                self.outbox.task_done()

    def activity_event(self, phase, turn, label, **fields):
        if self.activity and not self.closed:
            self.activity.emit(phase, turn, label, **fields)

    def decision_event(self, turn, event=None, status=None, reason=None):
        pending = self.decision_turns.pop(turn, None)
        if pending is None or not self.thinking or self.closed:
            return
        if event is None:
            event = self.decision.event(pending['source'], turn, status or 'skipped')
            event['elapsed_ms'] = (time.monotonic() - pending['created']) * 1000
        if reason:
            event['detail'] = reason
            event['error'] = reason
        if event['engine_status'] != 'ok':
            event.pop('answers', None)
        self.queue_event(event)

    def dispatch_decision(self, turn):
        pending = self.decision_turns.get(turn)
        if pending is None or pending['started']:
            return
        if not self.decision.url:
            self.decision_event(turn, status='disabled', reason='Decision engine is disabled')
        elif pending['internal']:
            self.decision_event(turn, status='skipped', reason='Internal job-result narration')
        elif self.shadow and self.shadow.dispatch(turn):
            pending['started'] = True
        else:
            self.decision_event(turn, status='error', reason='Decision shadow could not start')

    def finish_decision(self, turn, reason):
        pending = self.decision_turns.get(turn)
        if pending and not pending['started']:
            status = 'disabled' if not self.decision.url else 'skipped'
            self.decision_event(turn, status=status,
                                reason='Decision engine is disabled' if status == 'disabled' else reason)
            if self.shadow:
                self.shadow.abandon(turn)

    def shadow_hook(self, name, *args):
        if self.shadow:
            try:
                getattr(self.shadow, name)(*args)
            except Exception as error:
                logging.warning('Decision shadow hook failed: %s', type(error).__name__)

    async def emit(self, kind, **data):
        if not self.closed:
            await asyncio.wait_for(self.send_json({"type": kind, **data}), 5)

    async def interrupt(self):
        if not self.closed:
            self.shadow_hook('interrupt', time.monotonic() < self.play_until,
                             bool(self.task and not self.task.done()))
        self.progress.cancel_speech()
        interrupted_turn = self.epoch
        self.epoch += 1
        old, self.task = self.task, None
        if old and old is not asyncio.current_task():
            old.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await old
        self.finish_decision(interrupted_turn, "Turn interrupted before reply dispatch")
        self.play_until = 0
        await self.emit("interrupt", turn=self.epoch)
        await self.emit("state", state="listening", turn=self.epoch)

    async def start(self, text=None, pcm=None, image=None, speak=True, internal=False):
        await self.interrupt()
        self.speak_out = speak
        if not internal:
            self.sleeping = False
        self.receiving_speech = False
        epoch = self.epoch
        if self.thinking:
            self.decision_turns[epoch] = {'source': 'voice' if pcm is not None else 'text',
                                          'created': time.monotonic(), 'started': False, 'internal': internal}
        self.task = asyncio.create_task(self._turn(epoch, text, pcm, image, internal))

    async def _turn(self, epoch, text, pcm, image, internal):
        from .identity import current_identity
        identity_token = current_identity.set(self.identity)
        started = time.monotonic()
        turn_status = 'ok'
        decision_reason = 'No reply was dispatched'
        try:
            await self.emit("state", state="thinking", turn=epoch)
            if pcm is not None:
                text = await asyncio.wait_for(self.provider.transcribe(pcm), 25)
                if not text:
                    return
                await self.emit("stt", text=text, turn=epoch)
            if not internal:
                self.activity_event('heard', epoch, 'Heard you', detail=text or 'Describe this image.')
                self.store.append(self.session, "user", {"text": (text or "Describe this image.") +
                                                         (" [image attached]" if image else "")})
            messages = [{"role": "system", "content": self.provider.system}] + self.store.messages(self.session)
            # Job state is supplied as tool data; it is not another user's instruction.
            jobs = self.store.jobs(self.session)
            if jobs:
                messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": "job_state",
                    "type": "function", "function": {"name": "job_status", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "job_state", "content": json.dumps(jobs)[:20000]}]
            if internal:
                messages.append({"role": "user", "content": "Report the newly finished job's actual outcome briefly: " + str(text)[:500]})
            elif image:
                messages.append({"role": "user", "content": [{"type": "text", "text": text or "Describe this image."},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]})
            # The model stream and playback consume separate bounded queues. A slow
            # synthesizer no longer holds up parsing tool-call fragments.
            clauses = asyncio.Queue(maxsize=8)
            planning_started = time.monotonic()
            planned = False
            self.activity_event('planning', epoch, 'Choosing a response')
            def on_activity(phase, **fields):
                nonlocal planned
                labels = {'planned': 'Plan ready', 'retry': 'Retrying the response',
                          'fallback': 'Asking you to repeat'}
                if phase == 'planned':
                    if planned:
                        return
                    planned = True
                    fields['elapsed_ms'] = int((time.monotonic() - planning_started) * 1000)
                elif phase == 'fallback':
                    planned = True
                self.activity_event(phase, epoch, labels[phase], **fields)
            async def on_clause(clause):
                if not planned:
                    on_activity('planned', tool='respond')
                await clauses.put(clause)
            async def producer():
                kwargs = {'reply_only': internal}
                if self.activity_enabled and getattr(self.provider, 'supports_activity', False):
                    kwargs['on_activity'] = on_activity
                result = await asyncio.wait_for(self.provider.chat(messages, on_clause, **kwargs), 60)
                if not planned:
                    calls = result[1]
                    on_activity('planned', tool=calls[0].get('function', {}).get('name', 'respond') if calls else 'respond')
                await clauses.put(None)
                return result
            producer_task = asyncio.create_task(producer())
            if not internal:
                self.shadow_hook('begin', text or 'Describe this image.',
                                 'voice' if pcm is not None else 'text', epoch)
            async def consumer():
                while True:
                    item = await clauses.get()
                    if item is None:
                        return
                    await self.say(item, epoch)
            consumer_task = asyncio.create_task(consumer())
            try:
                # Fail either side promptly: don't leave a consumer waiting forever
                # when the producer failed before enqueuing its sentinel.
                done, _ = await asyncio.wait([producer_task, consumer_task], return_when=asyncio.FIRST_EXCEPTION)
                for task in done:
                    task.result()
                content, calls = await producer_task
                await consumer_task
            finally:
                for task in (producer_task, consumer_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(producer_task, consumer_task, return_exceptions=True)
            for call in calls[:4]:
                function = call.get("function", {})
                name = function.get("name", "")
                args = json.loads(function.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("Invalid tool arguments")
                if name == "end_session":
                    await self.say("Talk to you later.", epoch)
                    self.sleeping = True
                    await self.emit("bye", mode="off" if args.get("mode") == "off" else "sleep",
                                    after_ms=max(0, int((self.play_until-time.monotonic())*1000))+300)
                elif name == "cancel_job":
                    result = self.jobs.cancel(self.session, args.get("id", ""))
                    await self.say(result, epoch)
                elif name == "job_status":
                    await self.say("Your job status is available in this conversation.", epoch)
                else:
                    jid = self.jobs.start(self.session, name, args, self.store.messages(self.session))
                    self.progress.start(jid, epoch, name, args)
                    if self.activity:
                        timeout = self.jobs.agent_timeout + 30 if name == 'delegate_to_hermes' else self.jobs.read_timeout
                        self.activity.start_job(jid, epoch, name, args, int(timeout * 1000))
                    await self.emit("tool", id=jid, title=name, status="running")
                    if not content:
                        await self.say("I'm working on that. You can keep talking while I check.", epoch)
        except asyncio.CancelledError:
            turn_status = 'cancelled'
            decision_reason = 'Turn interrupted before reply dispatch'
            raise
        except Exception as error:
            turn_status = 'failed'
            decision_reason = 'Turn failed before reply dispatch'
            self.activity_event('error', epoch, 'Voice turn failed', detail=(str(error) or type(error).__name__)[:500])
            logging.warning("Voice turn failed: %s at %s", type(error).__name__,
                            [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)])
            await self.emit("error", msg="Voice turn failed: " + type(error).__name__ + ". Please try again.")
        finally:
            current_identity.reset(identity_token)
            self.finish_decision(epoch, decision_reason)
            self.activity_event('done', epoch, 'Turn finished', status=turn_status,
                                elapsed_ms=int((time.monotonic()-started)*1000))
            if epoch == self.epoch and not self.closed:
                self.shadow_hook('completed', epoch)
                await self.emit("assistant_done", turn=epoch)
                await self.emit("state", state="listening", turn=epoch)
                await self.emit("metrics", turn=epoch, duration_ms=int((time.monotonic()-started)*1000))
                asyncio.get_running_loop().call_soon(self.drain_results)

    async def say(self, text, epoch, progress_job=None):
        if epoch != self.epoch or self.closed or not text.strip():
            return
        if progress_job is not None:
            # Synthesis can be slow. Recheck the live job and suppression before
            # publishing any text/audio; a completed result always wins.
            pcm, sr = await asyncio.wait_for(self.provider.synthesize(text, self.voice), 25)
            if (progress_job not in self.progress.jobs.values() or self.progress.suppressed() or
                    epoch != self.epoch):
                return False
        await self.emit("assistant_delta", text=text, turn=epoch)
        if progress_job is None:
            self.shadow_hook('reply', text, epoch)
        # Record generated speech honestly. Playback acknowledgements are tracked
        # separately; interruption must not make the model assume all of it was heard.
        speaking = self.speak_out or progress_job is not None
        record = text if not speaking else "[Spoken response generated; playback may be interrupted] " + text
        self.store.append(self.session, "assistant", {"text": record})
        # Reply is on the websocket path and history is committed before any
        # advisory request can begin. No shadow network or DB operation is awaited.
        if not speaking:
            self.dispatch_decision(epoch)
            return
        await self.emit("state", state="speaking", turn=epoch)
        self.activity_event('speaking', epoch, 'Speaking')
        if progress_job is None:
            pcm, sr = await asyncio.wait_for(self.provider.synthesize(text, self.voice), 25)
        await self.emit("audio_sr", sr=sr, turn=epoch)
        step = int(sr * .04) * 2
        for offset in range(0, len(pcm), step):
            if epoch != self.epoch or self.closed:
                return
            part = pcm[offset:offset+step]
            packet = b"RK2A" + struct.pack(">I", epoch) + part if self.protocol >= 2 else part
            await asyncio.wait_for(self.send_bytes(packet), 5)
            if offset == 0:
                if progress_job is None:
                    self.dispatch_decision(epoch)
                else:
                    fields = {'tool': progress_job['tool'], 'elapsed_ms': int((time.monotonic()-progress_job['started'])*1000)}
                    if progress_job['worker']:
                        fields['worker'] = progress_job['worker']
                    self.activity_event('progress', progress_job['turn'], text, **fields)
            if progress_job is None:
                self.progress.spoken(epoch)
            if self.shadow:
                self.shadow.last_spoke = time.monotonic()
            self.audio_sent[epoch] = self.audio_sent.get(epoch, 0) + len(part)//2
            self.play_until = max(self.play_until, time.monotonic()) + len(part)/(2*sr)
            await asyncio.sleep(len(part)/(2*sr))
        # Limit per-connection telemetry growth.
        for table in (self.audio_sent, self.audio_played):
            for old in list(table):
                if old < epoch - 8:
                    del table[old]

    async def say_progress(self, text, job):
        epoch = self.epoch
        result = await self.say(text, epoch, progress_job=job)
        if result is False:
            return False
        await self.emit('state', state='listening', turn=epoch)

    def job_event(self, event):
        if self.closed:
            return
        self.progress.event(event)
        if self.activity:
            self.activity.result(event)
        if event.get("status") != "running":
            self.pending_results.append(event)
            self.pending_results = self.pending_results[-20:]
        # Caller owns a bounded outbound event queue; no task per progress token.

    def drain_results(self):
        if self.closed or self.receiving_speech or (self.task and not self.task.done()) or not self.pending_results:
            return
        event = self.pending_results.pop(0)
        async def report():
            await self.start(text=event["id"] + " " + event["status"], speak=self.speak_out, internal=True)
        task = asyncio.create_task(report())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def close(self):
        self.closed = True
        await self.interrupt()
        await self.progress.close()
        if self.shadow:
            await self.shadow.close()
        if self.activity:
            await self.activity.close()
        if self.outbox_task:
            self.outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.outbox_task
