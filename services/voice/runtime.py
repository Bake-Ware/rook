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
    def __init__(self, store, jobs, provider, session, send_json, send_bytes, protocol=2):
        self.store, self.jobs, self.provider, self.session = store, jobs, provider, session
        self.send_json, self.send_bytes = send_json, send_bytes
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
        self.audio_sent = {}
        self.audio_played = {}

    async def emit(self, kind, **data):
        if not self.closed:
            await asyncio.wait_for(self.send_json({"type": kind, **data}), 5)

    async def interrupt(self):
        self.epoch += 1
        old, self.task = self.task, None
        if old and old is not asyncio.current_task():
            old.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await old
        self.play_until = 0
        await self.emit("interrupt", turn=self.epoch)
        await self.emit("state", state="listening", turn=self.epoch)

    async def start(self, text=None, pcm=None, image=None, speak=True, internal=False):
        await self.interrupt()
        self.speak_out = speak
        self.receiving_speech = False
        epoch = self.epoch
        self.task = asyncio.create_task(self._turn(epoch, text, pcm, image, internal))

    async def _turn(self, epoch, text, pcm, image, internal):
        started = time.monotonic()
        try:
            await self.emit("state", state="thinking", turn=epoch)
            if pcm is not None:
                text = await asyncio.wait_for(self.provider.transcribe(pcm), 25)
                if not text:
                    return
                await self.emit("stt", text=text, turn=epoch)
            if not internal:
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
            async def on_clause(clause):
                await clauses.put(clause)
            async def producer():
                result = await asyncio.wait_for(self.provider.chat(messages, on_clause, reply_only=internal), 60)
                await clauses.put(None)
                return result
            producer_task = asyncio.create_task(producer())
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
                    await self.emit("bye", mode="off" if args.get("mode") == "off" else "sleep",
                                    after_ms=max(0, int((self.play_until-time.monotonic())*1000))+300)
                elif name == "cancel_job":
                    result = self.jobs.cancel(self.session, args.get("id", ""))
                    await self.say(result, epoch)
                elif name == "job_status":
                    await self.say("Your job status is available in this conversation.", epoch)
                else:
                    jid = self.jobs.start(self.session, name, args, self.store.messages(self.session))
                    await self.emit("tool", id=jid, title=name, status="running")
                    if not content:
                        await self.say("I'm working on that. You can keep talking while I check.", epoch)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logging.warning("Voice turn failed: %s at %s", type(error).__name__,
                            [(frame.name, frame.lineno) for frame in traceback.extract_tb(error.__traceback__)])
            await self.emit("error", msg="Voice turn failed: " + type(error).__name__ + ". Please try again.")
        finally:
            if epoch == self.epoch and not self.closed:
                await self.emit("assistant_done", turn=epoch)
                await self.emit("state", state="listening", turn=epoch)
                await self.emit("metrics", turn=epoch, duration_ms=int((time.monotonic()-started)*1000))
                asyncio.get_running_loop().call_soon(self.drain_results)

    async def say(self, text, epoch):
        if epoch != self.epoch or self.closed or not text.strip():
            return
        await self.emit("assistant_delta", text=text, turn=epoch)
        # Record generated speech honestly. Playback acknowledgements are tracked
        # separately; interruption must not make the model assume all of it was heard.
        record = text if not self.speak_out else "[Spoken response generated; playback may be interrupted] " + text
        self.store.append(self.session, "assistant", {"text": record})
        if not self.speak_out:
            return
        await self.emit("state", state="speaking", turn=epoch)
        pcm, sr = await asyncio.wait_for(self.provider.synthesize(text, self.voice), 25)
        await self.emit("audio_sr", sr=sr, turn=epoch)
        step = int(sr * .04) * 2
        for offset in range(0, len(pcm), step):
            if epoch != self.epoch or self.closed:
                return
            part = pcm[offset:offset+step]
            packet = b"RK2A" + struct.pack(">I", epoch) + part if self.protocol >= 2 else part
            await asyncio.wait_for(self.send_bytes(packet), 5)
            self.audio_sent[epoch] = self.audio_sent.get(epoch, 0) + len(part)//2
            self.play_until = max(self.play_until, time.monotonic()) + len(part)/(2*sr)
            await asyncio.sleep(len(part)/(2*sr))
        # Limit per-connection telemetry growth.
        for table in (self.audio_sent, self.audio_played):
            for old in list(table):
                if old < epoch - 8:
                    del table[old]

    def job_event(self, event):
        if self.closed:
            return
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
