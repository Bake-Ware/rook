"""Remote voice: a remote's utterance into a voice session, the replies onto its screen.

The audio is what the Bluetooth remote sent, untouched: ATVV 0.4 frames of
134 bytes (sequence 2, remote id 1, the ADPCM predictor 2 and step index 1,
then 128 bytes: 256 samples of 4-bit IMA ADPCM), concatenated. Each frame
carries its own decoder state, so a lost frame costs only itself.
"""

from __future__ import annotations

import asyncio
import base64
import io
import struct
import textwrap
import time
import wave

FRAME, HEAD = 134, 6
KEEP = 120.0                 # seconds a half-arrived utterance is kept
PARTS: dict[str, dict] = {}  # utterance id -> {"at": time, "parts": {seq: bytes}}
SESSIONS: dict[str, dict] = {}   # remote -> {"stream": id, "next": cursor}

STEPS = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80,
         88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
         598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749,
         3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
         13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767)
INDEX = (-1, -1, -1, -1, 2, 4, 6, 8)


def on_part(ctx, event=None, payload=None, **_):
    """Hook on remote.voice.part: keep the piece until remote.voice asks for the whole."""
    now = time.time()
    for k in [k for k, v in PARTS.items() if now - v["at"] > KEEP]:
        PARTS.pop(k, None)
    p = payload or {}
    try:
        data = base64.b64decode(p.get("data") or "")
        seq = int(p.get("seq") or 0)
    except (ValueError, TypeError):
        return
    u = PARTS.setdefault(str(p.get("id")), {"at": now, "parts": {}})
    u["at"] = now
    u["parts"][seq] = data


def _nibbles(data: bytes, pred: int, index: int, high_first: bool) -> list[int]:
    out = []
    for b in data:
        for n in ((b >> 4, b & 15) if high_first else (b & 15, b >> 4)):
            step = STEPS[index]
            diff = step >> 3
            if n & 4:
                diff += step
            if n & 2:
                diff += step >> 1
            if n & 1:
                diff += step >> 2
            pred = max(-32768, min(32767, pred - diff if n & 8 else pred + diff))
            index = max(0, min(88, index + INDEX[n & 7]))
            out.append(pred)
    return out


def _frames(raw: bytes) -> tuple[list[bytes], int]:
    """The 134-byte frames in raw. Frames follow one another with rising
    sequence numbers and a step index of at most 88; where that breaks (a
    notification was lost, or junk leads), it picks up at the offset from
    which the longest run of frames starts."""
    def run(at: int) -> int:
        n, want = 0, None
        while at + FRAME <= len(raw):
            seq = struct.unpack_from(">H", raw, at)[0]
            if raw[at + 5] > 88 or (want is not None and seq != want):
                break
            n, want, at = n + 1, (seq + 1) % 65536, at + FRAME
        return n

    frames, skipped, at = [], 0, 0
    while at + FRAME <= len(raw):
        start = max(range(at, min(at + FRAME, len(raw) - FRAME + 1)), key=lambda o: (run(o), -o))
        n = run(start)
        if not n:
            break
        skipped += start - at
        frames += [raw[start + i * FRAME:start + (i + 1) * FRAME] for i in range(n)]
        at = start + n * FRAME
    return frames, skipped


def _rough(pcm: list[int]) -> float:
    return sum(abs(pcm[i] - 2 * pcm[i - 1] + pcm[i - 2]) for i in range(2, len(pcm))) / max(1, len(pcm))


def decode(raw: bytes) -> tuple[list[int], dict]:
    """ATVV frames to 16-bit samples. The nibble order and the predictor's byte
    order are whichever gives the smoother sound (wrong ones give noise)."""
    frames, skipped = _frames(raw)
    info = {"bytes": len(raw), "frames": len(frames), "skipped": skipped}
    if not frames:
        return [], info
    best = None
    for big in (True, False):
        for high in (False, True):
            pcm: list[int] = []
            for f in frames:
                pred = struct.unpack_from(">h" if big else "<h", f, 3)[0]
                pcm += _nibbles(f[HEAD:], pred, f[5], high)
            r = _rough(pcm)
            if best is None or r < best[0]:
                best = (r, pcm, big, high)
    info.update(predictor="big-endian" if best[2] else "little-endian", nibble="high first" if best[3] else "low first",
                rough=round(best[0], 1), peak=max(abs(s) for s in best[1]))
    return best[1], info


