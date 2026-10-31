"""The speech runtime: voice sessions as streams, System One in front of the agent.

A voice session is a durable ``voice`` stream (rook.streams) in this
plugin's own store, with the person and ``agent:<name>`` as participants.
Its events:

- ``turn`` ``{role: prompt, text, speaker}``: what the person said;
- ``turn`` ``{role: reply, text, system: one|two}``: what was said back,
  by System One (the fast talker) or the agent itself;
- ``status`` ``{state: listening|thinking|idle|error, ...}``.

The latest speaker ID result is also on the stream's attrs (``speaker``),
with any half-finished utterance (``pending``) while voice.turn says the
person only paused.

System Two is the agent's normal brain: speech hands it the request with
``agents.chat.send`` as the person, so it lands in their persistent session
with that agent (the one they also type in) and runs with their rights.
That call runs in the background; ``speech.hear`` answers with System One's
reply at once.

TODO(castling:streams): sessions live here until the hub keeps one stream
index (stream.* caps and the relay); then speech.read is stream.read.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from pathlib import Path

from rook.kit.ctx import KitError
from rook.streams import SqliteStore, StreamError, Streams

log = logging.getLogger("rook.plugin.speech")

AUDIO_CACHE = 64          # spoken replies kept for speech.read(audio=true)
SAY_MAX = 600             # System One says one or two sentences
CHUNK = 300               # characters per spoken clip

TALKER = """\
You are the voice of {agent}, the quick front talker in a spoken conversation
with {person}. {agent} itself - the agent's full brain, with its tools, memory
and the person's work - handles anything that needs thinking, looking up or
doing; you hand those to it.

Answer with one JSON object and nothing else:
{{"say": "what you say aloud now", "handoff": "the request for {agent}, or empty"}}

- Greetings, small talk, thanks, and what the conversation already answers:
  say it yourself, handoff empty.
- Everything else (their things, tasks, lookups, actions, advice that needs
  context): a short acknowledgement in say ("Let me check.", or empty) and the
  whole request in handoff, in the person's words with the context it needs.
- Never claim you did or found something yourself. say is spoken aloud: one
  or two short plain sentences, no markdown.{speaker}"""

SPOKEN = """\
(Spoken, in a voice session.) {text}

