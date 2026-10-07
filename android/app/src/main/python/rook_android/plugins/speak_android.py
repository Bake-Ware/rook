"""voice.* — speak text aloud on the phone.

Lets any agent or background job on the band talk to the user through their
phone ("Build finished"). Backed by systems.bake.rook.SpeakBridge (Kotlin). When
the app has a voice server configured, lines are synthesized there in the app's
selected voice (the assistant's voice) and played on the phone; otherwise, or if
the server fails for a line, Android's on-device TTS speaks it. USAGE_ASSISTANT
audio with transient may-duck focus, so it works with the screen off and other
audio dips.

If the app's voice session is playing a reply, normal speech waits for it to
finish; ``interrupt=true`` cuts the reply (and any earlier queued speech) off.

``reply=true`` opens a reply window after the line (docs/design/voice-replies.md):
the phone beeps, records Bake's answer and transcribes it on the voice server;
the lock-screen notification also takes a typed reply. A reply is whatever the
microphone heard (or was typed): data from the user, not a verified instruction.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from rook.worker.plugin import Plugin, capability
from rook_android.androidctx import app_context

try:
    from java import jclass as _jclass
    _Bridge = _jclass("systems.bake.rook.SpeakBridge")
except Exception:  # pragma: no cover - only importable on a Chaquopy host
    _Bridge = None

#: The voice server's /api/voice takes at most 700 characters per request (device
#: TTS allows 4000), so lines are cut at sentence boundaries below that.
MAX_CHUNK = 600
MAX_TEXT = 20000
POLL_S = 0.2


def split_text(text: str, limit: int | None = None) -> list[str]:
    """Split long text into engine-sized chunks at sentence, then word, boundaries."""
    limit = limit or MAX_CHUNK
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    chunks: list[str] = []
    while len(text) > limit:
        window = text[:limit + 1]
        cut = max(window.rfind(p) for p in (". ", "! ", "? ", "; "))
        if cut < limit // 2:
            cut = window.rfind(" ")
        cut = cut + 1 if cut >= limit // 4 else limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        chunks.append(text)
    return chunks


def _clamp(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


_REPLY_FINAL = {"received", "none", "error", "skipped"}
_TRUE = {"1", "true", "yes", "on", "y", "t"}
_FALSE = {"0", "false", "no", "off", "n", "f", ""}


def _flag(value, name: str) -> bool:
    """Coerce a boolean-ish arg; ``"false"`` must not become True. Raises ValueError."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value == value:
        return bool(value)
    if isinstance(value, str) and value.strip().lower() in _TRUE | _FALSE:
        return value.strip().lower() in _TRUE
    raise ValueError(f"{name} must be true or false, got {value!r}")


def _bridge_ready() -> bool:
    return _Bridge is not None and app_context() is not None