def wav_bytes(pcm: list[int], rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))
    return buf.getvalue()


def pages(text: str, cols: int, rows: int) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        lines += textwrap.wrap(para, cols, break_long_words=True) or [""]
    return ["\n".join(lines[i:i + rows]) for i in range(0, len(lines), rows)] or [""]


async def _show(ctx, remote: str, text: str, hold: int = 60) -> None:
    """text on the remote's screen, a page at a time; the last page stays for hold seconds."""
    s = ctx.settings
    ps = pages(text, int(s["cols"]), int(s["rows"]))
    for i, page in enumerate(ps):
        last = i == len(ps) - 1
        try:
            await ctx.call_on(remote, "remote.display", text=page, seconds=hold if last else int(s["page_seconds"]) + 2)
        except ctx.Error:
            return                       # the remote went away: nothing to show it on
        if not last:
            await asyncio.sleep(int(s["page_seconds"]))


async def _session(ctx, remote: str, fresh: bool = False) -> dict:
    s = SESSIONS.get(remote)
    if s is None or fresh:
        args = {"device": remote, "title": f"Voice: {remote}"}
        if ctx.settings["agent"]:
            args["agent"] = ctx.settings["agent"]
        got = await ctx.call("speech.open", **args)
        s = SESSIONS[remote] = {"stream": got["stream"], "next": 0}
    return s


async def hear(ctx, event):
    p = (event or {}).get("payload") or {}
    uid, remote, n = str(p.get("id")), p.get("worker"), int(p.get("parts") or 0)
    if not remote:
        raise ctx.Error("the remote.voice event names no worker")
    deadline = time.monotonic() + 4          # the last parts may still be on their way in
    while len((PARTS.get(uid) or {}).get("parts", {})) < n and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    u = PARTS.pop(uid, None)
    if not u or not u["parts"]:
        await _show(ctx, remote, "The audio didn't arrive", 6)
        raise ctx.Error(f"no audio arrived for utterance {uid}")
    raw = b"".join(u["parts"][k] for k in sorted(u["parts"]))
    rate = 16000 if p.get("codec") == "adpcm16k" else 8000
    pcm, info = decode(raw)
    info.update(parts=len(u["parts"]), of=n, rate=rate, seconds=round(len(pcm) / rate, 2))
    try:                                     # the last utterance, kept to look at when it goes wrong
        ctx.data.mkdir(parents=True, exist_ok=True)
        (ctx.data / "last.raw").write_bytes(raw)
        if pcm:
            (ctx.data / "last.wav").write_bytes(wav_bytes(pcm, rate))
    except OSError:
        pass
    if len(pcm) < rate // 4:
        await _show(ctx, remote, "Heard nothing", 6)
        return {"heard": "", "audio": info}
    audio = {"data": base64.b64encode(wav_bytes(pcm, rate)).decode(), "mime": "audio/wav"}
    s = await _session(ctx, remote)
    try:
        s["next"] = (await ctx.call("speech.read", stream=s["stream"], after=s["next"], limit=500))["next"]
        out = await ctx.call("speech.hear", stream=s["stream"], audio=audio, final=True)
    except ctx.Error:                        # the session was closed, or is someone else's: a new one
        s = await _session(ctx, remote, fresh=True)
        out = await ctx.call("speech.hear", stream=s["stream"], audio=audio, final=True)
    heard = (out.get("heard") or "").strip()
    if not heard:
        await _show(ctx, remote, "Didn't catch that", 6)
        return {"heard": "", "audio": info}
    say = ((out.get("say") or {}).get("text") or "").strip()
    handoff = bool(out.get("handoff"))
    await _show(ctx, remote, f"> {heard}" + (f"\n{say}" if say else "\n..." if handoff else ""), 20 if handoff else 60)
    reply = ""
    if handoff:                              # the agent's own answer follows in the session
        end = time.monotonic() + 150
        while not reply and time.monotonic() < end:
            got = await ctx.call("speech.read", stream=s["stream"], after=s["next"], wait=30)
            s["next"] = got["next"]
            for e in got["events"]:
                d = e.get("data") or {}
                if e.get("type") == "turn" and d.get("role") == "reply" and d.get("system") != "one" and d.get("text"):
                    reply = d["text"].strip()
            if got.get("closed"):
                break
        await _show(ctx, remote, reply or "(no answer yet)", 90 if reply else 8)
    return {"heard": heard, "say": say, "reply": reply, "handoff": handoff, "audio": info}
