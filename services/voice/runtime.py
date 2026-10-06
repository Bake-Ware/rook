"""Conversation orchestration without model or web-framework dependencies."""
import asyncio
import contextlib
import json
import logging
import struct
import time
import uuid
import re
import traceback
from .identity import Identity, current_identity
from .modes import Mode, dictation_command
from .timing import TurnTiming


SILENT_RECORD = "[Stayed silent: speech was not addressed to the assistant]"


class Connection:
    def __init__(self, store, jobs, provider, session, send_json, send_bytes, protocol=2,
                 decision=None, feedback=None, thinking=False, conversation=None, activity=False, enqueue=None, progress_updates=None, identity=None,
                 gate_threshold=None):
        self.store, self.jobs, self.provider, self.session = store, jobs, provider, session
        self.send_json, self.send_bytes = send_json, send_bytes
        self.identity = identity or Identity()
        self.protocol = protocol
        self.epoch = 0
        self.task = None
        self.closed = False
        self.speak_out = True
        self.full_duplex = False
        self.voice = provider.default_voice
        self.tts_fallback_reported = False
        self.play_until = 0.0
        self.pending_results = []
        self.receiving_speech = False
        self.last_speech = 0.0
        self.sleeping = False
        from .progress import ProgressUpdates
        self.progress = ProgressUpdates(self, progress_updates)
        self.audio_sent = {}
        self.audio_played = {}
        self.timing = None
        self.mode = Mode()
        # 'classic' (default) or 'front_background' (hello opt-in); see pipeline.py.
        self.pipeline = 'classic'
        self.fb = None
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
        if gate_threshold is None:
            from .decision import gate_threshold as configured
            gate_threshold = configured()
        # The reply gate needs decisions (and their records) on every connection, opted-in or not.
        self.gate_threshold = gate_threshold if decision and decision.url and feedback else None
        if (self.thinking or self.gate_threshold is not None) and decision and decision.url and feedback:
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
        if self.thinking or self.shadow:
            self.decision_turns[epoch] = {'source': 'voice' if pcm is not None else 'text',
                                          'created': time.monotonic(), 'started': False, 'internal': internal}
        self.task = asyncio.create_task(self._turn(epoch, text, pcm, image, internal))

    async def _gate(self, text, epoch):
        """Pre-reply needs_response check for a voice turn. Returns (began, gate): gate is set only
        when the engine answered in time and judged the speech not addressed. Fails open."""
        try:
            future = self.shadow.begin(text, 'voice', epoch, immediate=True)
        except Exception as error:
            logging.warning('Decision gate could not start: %s', type(error).__name__)
            return False, None
        if future is None:
            return True, None
        try:
            event = await asyncio.wait_for(asyncio.shield(future), self.decision.timeout + .05)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            if asyncio.current_task().cancelling():
                raise
            return True, None
        if event.get('status') != 'ok':
            return True, None
        p = next((a['p'] for a in event.get('answers', []) if a['id'] == 'needs_response'), None)
        if p is None or p >= self.gate_threshold:
            return True, None
        return True, {'p': p, 'threshold': self.gate_threshold, 'answers': event.get('answers', [])}

    async def _turn(self, epoch, text, pcm, image, internal):
        identity_token = current_identity.set(self.identity)
        started = time.monotonic()
        source = 'internal' if internal else 'voice' if pcm is not None else 'text'
        timing = self.timing = TurnTiming(epoch, source, started,
                                          self.last_speech if source == 'voice' and self.last_speech else None)
        turn_status = 'ok'
        decision_reason = 'No reply was dispatched'
        try:
            await self.emit("state", state="thinking", turn=epoch)
            if pcm is not None:
                stt_started = time.monotonic()
                text = await asyncio.wait_for(self.provider.transcribe(pcm), 25)
                timing.span('stt_ms', stt_started)
                if not text:
                    return
                await self.emit("stt", text=text, turn=epoch)
            if not internal and not self.mode.uses_model:
                if image:
                    await self.emit("error", msg="Images are not used in dictation mode.")
                if text and text.strip():
                    await self._dictate(text.strip(), epoch)
                return
            if not internal:
                self.activity_event('heard', epoch, 'Heard you', detail=text or 'Describe this image.')
                self.store.append(self.session, "user", {"text": (text or "Describe this image.") +
                                                         (" [image attached]" if image else "")})
            if self.fb is not None and not internal and not image:
                # Fast Front reply plus a parallel Background worker; no planner call here.
                decision_reason = 'front_background pipeline'
                await self.fb.turn(epoch, text, timing)
                return
            # The planner appends the personal-data policy (agent modes only).
            system = self.mode.system(self.provider.system)
            messages = [{"role": "system", "content": system}] + self.store.messages(self.session)
            # Job state is supplied as tool data; it is not another user's instruction.
            jobs = self.store.jobs(self.session) if self.mode.agent else []
            if jobs:
                messages += [{"role": "assistant", "content": None, "tool_calls": [{"id": "job_state",
                    "type": "function", "function": {"name": "job_status", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "job_state", "content": json.dumps(jobs)[:20000]}]
            if internal:
                messages.append({"role": "user", "content": "Report the newly finished job's actual outcome briefly: " + str(text)[:500]})
            elif image:
                messages.append({"role": "user", "content": [{"type": "text", "text": text or "Describe this image."},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]})
            began, gate = False, None
            if pcm is not None and not internal and self.gate_threshold is not None and self.shadow:
                began, gate = await self._gate(text, epoch)
            # The model stream and playback consume separate bounded queues. A slow
            # synthesizer no longer holds up parsing tool-call fragments.
            allowed = self._tools()
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
                    timing.mark('plan_ms')
                    timing.span('llm_ms', planning_started)
                elif phase == 'fallback':
                    planned = True
                self.activity_event(phase, epoch, labels[phase], **fields)
            async def on_clause(clause):
                if not planned:
                    on_activity('planned', tool='respond')
                await clauses.put(clause)
            async def producer():
                kwargs = {'reply_only': internal}
                if allowed is not None:
                    kwargs['tools'] = allowed
                if not self.mode.agent:
                    kwargs['identity_prompt'] = ''
                if self.activity_enabled and getattr(self.provider, 'supports_activity', False):
                    kwargs['on_activity'] = on_activity
                if gate:
                    kwargs['gate'] = {'p': gate['p']}
                result = await asyncio.wait_for(self.provider.chat(messages, on_clause, **kwargs), 60)
                if not planned:
                    calls = result[1]
                    on_activity('planned', tool=calls[0].get('function', {}).get('name', 'respond') if calls else 'respond')
                await clauses.put(None)
                return result
            producer_task = asyncio.create_task(producer())
            if not internal and not began:
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
            if gate:
                chosen = [call.get('function', {}).get('name', '') for call in calls] or ['respond']
                gate_value = {'p': gate['p'], 'threshold': gate['threshold'], 'model_choice': chosen}
                if chosen == ['stay_silent']:
                    self.shadow_hook('gate_outcome', epoch, 'gate_silenced', gate_value)
                else:
                    # The model judged the engine wrong: flag the decision and keep it for retraining.
                    logging.info('Decision gate overridden by model (p=%.3f, choice=%s)', gate['p'], chosen)
                    self.shadow_hook('gate_outcome', epoch, 'gate_override', gate_value, {
                        'answers': gate['answers'], 'label': {'needs_response': True},
                        'label_source': 'model_override', 'model_choice': chosen, 'reply': (content or '')[:1000],
                        'threshold': gate['threshold'], 'p_needs_response': gate['p']})
            for call in calls[:4]:
                function = call.get("function", {})
                name = function.get("name", "")
                args = json.loads(function.get("arguments") or "{}")
                if not isinstance(args, dict):
                    raise ValueError("Invalid tool arguments")
                if name == "stay_silent":
                    # History keeps alternating roles; the model sees it chose not to answer.
                    self.store.append(self.session, "assistant", {"text": SILENT_RECORD})
                    self.dispatch_decision(epoch)
                    continue
                if not self.mode.allows(name):
                    # The model was not offered this tool; never act on it.
                    if not content:
                        await self.say("I can't do that in this mode.", epoch)
                    continue
                if allowed is not None and name not in allowed:
                    if not content:
                        await self.say("That needs an owner voice key.", epoch)
                    continue
                if name == "end_session":
                    await self.say("See ya!", epoch)
                    self.sleeping = True
                    await self.emit("bye", mode="off" if args.get("mode") == "off" else "sleep",
                                    after_ms=max(0, int((self.play_until-time.monotonic())*1000))+300)
                elif name == "cancel_job":
                    result = self.jobs.cancel(self.session, args.get("id", ""))
                    await self.say(result, epoch)
                elif name == "job_status":
                    await self.say("Your job status is available in this conversation.", epoch)
                else:
                    if name in ('delegate_to_hermes', 'escalate') and not self.identity.owner:
                        await self.say('An owner voice key is required for agent work and changes.', epoch)
                        continue
                    # Keep the current image available to escalated work as well.
                    jid = self.jobs.start(self.session, name, args, messages[1:])
                    self.progress.start(jid, epoch, name, args)
                    if self.activity:
                        timeout = self.jobs.timeout(name)
                        self.activity.start_job(jid, epoch, name, args, int(timeout * 1000))
                    await self.emit("tool", id=jid, title=name, status="running")
                    if not content:
                        await self.say("Working.", epoch)
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
            timing.front_status = turn_status
            # front_background logs the line once Background (and its follow-up) finish.
            timed = timing.finish(turn_status, emit=not getattr(timing, 'deferred', False))
            if self.timing is timing:
                self.timing = None
            self.activity_event('done', epoch, 'Turn finished', status=turn_status,
                                elapsed_ms=int((time.monotonic()-started)*1000), timing=timed)
            if epoch == self.epoch and not self.closed:
                self.shadow_hook('completed', epoch)
                await self.emit("assistant_done", turn=epoch)
                await self.emit("state", state="listening", turn=epoch)
                await self.emit("metrics", turn=epoch, duration_ms=int((time.monotonic()-started)*1000))
                asyncio.get_running_loop().call_soon(self.drain_results)

    async def synthesize(self, text, epoch):
        """One clause in this session's voice. A Chatterbox failure is spoken by Kokoro
        instead (inside the provider); the session is told once."""
        if not hasattr(self.provider, 'resolve_voice'):
            return await self.provider.synthesize(text, self.voice)
        failures = []
        result = await self.provider.synthesize(text, self.voice, on_fallback=failures.append)
        if failures and not self.tts_fallback_reported and not self.closed:
            self.tts_fallback_reported = True
            self.activity_event('error', epoch, 'Voice engine failed; speaking with Kokoro',
                                detail=(str(failures[0]) or type(failures[0]).__name__)[:300])
            await self.emit('error', code='tts_fallback',
                            msg='The selected voice engine failed, so the Kokoro voice is speaking instead.')
        return result

    async def say(self, text, epoch, progress_job=None, record=True, on_audio=None):
        if epoch != self.epoch or self.closed or not text.strip():
            return
        if progress_job is not None:
            # Synthesis can be slow. Recheck the live job and suppression before
            # publishing any text/audio; a completed result always wins.
            pcm, sr = await asyncio.wait_for(self.synthesize(text, epoch), 25)
            if (progress_job not in self.progress.jobs.values() or self.progress.suppressed() or
                    epoch != self.epoch):
                return False
        await self.emit("assistant_delta", text=text, turn=epoch)
        timing = self.timing if progress_job is None and self.timing and self.timing.turn == epoch else None
        if timing:
            timing.mark('first_text_ms')
        if progress_job is None:
            self.shadow_hook('reply', text, epoch)
        # Record generated speech honestly. Playback acknowledgements are tracked
        # separately; interruption must not make the model assume all of it was heard.
        # Dictation mode speaks only read-backs and confirmations: none of it is
        # conversation, and read-back would copy the dictated text into model history.
        speaking = self.speak_out or progress_job is not None
        if self.mode.uses_model and record and (progress_job is None or self.mode.agent):
            entry = text if not speaking else "[Spoken response generated; playback may be interrupted] " + text
            self.store.append(self.session, "assistant", {"text": entry})
        # Reply is on the websocket path and history is committed before any
        # advisory request can begin. No shadow network or DB operation is awaited.
        if not speaking:
            self.dispatch_decision(epoch)
            return
        await self.emit("state", state="speaking", turn=epoch)
        self.activity_event('speaking', epoch, 'Speaking')
        if progress_job is None:
            pcm, sr = await asyncio.wait_for(self.synthesize(text, epoch), 25)
        await self.emit("audio_sr", sr=sr, turn=epoch)
        step = int(sr * .04) * 2
        for offset in range(0, len(pcm), step):
            if epoch != self.epoch or self.closed:
                return
            part = pcm[offset:offset+step]
            packet = b"RK2A" + struct.pack(">I", epoch) + part if self.protocol >= 2 else part
            await asyncio.wait_for(self.send_bytes(packet), 5)
            if offset == 0:
                if timing:
                    timing.mark('first_audio_ms')
                if on_audio:
                    on_audio()
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

    def _tools(self):
        """Tools offered this turn: the mode's set narrowed by the credential's."""
        by_mode, by_identity = self.mode.tools, self.identity.tools()
        if by_mode is None or by_identity is None:
            return by_identity if by_mode is None else by_mode
        return set(by_mode) & set(by_identity)

    async def set_mode(self, mode):
        self.mode = mode
        await self.emit("mode", mode=mode.id, custom=mode.custom)
        self.drain_results()

    async def _dictate(self, text, epoch):
        """Dictation never calls the model: record speech, act only on explicit commands."""
        command = dictation_command(text)
        if command in ("undo", "clear"):
            self.store.drop_dictation(self.session, last=command == "undo")
            await self.say("Removed the last part." if command == "undo" else "Cleared.", epoch)
            return
        if command is None:
            if not self.store.add_dictation(self.session, text[:16000]):
                await self.emit("error", msg="Dictation is full. Say \"I'm done\" to collect it, or \"start over\".")
                await self.say("Dictation is full. Say I'm done to collect it.", epoch)
            return
        segments = self.store.dictation(self.session)
        body = " ".join(part for _, part in segments)
        final = command == "finish"
        await self.emit("dictation", text=body, final=final, turn=epoch)
        if final and segments:
            # The client now holds the finished text; the next dictation starts fresh.
            self.store.drop_dictation(self.session, through=segments[-1][0])
        if not body:
            await self.say("Nothing has been dictated yet.", epoch)
        elif command == "read":
            # Synthesise sentence by sentence so long dictation stays within TTS limits.
            for sentence in re.split(r"(?<=[.!?])\s+", body):
                await self.say(sentence, epoch)
        else:
            await self.emit("assistant_delta", text=body, turn=epoch)
            await self.say("That's your dictation. The next one starts fresh.", epoch)
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
        # Job reports wait for assistant mode; they are persisted and reported on switching back.
        if self.closed or not self.mode.agent or self.receiving_speech or (self.task and not self.task.done()) \
                or not self.pending_results:
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
        if self.fb:
            await self.fb.close()
        if self.shadow:
            await self.shadow.close()
        if self.activity:
            await self.activity.close()
        if self.outbox_task:
            self.outbox_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.outbox_task