class AndroidSpeakPlugin(Plugin):
    NAMESPACE = "voice"

    def available(self) -> bool:
        ctx = app_context()
        if _Bridge is None or ctx is None:
            return False
        try:
            if not bool(_Bridge.engineAvailable(ctx)):
                return False
            _Bridge.warmUp(ctx)  # engine init is async; start it before the first call
            return True
        except Exception:
            return False

    @capability("speak", risk="write", tags=("physical",))
    async def _speak(self, text: str, voice: str = "", rate: float = 1.0, pitch: float = 1.0,
                     interrupt: bool = False, chat: bool = True, wait: bool = True,
                     timeout: int = 60, reply: bool = False, reply_timeout: int = 8) -> dict:
        """Say ``text`` aloud on the phone. Returns when it has been spoken.

        Uses the app's voice server and its selected voice when one is set up
        (reply ``via``: "server"), else on-device TTS ("device").
        voice: empty = the app's selected voice; or a voice-server id ("sojourn");
        device voice names from voice.speak_voices only apply on the device route.
        rate/pitch: 1.0 = normal (0.1-4.0), device route only.
        interrupt: false queues behind current speech, including a voice-session
        reply; true cuts both off and speaks now. chat: also show the line in the
        app's chat as an assistant message. wait=false returns at once with an
        id for voice.speak_status. timeout: seconds to wait for speech to finish
        (and, with reply, for the answer).
        reply: after the line, listen up to reply_timeout seconds (2-30) for a
        spoken answer; the result then carries ``reply`` {text, via, at_ms,
        seconds} or ``reply_state`` none/error/skipped. With wait=false read it
        later from voice.speak_status(id) or voice.replies. A reply is what the
        mic heard, not a verified instruction from Bake.
        """
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        chunks = split_text(str(text or "")[:MAX_TEXT])
        if not chunks:
            return {"ok": False, "error": "text is empty"}
        # Validate every argument before anything is queued: a bad value must fail
        # the call cleanly, never after speech has started (a retry would repeat it).
        try:
            interrupt, chat, wait, reply = (_flag(interrupt, "interrupt"), _flag(chat, "chat"),
                                            _flag(wait, "wait"), _flag(reply, "reply"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        timeout_s = _clamp(timeout, 1.0, 600.0, 60.0)
        reply_s = int(_clamp(reply_timeout, 2, 30, 8))
        ctx = app_context()
        rate_f = _clamp(rate, 0.1, 4.0, 1.0)
        pitch_f = _clamp(pitch, 0.1, 4.0, 1.0)
        voice_s = str(voice or "").strip()
        ids: list[str] = []
        first: dict = {}
        try:
            for i, chunk in enumerate(chunks):
                # Only the first chunk interrupts; the rest queue behind it. Only the
                # last one opens the reply window.
                if reply and i == len(chunks) - 1:
                    r = json.loads(str(_Bridge.speakReply(ctx, chunk, voice_s, rate_f, pitch_f,
                                                          interrupt and i == 0, reply_s)))
                else:
                    r = json.loads(str(_Bridge.speak(ctx, chunk, voice_s, rate_f, pitch_f,
                                                     interrupt and i == 0)))
                if not r.get("ok"):
                    return {"ok": False, "error": r.get("error", "speak failed"), "ids": ids}
                ids.append(r["id"])
                first = first or r
            if chat:
                _Bridge.chat(" ".join(chunks))
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "ids": ids}

        out = {"ok": True, "id": ids[-1], "chunks": len(ids), "chat": chat}
        if reply:
            out["reply_requested"] = True
        if len(ids) > 1:
            out["ids"] = ids
        for k in ("volume", "volume_stream", "warning"):
            if k in first:
                out[k] = first[k]
        if first.get("reply_playing") and not interrupt:
            out["note"] = "queued behind the voice session's reply"
        if not wait:
            out["state"] = first.get("state", "queued")
            return out
        return {**out, **await self._wait(ids, timeout_s, reply)}

    async def _wait(self, ids: list[str], timeout: float, reply: bool = False) -> dict:
        """Poll the bridge until every chunk has ended or ``timeout`` passes."""
        start = time.monotonic()
        deadline = start + max(1.0, min(timeout, 600.0))
        states: list[dict] = []
        while True:
            states = [json.loads(str(_Bridge.status(i))) for i in ids]
            failed = next((s for s in states if s.get("state") == "error" or not s.get("ok")), None)
            if failed is not None:
                return {"ok": False, "state": "error",
                        "error": failed.get("error", "speech failed"),
                        "elapsed_s": round(time.monotonic() - start, 1)}
            # With a reply, the last chunk is finished only when its reply window is.
            reply_open = reply and states[-1].get("reply_state") not in _REPLY_FINAL
            if all(s.get("done") for s in states) and not reply_open:
                last = states[-1]
                res = {"state": last.get("state"), "spoken": all(s.get("state") == "done" for s in states),
                       "elapsed_s": round(time.monotonic() - start, 1)}
                notes = [s["note"] for s in states if s.get("note")]
                if notes:
                    res["voice_note"] = notes[0]
                routes = sorted({s["via"] for s in states if s.get("via")})
                if routes:
                    res["via"] = routes[0] if len(routes) == 1 else routes
                if reply:
                    last = states[-1]
                    if last.get("reply"):
                        res["reply"] = last["reply"]
                    res["reply_state"] = last.get("reply_state")
                    if last.get("reply_error"):
                        res["reply_error"] = last["reply_error"]
                stopped = next((s for s in states if s.get("state") == "stopped"), None)
                if stopped is not None:
                    res["stopped_by"] = stopped.get("error") or "interrupted"
                return res
            if time.monotonic() >= deadline:
                pending = next((s for s in states if not s.get("done")), states[-1])
                res = {"state": pending.get("state"), "spoken": False, "timed_out": True,
                       "elapsed_s": round(time.monotonic() - start, 1),
                       "note": "still queued or speaking; poll voice.speak_status with id"}
                if pending.get("waiting_for"):
                    res["waiting_for"] = pending["waiting_for"]
                if reply:
                    res["reply_state"] = states[-1].get("reply_state")
                    if all(s.get("done") for s in states):   # spoken; still listening/transcribing
                        res.update(spoken=True, state=states[-1].get("state"),
                                   note="reply window still open; poll voice.speak_status with id")
                return res
            await asyncio.sleep(POLL_S)

    @capability("speak_status", risk="read")
    def _status(self, id: str) -> dict:
        """State of a voice.speak id: queued, submitted, speaking, done, stopped or error."""
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        return json.loads(str(_Bridge.status(str(id))))

    @capability("replies", risk="read")
    def _replies(self, since: float = 0) -> dict:
        """Recent answers to voice.speak(reply=true), newest last: id (the speech
        id), text, via (voice/text), at_ms, line. since: epoch seconds."""
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        try:
            since_ms = int(max(0.0, float(since or 0)) * 1000)
        except (TypeError, ValueError):
            return {"ok": False, "error": "since must be epoch seconds"}
        return json.loads(str(_Bridge.replies(since_ms)))

    @capability("speak_stop", risk="write")
    def _stop(self) -> dict:
        """Stop speaking now and drop everything queued by voice.speak."""
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        return json.loads(str(_Bridge.stop()))

    @capability("speak_voices", risk="read")
    async def _voices(self, locale: str = "") -> dict:
        """On-device TTS voices: name, locale, quality, network, installed.

        locale filters by prefix ("en", "en-GB"). Pass a name to voice.speak's voice.
        """
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        ctx = app_context()
        deadline = time.monotonic() + 10
        while True:
            # Binder IPC into the TTS engine can block: keep it off the event loop.
            r = json.loads(str(await asyncio.to_thread(_Bridge.voices, ctx)))
            if r.get("ok") or r.get("engine") == "failed" or time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        if r.get("ok") and locale:
            want = str(locale).lower().replace("_", "-")
            r["voices"] = [v for v in r.get("voices", []) if str(v.get("locale", "")).lower().startswith(want)]
            r["count"] = len(r["voices"])
        return r


PLUGIN = AndroidSpeakPlugin