Your reply is read aloud: a few plain spoken sentences, no markdown, lists or links."""


class Runtime:
    def __init__(self, data: Path):
        data.mkdir(parents=True, exist_ok=True)
        self.streams = Streams(SqliteStore(str(data / "sessions.db")), host="hub")
        self.locks: dict[str, asyncio.Lock] = {}
        self.audio: OrderedDict = OrderedDict()   # (stream, seq) -> file map
        self.tasks: set[asyncio.Task] = set()


_RT: Runtime | None = None


def _rt(ctx) -> Runtime:
    global _RT
    if _RT is None:
        _RT = Runtime(ctx.data)
        ctx.index_streams(_RT.streams)       # voice sessions in the hub's stream list (N28)
    return _RT


async def stop(ctx):
    global _RT
    rt, _RT = _RT, None
    if rt is None:
        return
    for t in list(rt.tasks):
        t.cancel()
    await asyncio.gather(*rt.tasks, return_exceptions=True)
    rt.streams.durable.close()


# -- the caps --------------------------------------------------------------------

async def open_session(ctx, agent=None, voice=None, device=None, title=None):
    who = _who(ctx)
    agent = agent or ctx.settings["agent"]
    await ctx.call("agents.get", name=agent)        # a missing agent fails here, not mid-conversation
    voice = voice if voice is not None else ctx.settings["voice"]
    attrs = {"agent": agent, "voice": voice or "", **({"device": device} if device else {})}
    meta = _rt(ctx).streams.open("voice", title=title or f"Voice: {_name(who)} and {agent}",
                                 producer="speech", participants=[who, f"agent:{agent}"], attrs=attrs)
    return {"stream": meta.id, "agent": agent, "voice": voice or ""}


async def hear(ctx, stream, audio=None, text=None, final=False):
    if (audio is None) == (text is None):
        raise ctx.Error("give audio or text (one of them)")
    rt = _rt(ctx)
    meta = _session(ctx, rt, stream, write=True)
    async with rt.locks.setdefault(stream, asyncio.Lock()):
        speaker, complete = None, True
        if audio is not None:
            got, speaker, turn = await asyncio.gather(
                ctx.call("voice.stt", audio=audio), _maybe(ctx.call("voice.speaker", audio=audio)),
                _maybe(ctx.call("voice.turn", audio=audio)) if not final else _none())
            heard = (got.get("text") or "").strip()
            complete = final or turn is None or bool(turn.get("complete", True))
        else:
            heard = text.strip()
        if speaker is not None:
            speaker = {k: speaker[k] for k in ("label", "score") if k in speaker}
            meta = rt.streams.update(stream, attrs={"speaker": speaker})
        heard = " ".join(p for p in (meta.attrs.get("pending") or "", heard) if p)
        if not complete:
            rt.streams.update(stream, attrs={"pending": heard})
            if heard:
                rt.streams.append(stream, "status", {"state": "listening", "pending": heard}, by="speech")
            return {"heard": heard, "complete": False, "speaker": speaker, "say": None, "handoff": False}
        if meta.attrs.get("pending"):
            rt.streams.update(stream, attrs={"pending": ""})
        if not heard:
            return {"heard": "", "complete": True, "speaker": speaker, "say": None, "handoff": False}
        who = _who(ctx)
        rt.streams.append(stream, "turn", {"role": "prompt", "text": heard,
                                           **({"speaker": speaker} if speaker else {})}, by=who)
        agent = meta.attrs["agent"]
        say, handoff = await _talk(ctx, rt, meta, heard, speaker)
        out = {"heard": heard, "complete": True, "speaker": speaker, "say": None, "handoff": bool(handoff)}
        if say:
            ev = rt.streams.append(stream, "turn", {"role": "reply", "text": say, "system": "one"},
                                   by=f"agent:{agent}")
            out["say"] = {"text": say, "seq": ev.seq, "audio": await _audio(ctx, rt, meta, ev.seq, say)}
        if handoff:
            rt.streams.append(stream, "status", {"state": "thinking", "agent": agent}, by="speech")
            task = asyncio.create_task(_think(ctx, rt, stream, agent, handoff))
            rt.tasks.add(task)
            task.add_done_callback(rt.tasks.discard)
        return out


async def read(ctx, stream, after=0, limit=50, wait=0, audio=False):
    rt = _rt(ctx)
    meta = _session(ctx, rt, stream, open_only=False)
    got = await rt.streams.read(stream, after, limit, wait=float(wait))
    events = [e.wire() for e in got["events"]]
    if audio:
        for e in events:
            d = e.get("data") or {}
            if e["type"] == "turn" and d.get("role") == "reply" and d.get("text"):
                try:
                    e["audio"] = await _audio(ctx, rt, meta, e["seq"], d["text"])
                except KitError as err:
                    e["audio_error"] = err.message
    return {"stream": stream, "events": events, "next": got["next"], "closed": got["closed"],
            "attrs": {k: meta.attrs[k] for k in ("agent", "voice", "speaker", "device") if k in meta.attrs}}


async def close(ctx, stream):
    rt = _rt(ctx)
    _session(ctx, rt, stream, open_only=False, write=True)
    meta = rt.streams.close(stream, by=_who(ctx))
    return {"stream": stream, "closed": meta.closed}


async def voices(ctx):
    out, errors = {}, {}
    for plugin in ("speech-engine", "speech-elevenlabs"):
        try:
            out[plugin] = await ctx.call(f"{plugin}.voices")
        except KitError as e:
            errors[plugin] = e.message
    return {"voices": out, **({"errors": errors} if errors else {})}


async def status(ctx):
    rt = _rt(ctx)
    mine = [m.wire() for m in rt.streams.list("voice", participant=_who(ctx), open_only=True)]
    try:
        engine = await ctx.call("speech-engine.status")
    except KitError as e:
        engine = {"error": e.message}
    return {"sessions": mine, "agent": ctx.settings["agent"], "talker": bool(ctx.settings["talker"]),
            "engine": engine}


# -- System One and Two ------------------------------------------------------------

async def _talk(ctx, rt: Runtime, meta, heard: str, speaker) -> tuple[str, str]:
    """System One's ``(say, handoff)``. Off, or no model to ask, or an
    answer it can't read: everything goes to the agent."""
    if not ctx.settings["talker"]:
        return "", heard
    agent, person = meta.attrs["agent"], _name(_who(ctx))
    note = ""
    if speaker and speaker.get("label"):
        note = (f"\n\nSpeaker ID (advisory) says the voice sounds like: {speaker['label']}"
                + (f" (similarity {speaker['score']:.2f})." if isinstance(speaker.get("score"), (int, float)) else "."))
    messages = [{"role": "system", "content": TALKER.format(agent=agent, person=person, speaker=note)}]
    turns = await _history(rt, meta.id, int(ctx.settings["history"]))
    messages += turns or [{"role": "user", "content": heard}]
    try:
        got = await ctx.call("llm.complete", messages=messages, model=ctx.settings["talker_model"] or "one")
    except KitError as e:
        log.info("System One unavailable, handing off: %s", e.message)
        return "", heard
    plan = parse_plan(got.get("text") or "")
    if plan is None:
        return "", heard
    return plan


