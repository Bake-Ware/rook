"""voice.*: systems.bake.rook.SpeakBridge (the app's voice server, else
Android's TextToSpeech), USAGE_ASSISTANT audio with ducking focus."""

import json
import re
import time

from rook.kit import android
from rook.kit.ctx import KitError

#: The voice server's Chatterbox voices stop at about 32 s of audio and drop
#: the rest, so lines are cut at sentence boundaries well below (about 17 s).
MAX_CHUNK = 280
POLL_S = 0.2
REPLY_DONE = {"received", "none", "error", "skipped"}


def split_text(text: str, limit: int = MAX_CHUNK) -> list[str]:
    """Engine-sized chunks, cut at sentence ends, else at a space."""
    text = re.sub(r"\s+", " ", text).strip()
    chunks = []
    while len(text) > limit:
        window = text[:limit + 1]
        cut = max(window.rfind(p) for p in (". ", "! ", "? ", "; "))
        if cut < limit // 2:
            cut = window.rfind(" ")
        cut = cut + 1 if cut >= limit // 4 else limit
        chunks.append(text[:cut].strip())
        text = text[cut:].strip()
    return chunks + [text] if text else chunks


def _bridge():
    return android.app("SpeakBridge")


def start(ctx):
    """Start the speech engine before the first line (its init is async). Never fails the load."""
    try:
        b, c = _bridge(), android.context()
        if b.engineAvailable(c):
            b.warmUp(c)
    except Exception:
        pass


def speak(ctx, text, voice, rate, pitch, interrupt, chat, wait, timeout, reply, reply_timeout=8):
    chunks = split_text(text)
    if not chunks:
        raise KitError("text is blank")
    b, c = _bridge(), android.context()
    ids, first = [], {}
    for n, chunk in enumerate(chunks):
        # Only the first chunk interrupts; only the last opens the reply window.
        cut = interrupt and n == 0
        if reply and n == len(chunks) - 1:
            r = json.loads(str(b.speakReply(c, chunk, voice, rate, pitch, cut, reply_timeout)))
        else:
            r = json.loads(str(b.speak(c, chunk, voice, rate, pitch, cut)))
        if not r.get("ok"):
            raise KitError(f"{r.get('error') or 'speech failed'}" + (f" (after {len(ids)} parts)" if ids else ""))
        ids.append(r["id"])
        first = first or r
    if chat:
        b.chat(" ".join(chunks))
    out = {"id": ids[-1], "chunks": len(ids), **{k: first[k] for k in ("volume", "volume_stream", "warning")
                                                    if k in first}}
    if first.get("reply_playing") and not interrupt:
        out["note"] = "queued behind the voice session's reply"
    if not wait:
        return {**out, "state": first.get("state", "queued")}
    return {**out, **_wait(b, ids, timeout, reply)}


def _wait(b, ids, timeout, reply):
    """Poll until every chunk has ended (and, with reply, its listening) or ``timeout`` seconds."""
    start = time.monotonic()
    while True:
        states = [json.loads(str(b.status(i))) for i in ids]
        failed = next((s for s in states if s.get("state") == "error" or not s.get("ok", True)), None)
        if failed is not None:
            raise KitError(str(failed.get("error") or "speech failed"))
        last = states[-1]
        spoken = all(s.get("done") for s in states)
        done = spoken and not (reply and last.get("reply_state") not in REPLY_DONE)
        elapsed = round(time.monotonic() - start, 1)
        if done or elapsed >= timeout:
            res = {"state": last.get("state"), "spoken": spoken and all(s.get("state") == "done" for s in states),
                   "elapsed_s": elapsed}
            routes = sorted({s["via"] for s in states if s.get("via")})
            if routes:
                res["via"] = routes[0] if len(routes) == 1 else routes
            stopped = next((s for s in states if s.get("state") == "stopped"), None)
            if stopped is not None:
                res["stopped_by"] = stopped.get("error") or "interrupted"
            if reply:
                res.update({k: last[k] for k in ("reply", "reply_state", "reply_error") if last.get(k) is not None})
            if not done:
                res["timed_out"] = True
                res["note"] = ("still listening; the answer will be in voice.replies" if spoken
                               else "still queued or speaking")
            return res
        time.sleep(POLL_S)


def replies(ctx, since):
    return android.result(_bridge().replies(int(since * 1000)))


def stop(ctx):
    return android.result(_bridge().stop())


def voices(ctx, locale):
    b, c = _bridge(), android.context()
    deadline = time.monotonic() + 10
    while True:   # the engine lists its voices only once it is up
        r = json.loads(str(b.voices(c)))
        if r.get("ok") or r.get("engine") == "failed" or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if not r.pop("ok", False):
        raise KitError(str(r.get("error") or "the phone's speech engine didn't start"))
    out = r
    if locale:
        want = locale.lower().replace("_", "-")
        out["voices"] = [v for v in out.get("voices", []) if str(v.get("locale", "")).lower().startswith(want)]
        out["count"] = len(out["voices"])
    return out
