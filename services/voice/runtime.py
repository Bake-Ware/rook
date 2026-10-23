"""Conversation orchestration without model or web-framework dependencies."""
import asyncio
import contextlib
import difflib
import json
import logging
import os
import struct
import time
import uuid
import re
import traceback
from .identity import Identity, current_identity
from .modes import Mode, dictation_command
from .sounds import stage_directions
from .timing import TurnTiming, log_dropped
from .stt_gate import normalise


SILENT_RECORD = "[Stayed silent: speech was not addressed to the assistant]"
# A new utterance joins the pending turn (instead of replacing it) when that turn
# started from speech this recently and nothing of its reply was played yet:
# "what's the weather" ... "in Austin" becomes one question.
MERGE_WINDOW_S = float(os.environ.get('VOICE_MERGE_WINDOW_S', '10'))
# A held barge-in (client paused playback on local speech) resumes on its own when
# no utterance arrives in this long, and is forced back after HOLD_MAX_S.
HOLD_QUIET_S = float(os.environ.get('VOICE_HOLD_QUIET_S', '2.5'))
HOLD_MAX_S = float(os.environ.get('VOICE_HOLD_MAX_S', '8'))   # the phone gives up at 10 s
# 'enforce': drop an utterance whose speaker is the assistant's own voice (echo)
# while it speaks or just after; 'shadow': log the label only; 'off'.
# A reply does not start talking over the user: its first audio waits this long at
# most while an utterance is still coming in (it usually merges into the turn).
FLOOR_WAIT_S = float(os.environ.get('VOICE_FLOOR_WAIT_S', '3'))
ECHO_GUARD = os.environ.get('VOICE_ECHO_GUARD', 'enforce').strip().lower()
ECHO_WINDOW_S = 1.5
# The voice match must be clearly the assistant's: its own echo scored >= 0.59
# (5th percentile) through simulated phone/room channels, people at most 0.43
# (TitaNet small, kaiju, 2026-10-23). The speaker label itself uses 0.50.
ECHO_SPEAKER_MIN = float(os.environ.get('VOICE_ECHO_SPEAKER_MIN', '0.6'))
# Commands only at the start of an utterance ("hey sojourn, please enroll my voice"),
# so a sentence that merely mentions a voice never starts or deletes anything.
_LEAD = r"^\W*(?:(?:hey|ok|okay)\W+\w+\W+)?(?:(?:please|can you|could you|would you)\W+)?"
ENROLL_RE = re.compile(_LEAD + r"(?:enrol+|learn|register|remember)\s+my\s+voice\b", re.I)
FORGET_RE = re.compile(_LEAD + r"(?:forget|delete|remove|erase)\s+my\s+voice\b", re.I)
CANCEL_RE = re.compile(r"^\W*(stop|cancel|never ?mind|forget it|quit)\b", re.I)
# A scribed conversation ends after this long with nobody talking.
SCRIBE_IDLE_S = float(os.environ.get('VOICE_SCRIBE_IDLE_S', '300'))


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
        # Per turn: what was spoken, as (text, first sample, end sample) at the audio rate.
        self.spoken_audio = {}
        self.audio_rate = {}
        # Turn intake: utterances are transcribed one at a time, in order, before
        # they may interrupt anything (see heard()).
        self.intake_lock = asyncio.Lock()
        self.intake_pending = 0
        self.intake_tasks = set()
        self.turn_meta = {}
        # Confirmed barge-in (hello "barge": "confirm"): the client paused playback on
        # local speech; audio stops here too until the transcript decides.
        self.barge_confirm = False
        self.held_since = None
        self._unheld = asyncio.Event()
        self._unheld.set()
        self.speech_permission_until = 0.0
        self.speakers = None
        self.enrolling = None
        self.timing = None
        self.mode = Mode()
        # Set when the house voice switched into dictation or scribe: the mode to go back to.
        self.mode_return = None
        self.scribe_speakers = None
        self.scribe_idle = None
        self.handoff_tasks = set()
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
        if self.held_since is not None:
            # The client flushes on "interrupt"; nothing to resume.
            self.held_since = None
            self.speech_permission_until = 0.0
            self._unheld.set()
        await self.emit("interrupt", turn=self.epoch)
        await self.emit("state", state="listening", turn=self.epoch)

    async def start(self, text=None, pcm=None, image=None, speak=True, internal=False, heard=None):
        """``heard``: an utterance already transcribed by heard() (stt_ms, speech_end, ...)."""
        merged = self._mergeable() if heard is not None and text else None
        if merged is not None:
            text = merged['text'] + ' ' + text
        await self.interrupt()
        self.speak_out = speak
        if not internal:
            self.sleeping = False
        if heard is None:
            # A transcribed utterance starts after STT, maybe while the user already says
            # more: the audio loop owns receiving_speech then.
            self.receiving_speech = False
        epoch = self.epoch
        voice = pcm is not None or heard is not None
        if self.thinking or self.shadow:
            self.decision_turns[epoch] = {'source': 'voice' if voice else 'text',
                                          'created': time.monotonic(), 'started': False, 'internal': internal}
        if not internal:
            self.turn_meta[epoch] = {'text': text, 'voice': voice, 'started': time.monotonic(),
                                     'merged_from': merged['epoch'] if merged else None}
            for old in [t for t in self.turn_meta if t < epoch - 8]:
                del self.turn_meta[old]
        self.task = asyncio.create_task(self._turn(epoch, text, pcm, image, internal, heard, merged))

    def _mergeable(self):
        """The pending turn a new utterance should join, or None: a spoken turn still
        running, started within MERGE_WINDOW_S, with none of its reply played yet."""
        meta = self.turn_meta.get(self.epoch)
        if (meta is None or not meta['voice'] or not (self.task and not self.task.done()) or
                self.audio_sent.get(self.epoch, 0) or time.monotonic() - meta['started'] > MERGE_WINDOW_S):
            return None
        return {'epoch': self.epoch, 'text': meta['text']}

    # --- turn intake ----------------------------------------------------------
    def heard(self, pcm, speech_end=None, held=False):
        """A finished utterance. It is transcribed (and its speaker labelled) before it
        may touch the current turn, so silence, noise, Whisper's stock phrases and the
        assistant's own voice never cancel a reply. Utterances are handled in order."""
        self.intake_pending += 1
        task = asyncio.create_task(self._intake(pcm, speech_end or time.monotonic(), held, time.monotonic()))
        self.intake_tasks.add(task)
        task.add_done_callback(lambda t: (self.intake_tasks.discard(t), t.exception() if not t.cancelled() else None))
        return task

    async def _intake(self, pcm, speech_end, held, decided):
        try:
            async with self.intake_lock:
                if not self.closed:
                    await self._intake_one(pcm, speech_end, held, decided)
        finally:
            self.intake_pending -= 1
            if not self.intake_pending and not self.closed:
                asyncio.get_running_loop().call_soon(self.drain_results)

    async def _transcribe(self, pcm):
        if hasattr(self.provider, 'transcribe_detail'):
            return await self.provider.transcribe_detail(pcm)
        text = await self.provider.transcribe(pcm)
        return {'text': text or '', 'reason': None if text else 'empty', 'stats': {}}

    async def _identify(self, pcm):
        try:
            return await asyncio.wait_for(self.speakers.identify(pcm, assistant_voice=self.voice), 3)
        except Exception as error:
            logging.warning('Speaker identification failed: %s', type(error).__name__)
            return None

    def echo_window(self):
        """The assistant is speaking, held, or stopped speaking moments ago."""
        return self.held_since is not None or time.monotonic() < self.play_until + ECHO_WINDOW_S

    async def _intake_one(self, pcm, speech_end, held, decided):
        echo_window = self.echo_window()
        stt_started = time.monotonic()
        who = None
        if self.speakers is not None and getattr(self.speakers, 'available', False):
            who = asyncio.create_task(self._identify(pcm))
        try:
            result = await asyncio.wait_for(self._transcribe(pcm), 25)
        except Exception as error:
            if who:
                who.cancel()
            logging.warning("Voice turn failed: %s at %s", type(error).__name__,
                            [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)])
            if held:
                await self.release_hold(resume=True)
            await self.emit("error", msg="Voice turn failed: " + type(error).__name__ + ". Please try again.")
            return
        stt_ms = max(0, int((time.monotonic() - stt_started) * 1000))
        speaker = await who if who else None
        endpoint_ms = max(0, int((decided - speech_end) * 1000))
        extra = dict(result.get('stats') or {})
        if speaker:
            extra.update(speaker=speaker.get('label'), speaker_score=speaker.get('score'),
                         assistant_score=speaker.get('assistant_score'))
        reason = result.get('reason')
        if not reason and not result.get('text', '').strip():
            reason = 'empty'
        if not reason and echo_window and ECHO_GUARD != 'off':
            if self.echo_text(result['text']):
                # Our own words came back (the transcript repeats what was just said).
                reason = 'echo_text'
            elif (ECHO_GUARD == 'enforce' and speaker and speaker.get('label') == 'assistant'
                  and (speaker.get('assistant_score') or 0) >= ECHO_SPEAKER_MIN):
                reason = 'echo'
        known = getattr(self.speakers, 'has_assistant_voice', None)
        if speaker and known is not None and not known(self.voice):
            self._learn_voice()
        if reason:
            log_dropped(self.epoch, reason, endpoint_ms, stt_ms, held=held or None, **extra)
            self.activity_event('dropped', self.epoch, 'Ignored sound', reason=reason)
            if held:
                await self.release_hold(resume=True)
            return
        text = result['text'].strip()
        if await self._voice_command(text, pcm, held):
            return
        await self.start(text=text, heard={'speech_end': speech_end, 'decided': decided,
                                           'stt_ms': stt_ms, 'extra': extra, 'speaker': speaker})

    def echo_text(self, text):
        """The transcript repeats a run of what the assistant said in this or the
        previous turn: its own speech leaking back into the microphone."""
        words = normalise(text).split()
        if len(words) < 3 or ENROLL_RE.search(text) or FORGET_RE.search(text):
            # Short answers often reuse the question's words; commands it just taught are meant.
            return False
        spoken = ' '.join(clause for epoch in (self.epoch - 1, self.epoch)
                          for clause, _, _ in self.spoken_audio.get(epoch, []))
        said = normalise(spoken).split()
        if not said:
            return False
        match = difflib.SequenceMatcher(None, words, said, autojunk=False).find_longest_match(
            0, len(words), 0, len(said))
        return match.size >= max(3, int(0.6 * len(words) + 0.5))

    def _learn_voice(self):
        """Once per voice (cached by the speaker store): render the assistant's voice
        so its echo can be recognised."""
        voice = self.voice
        async def synthesize(text):
            return await self.synthesize(text, self.epoch)
        task = asyncio.create_task(self.speakers.learn_assistant_voice(voice, synthesize))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    # --- voice enrollment (opt-in, owner keys only; voiceprints stay on this host) ---
    async def _voice_command(self, text, pcm, held):
        """'enroll my voice' / 'forget my voice', and the enrollment reading itself.
        Returns True when the utterance was handled here (no model turn)."""
        speakers = self.speakers if self.speakers is not None and getattr(self.speakers, 'available', False) else None
        if not self.mode.agent:
            return False
        if self.enrolling is not None:
            if time.monotonic() - self.enrolling['started'] > 300 or CANCEL_RE.search(text):
                who = self.enrolling
                self.enrolling = None
                if who.get('other'):
                    self.report('enroll_failed', {'name': who['principal'], 'reason': 'stopped'})
                await self._reply_direct(text, ['Okay, I stopped learning ' + ("that voice" if who.get('other')
                                                                              else 'your voice') +
                                                '. Nothing new was saved.'])
                return True
            principal = self.enrolling['principal']
            result = await speakers.enroll_add(principal, pcm) if speakers else {'ok': False, 'reason': 'off'}
            sentences = speakers.enrollment_sentences() if speakers else []
            if result.get('ok') and result.get('samples', 0) >= result.get('needed', 3) and \
                    speakers.enroll_finish(principal):
                other = self.enrolling.get('other')
                self.enrolling = None
                if other:
                    self.report('enrolled', {'name': principal, 'samples': str(result.get('samples', 0))})
                    await self._reply_direct(text, [f"Thanks, {principal}, I've learned your voice. I only use it "
                                                    "to know who's talking, never as a password."])
                    return True
                await self._reply_direct(text, ["Thanks, I've learned your voice. I only use it to know who's "
                                                "talking, never as a password. Say forget my voice to delete it."])
                return True
            if result.get('ok'):
                self.enrolling['index'] += 1
            lines = [] if result.get('ok') else [{
                'too_short': 'That was a bit short.', 'too_quiet': 'That was a bit quiet.',
                'clipped': 'That was a bit loud.', 'inconsistent': "That didn't sound like the earlier ones.",
            }.get(result.get('reason'), "That one didn't work.") + ' Please try this one again.']
            if not sentences:
                self.enrolling = None
                await self._reply_direct(text, ["Voice learning isn't available right now."])
                return True
            lines.append('Next, please read: ' if result.get('ok') else 'Please read: ')
            lines[-1] += sentences[self.enrolling['index'] % len(sentences)]
            await self._reply_direct(text, lines)
            return True
        if FORGET_RE.search(text):
            if not self.identity.owner or speakers is None:
                return False
            gone = speakers.forget(self.identity.principal)
            await self._reply_direct(text, ['Done, I deleted your voiceprint.' if gone else
                                            "I didn't have your voice saved."])
            return True
        if ENROLL_RE.search(text):
            if speakers is None:
                return False
            if not self.identity.owner or self.identity.principal in ('unmapped', 'guest', 'assistant'):
                # Kids and guests only with the owner's say-so: an owner key enrolls its own person.
                await self._reply_direct(text, ['Learning a voice needs the owner to set it up for you, so I '
                                                "won't do that from this device."])
                return True
            self.enrolling = {'principal': self.identity.principal, 'index': 0, 'started': time.monotonic()}
            await self._reply_direct(text, ["Sure. I'll ask you to read three short sentences, one at a time. "
                                            "Say stop to cancel.",
                                            'Please read: ' + speakers.enrollment_sentences()[0]])
            return True
        return False

    async def _reply_direct(self, heard, lines):
        """A fixed spoken reply without the model (enrollment); recorded in history."""
        await self.interrupt()
        epoch = self.epoch
        await self.emit("stt", text=heard, turn=epoch)
        self.store.append(self.session, "user", {"text": heard})
        async def speak():
            for line in lines:
                await self.say(line, epoch, record=True)
            if epoch == self.epoch and not self.closed:
                await self.emit("assistant_done", turn=epoch)
                await self.emit("state", state="listening", turn=epoch)
        self.task = asyncio.create_task(speak())

    # --- confirmed barge-in -----------------------------------------------------
    def hold(self):
        """The client paused playback for possible speech: stop sending audio until
        the utterance's transcript decides (interrupt) or it was nothing (resume)."""
        if self.held_since is None:
            self.held_since = time.monotonic()
            self._unheld.clear()

    async def release_hold(self, resume=True):
        if self.held_since is None:
            return
        paused = time.monotonic() - self.held_since
        self.held_since = None
        self.speech_permission_until = 0.0
        if self.play_until:
            # The client's playback stood still while held.
            self.play_until += paused
        self._unheld.set()
        if self.fb is not None:
            self.fb._poke()
        if resume:
            await self.emit("resume", turn=self.epoch)

    async def _wait_unheld(self, epoch):
        try:
            await asyncio.wait_for(self._unheld.wait(), HOLD_MAX_S)
        except asyncio.TimeoutError:
            if epoch != self.epoch or self.held_since is None:
                return
            if self.receiving_speech or self.intake_pending:
                # Still talking after HOLD_MAX_S: a real barge-in (the phone gives up at 10 s).
                await self.interrupt()
            else:
                await self.release_hold(resume=True)

    def check_hold(self, utterance_open):
        """From the audio loop: resume a hold that no utterance followed."""
        if self.held_since is None or utterance_open or self.intake_pending:
            return None
        if time.monotonic() - max(self.held_since, self.last_speech) > HOLD_QUIET_S:
            return self.release_hold(resume=True)
        return None

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

    async def _turn(self, epoch, text, pcm, image, internal, heard=None, merged=None):
        identity_token = current_identity.set(self.identity)
        started = time.monotonic()
        source = 'internal' if internal else 'voice' if pcm is not None or heard is not None else 'text'
        if heard is not None:
            # Measured from the end of speech: endpointing, then STT, then this turn.
            timing = self.timing = TurnTiming(epoch, source, heard['decided'], heard['speech_end'])
            timing.marks['stt_ms'] = heard['stt_ms']
            timing.extra.update({k: v for k, v in heard.get('extra', {}).items() if v is not None})
            if merged:
                timing.extra['merged'] = merged['epoch']
        else:
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
                    if self.mode.id == 'scribe':
                        await self._scribe(text.strip(), epoch, heard)
                    else:
                        await self._dictate(text.strip(), epoch)
                return
            if heard is not None:
                await self.emit("stt", text=text, turn=epoch)
            if not internal:
                self.activity_event('heard', epoch, 'Heard you', detail=text or 'Describe this image.')
                entry = (text or "Describe this image.") + (" [image attached]" if image else "")
                if merged:
                    # The earlier part was recorded already; one user line for both.
                    self.store.amend_last_user(self.session, entry)
                else:
                    self.store.append(self.session, "user", {"text": entry})
            if self.fb is not None and not internal and not image:
                # Fast Front reply plus a parallel Background worker; no planner call here.
                decision_reason = 'front_background pipeline'
                await self.fb.turn(epoch, text, timing, merged_from=merged['epoch'] if merged else None)
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
        # Sound tags read as stage directions on screen ("(laughs)"); the voice performs them.
        await self.emit("assistant_delta", text=stage_directions(text), turn=epoch)
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
        if progress_job is None:
            first = self.audio_sent.get(epoch, 0)
            self.audio_rate[epoch] = sr
            self.spoken_audio.setdefault(epoch, []).append((text, first, first + len(pcm) // 2))
        if progress_job is None and not self.audio_sent.get(epoch):
            await self._wait_floor(epoch)
        step = int(sr * .04) * 2
        for offset in range(0, len(pcm), step):
            if self.held_since is not None:
                await self._wait_unheld(epoch)
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
        for table in (self.audio_sent, self.audio_played, self.spoken_audio, self.audio_rate):
            for old in list(table):
                if old < epoch - 8:
                    del table[old]

    async def _wait_floor(self, epoch):
        """The user is still talking (or their words are being transcribed): hold the
        reply's first audio until they finish, at most FLOOR_WAIT_S."""
        end = time.monotonic() + FLOOR_WAIT_S
        while ((self.receiving_speech or self.intake_pending) and epoch == self.epoch and not self.closed
               and time.monotonic() < end):
            await asyncio.sleep(.02)

    def played_samples(self, epoch):
        """Samples of this turn's speech the listener has heard: the client's playback
        acknowledgement when it sends them, else what was sent minus what is still
        due to play."""
        if epoch in self.audio_played:
            return self.audio_played[epoch]
        sent = self.audio_sent.get(epoch, 0)
        # interrupt() cancels the turn before it clears play_until, so this still holds.
        ahead = max(0.0, self.play_until - time.monotonic())
        return max(0, sent - int(ahead * self.audio_rate.get(epoch, 24000)))

    def heard_text(self, epoch, clauses):
        """What of ``clauses`` (said in turn ``epoch``) was actually heard, cut at the
        interruption: whole clauses that played, then the played share of the next
        one's words, marked as cut off. Unspoken clauses count as unheard."""
        played = self.played_samples(epoch)
        spans = {}
        for text, first, end in self.spoken_audio.get(epoch, []):
            spans.setdefault(text, []).append((first, end))
        out, cut = [], False
        for clause in clauses:
            span = spans.get(clause, [])
            if not span:
                cut = True
                break
            first, end = span.pop(0)
            if played >= end:
                out.append(clause)
                continue
            if played > first:
                words = clause.split()
                n = int(len(words) * (played - first) / max(1, end - first))
                if n:
                    out.append(' '.join(words[:n]))
            cut = True
            break
        text = ' '.join(out).strip()
        if cut:
            text = (text + ' [cut off]') if text else ''
        return text

    def _tools(self):
        """Tools offered this turn: the mode's set narrowed by the credential's."""
        by_mode, by_identity = self.mode.tools, self.identity.tools()
        if by_mode is None or by_identity is None:
            return by_identity if by_mode is None else by_mode
        return set(by_mode) & set(by_identity)

    async def set_mode(self, mode, by=None):
        """``by='voice'``: the house voice switched it (the client accepts that only
        between modes.VOICE_SWITCHABLE); a client's own switch clears the way back."""
        self.mode = mode
        if by != 'voice':
            self.mode_return = None
        await self.emit("mode", mode=mode.id, custom=mode.custom, **({'by': by} if by else {}))
        self.drain_results()

    def report(self, op, args):
        """Tell the hub (through the house voice's lane) what happened here: a voice
        learned or forgotten, a scribed line. Nothing without the house voice."""
        house = self.fb.house if self.fb is not None else None
        if house is None:
            return
        task = asyncio.create_task(house.control(op, args))
        self.handoff_tasks.add(task)
        task.add_done_callback(lambda t: (self.handoff_tasks.discard(t), t.exception() if not t.cancelled() else None))

    # --- scribe: an overheard conversation (the hub keeps the transcript) -------
    async def _scribe(self, text, epoch, heard):
        """No reply: who said what, and when, goes to the hub. Saying the assistant's
        name ends it (and is answered); so does a quiet spell."""
        from .speaker import SessionSpeakers
        name = os.environ.get("ROOK_VOICE_ASSISTANT_NAME", "").strip() or "Rook"
        if re.search(rf"\b{re.escape(name)}\b", text, re.I):
            await self.scribe_end(then=text)
            return
        if self.scribe_speakers is None:
            self.scribe_speakers = SessionSpeakers()
        label = self.scribe_speakers.label((heard or {}).get('speaker'))
        self.report('scribe', {'text': text, 'speaker': label, 'at': f'{time.time():.3f}'})
        self._arm_scribe_idle()

    def _arm_scribe_idle(self):
        if self.scribe_idle is not None:
            self.scribe_idle.cancel()

        async def idle():
            await asyncio.sleep(SCRIBE_IDLE_S)
            if not self.closed and self.mode.id == 'scribe':
                await self.scribe_end()
        self.scribe_idle = asyncio.create_task(idle())
        self.scribe_idle.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def scribe_end(self, then=None):
        """Back to the mode before (the house voice's), the hub told the conversation ended;
        ``then``: what was just said (the assistant's name) becomes a normal turn."""
        if self.scribe_idle is not None and self.scribe_idle is not asyncio.current_task():
            self.scribe_idle.cancel()
        self.scribe_idle = None
        self.scribe_speakers = None
        self.report('scribe', {'end': 'true'})
        back = self.mode_return or Mode()
        self.mode_return = None
        await self.set_mode(back, by='voice')
        if then:
            current = self.task

            async def handoff():
                if current is not None and current is not asyncio.current_task():
                    with contextlib.suppress(BaseException):
                        await current
                if not self.closed:
                    await self.start(text=then, speak=self.speak_out)
            task = asyncio.create_task(handoff())
            self.handoff_tasks.add(task)
            task.add_done_callback(lambda t: (self.handoff_tasks.discard(t), t.exception() if not t.cancelled() else None))

    def _dictation_done(self, body):
        """A dictation the house voice started is finished: back to the mode it came
        from, and the text goes to the voice, which offers to save or send it."""
        back, self.mode_return = self.mode_return, None
        current = self.task

        async def handoff():
            if current is not None:
                with contextlib.suppress(BaseException):
                    await current
            if self.closed:
                return
            await self.set_mode(back, by='voice')
            if body:
                await self.start(text='[dictation] ' + body[:8000], speak=self.speak_out)
        task = asyncio.create_task(handoff())
        self.handoff_tasks.add(task)
        task.add_done_callback(lambda t: (self.handoff_tasks.discard(t), t.exception() if not t.cancelled() else None))

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
            if final and self.mode_return is not None:
                self._dictation_done("")
        elif final and self.mode_return is not None:
            await self.emit("assistant_delta", text=body, turn=epoch)
            self._dictation_done(body)
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
                or self.intake_pending or not self.pending_results:
            return
        event = self.pending_results.pop(0)
        async def report():
            await self.start(text=event["id"] + " " + event["status"], speak=self.speak_out, internal=True)
        task = asyncio.create_task(report())
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

    async def close(self):
        self.closed = True
        for task in [*self.intake_tasks, *self.handoff_tasks, *([self.scribe_idle] if self.scribe_idle else [])]:
            task.cancel()
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