def parse_plan(text: str) -> tuple[str, str] | None:
    """``(say, handoff)`` from System One's JSON answer (fenced or not), or None."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(d, dict) or not isinstance(d.get("say", ""), str) or not isinstance(d.get("handoff", ""), str):
        return None
    say, handoff = d.get("say", "").strip()[:SAY_MAX], d.get("handoff", "").strip()
    return (say, handoff) if say or handoff else None


async def _think(ctx, rt: Runtime, stream: str, agent: str, request: str) -> None:
    """System Two: the agent answers as itself, in the person's session with it."""
    try:
        got = await ctx.call("agents.chat.send", agent=agent, text=SPOKEN.format(text=request), wait=True)
        reply = (got.get("text") or "").strip()
        if reply:
            rt.streams.append(stream, "turn", {"role": "reply", "text": reply, "system": "two"},
                              by=f"agent:{agent}")
        rt.streams.append(stream, "status", {"state": "idle"}, by="speech")
    except KitError as e:
        _append_quietly(rt, stream, {"state": "error", "error": e.message})
    except StreamError:        # the session was closed meanwhile
        pass
    except Exception as e:     # plugin code never leaves a task failing unseen
        log.warning("System Two hand-off failed: %s: %s", type(e).__name__, e)
        _append_quietly(rt, stream, {"state": "error", "error": f"{type(e).__name__}: {e}"[:300]})


async def _history(rt: Runtime, stream: str, n: int) -> list[dict]:
    if n <= 0:
        return []
    meta = rt.streams.get(stream)
    got = await rt.streams.read(stream, max(0, meta.last_seq - 4 * n), 4 * n, types=("turn",))
    turns = [e.body for e in got["events"] if e.body.get("text")][-n:]
    out = [{"role": "user" if t["role"] == "prompt" else "assistant", "content": t["text"]} for t in turns]
    while out and out[0]["role"] == "assistant":    # a conversation starts with the person (the Claude API insists)
        out.pop(0)
    return out


async def _audio(ctx, rt: Runtime, meta, seq: int, text: str) -> list:
    """A reply's spoken audio, one clip per chunk of sentences (voice.tts in
    the session's voice), cached."""
    key = (meta.id, seq)
    if key in rt.audio:
        rt.audio.move_to_end(key)
        return rt.audio[key]
    voice = meta.attrs.get("voice") or ""
    clips = [await _tts(ctx, part, voice) for part in chunks(text)]
    rt.audio[key] = clips
    while len(rt.audio) > AUDIO_CACHE:
        rt.audio.popitem(last=False)
    return clips


async def _tts(ctx, text: str, voice: str) -> dict:
    try:
        return await ctx.call("voice.tts", text=text, **({"voice": voice} if voice else {}))
    except KitError as first:
        # A hosted voice that fails speaks in the local engine's default.
        try:
            return await ctx.call("speech-engine.tts", text=text)
        except KitError:
            raise first from None


def chunks(text: str, limit: int = CHUNK) -> list[str]:
    """Sentences packed into pieces of at most ``limit`` characters (a long
    sentence is cut at a space): an engine speaks a short piece well, and
    Chatterbox stops at about 30 seconds."""
    out, cur = [], ""
    for sentence in re.split(r"(?<=[.!?])\s+", text.strip()):
        while len(sentence) > limit:
            cut = sentence.rfind(" ", 0, limit)
            cut = cut if cut > 0 else limit
            out, cur = [*out, *([cur] if cur else []), sentence[:cut].strip()], ""
            sentence = sentence[cut:].strip()
        if cur and len(cur) + 1 + len(sentence) > limit:
            out, cur = [*out, cur], sentence
        else:
            cur = f"{cur} {sentence}".strip()
    return [*out, *([cur] if cur else [])]


# -- helpers --------------------------------------------------------------------------

def _who(ctx) -> str:
    who = ctx.person or ctx.agent
    if not who:
        raise ctx.Error("a voice session is someone's: call as a person or an agent")
    return who


def _name(who: str) -> str:
    return who.partition(":")[2] or who


def _session(ctx, rt: Runtime, stream: str, open_only: bool = True, write: bool = False):
    """The session, for a participant; owners may also read anyone's. Only
    a participant speaks into one: System Two runs as whoever speaks."""
    try:
        meta = rt.streams.get(stream)
    except StreamError:
        raise ctx.Error(f"no voice session {stream}") from None
    mine = _who(ctx) in meta.participants
    if meta.kind != "voice" or not (mine or ctx.caller.owner):
        raise ctx.Error(f"no voice session {stream}")
    if write and not mine:
        raise ctx.Error(f"voice session {stream} is someone else's: only they speak into it")
    if open_only and meta.closed:
        raise ctx.Error(f"voice session {stream} is closed")
    return meta


def _append_quietly(rt: Runtime, stream: str, body: dict) -> None:
    try:
        rt.streams.append(stream, "status", body, by="speech")
    except StreamError:
        pass


async def _maybe(aw):
    """An optional step (speaker ID, end of turn): its result, or None when
    nothing provides it or it failed."""
    try:
        return await aw
    except KitError as e:
        log.debug("optional speech step skipped: %s", e.message)
        return None


async def _none():
    return None
