"""voice.* — speak text aloud on the phone with Android's on-device TTS.

Lets any agent or background job on the band talk to the user through their
phone ("Build finished"). Backed by systems.bake.rook.SpeakBridge (Kotlin): one
TextToSpeech engine kept alive in the worker process, USAGE_ASSISTANT audio with
transient may-duck focus, so it works with the screen off and other audio dips.

If the app's voice session is playing a reply, normal speech waits for it to
finish; ``interrupt=true`` cuts the reply (and any earlier queued speech) off.
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

#: TextToSpeech.getMaxSpeechInputLength() is 4000; stay under it.
MAX_CHUNK = 3900
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
                     timeout: int = 60) -> dict:
        """Say ``text`` aloud on the phone (on-device TTS). Returns when it has been spoken.

        voice: a voice name from voice.speak_voices or a locale tag ("en-GB");
        empty = system default. rate/pitch: 1.0 = normal (0.1-4.0).
        interrupt: false queues behind current speech, including a voice-session
        reply; true cuts both off and speaks now. chat: also show the line in the
        app's chat as an assistant message. wait=false returns at once with an
        id for voice.speak_status. timeout: seconds to wait for speech to finish.
        """
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        chunks = split_text(str(text or "")[:MAX_TEXT])
        if not chunks:
            return {"ok": False, "error": "text is empty"}
        # Validate every argument before anything is queued: a bad value must fail
        # the call cleanly, never after speech has started (a retry would repeat it).
        try:
            interrupt, chat, wait = (_flag(interrupt, "interrupt"), _flag(chat, "chat"),
                                     _flag(wait, "wait"))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        timeout_s = _clamp(timeout, 1.0, 600.0, 60.0)
        ctx = app_context()
        rate_f = _clamp(rate, 0.1, 4.0, 1.0)
        pitch_f = _clamp(pitch, 0.1, 4.0, 1.0)
        voice_s = str(voice or "").strip()
        ids: list[str] = []
        first: dict = {}
        try:
            for i, chunk in enumerate(chunks):
                # Only the first chunk interrupts; the rest queue behind it.
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
        if len(ids) > 1:
            out["ids"] = ids
        for k in ("volume", "warning"):
            if k in first:
                out[k] = first[k]
        if first.get("reply_playing") and not interrupt:
            out["note"] = "queued behind the voice session's reply"
        if not wait:
            out["state"] = first.get("state", "queued")
            return out
        return {**out, **await self._wait(ids, timeout_s)}

    async def _wait(self, ids: list[str], timeout: float) -> dict:
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
            if all(s.get("done") for s in states):
                last = states[-1]
                res = {"state": last.get("state"), "spoken": all(s.get("state") == "done" for s in states),
                       "elapsed_s": round(time.monotonic() - start, 1)}
                notes = [s["note"] for s in states if s.get("note")]
                if notes:
                    res["voice_note"] = notes[0]
                stopped = next((s for s in states if s.get("state") == "stopped"), None)
                if stopped is not None:
                    res["stopped_by"] = stopped.get("error") or "interrupted"
                return res
            if time.monotonic() >= deadline:
                pending = next(s for s in states if not s.get("done"))
                res = {"state": pending.get("state"), "spoken": False, "timed_out": True,
                       "elapsed_s": round(time.monotonic() - start, 1),
                       "note": "still queued or speaking; poll voice.speak_status with id"}
                if pending.get("waiting_for"):
                    res["waiting_for"] = pending["waiting_for"]
                return res
            await asyncio.sleep(POLL_S)

    @capability("speak_status", risk="read")
    def _status(self, id: str) -> dict:
        """State of a voice.speak id: queued, submitted, speaking, done, stopped or error."""
        if not _bridge_ready():
            return {"ok": False, "error": "not an Android host"}
        return json.loads(str(_Bridge.status(str(id))))

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
