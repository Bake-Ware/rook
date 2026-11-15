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
import json
import ssl
import struct
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import wave

from rook.kit.net import Stream

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


def decode(raw: bytes) -> tuple[list[int], dict]:
    """ATVV frames to 16-bit samples: the predictor is big-endian and the high
    nibble of each byte comes first (checked on a real remote: decoded that
    way, every frame ends on exactly the state the next one's header gives)."""
    frames, skipped = _frames(raw)
    info = {"bytes": len(raw), "frames": len(frames), "skipped": skipped}
    pcm: list[int] = []
    for f in frames:
        pcm += _nibbles(f[HEAD:], struct.unpack_from(">h", f, 3)[0], f[5], True)
    if pcm:
        info["peak"] = max(abs(x) for x in pcm)
    return pcm, info


def wav_bytes(pcm: list[int], rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(struct.pack(f"<{len(pcm)}h", *pcm))
    return buf.getvalue()


def to_16k(pcm: list[int]) -> list[int]:
    """8 kHz to the 16 kHz the speech engines take: each sample, then the
    midpoint to the next."""
    out = []
    for i, s in enumerate(pcm):
        out.append(s)
        out.append((s + pcm[i + 1]) // 2 if i + 1 < len(pcm) else s)
    return out


def _server(url: str) -> urllib.parse.SplitResult:
    base = url.strip().rstrip("/")
    for was, now in (("wss://", "https://"), ("ws://", "http://")):
        if base.startswith(was):
            base = now + base[len(was):]
    if base.endswith("/ws"):
        base = base[:-3]
    return urllib.parse.urlsplit(base)


def _direct(u: urllib.parse.SplitResult, headers: dict, data: bytes) -> str:
    req = urllib.request.Request(u.geturl() + "/api/transcribe", data=data, method="POST", headers=headers)
    tls = ssl.create_default_context()
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=45, context=tls) as r:
        return str(json.loads(r.read()).get("text") or "")


async def transcribe(ctx, pcm: list[int]) -> str:
    """What the voice server hears in 16 kHz samples: its POST /api/transcribe,
    the way the phone app asks it (audio/L16, a bearer key when it wants one).
    The server signs its own certificate, so it is not checked. With voice_via,
    the request goes out from that worker (one on the server's network), as
    the TV plugins reach their TVs."""
    st = ctx.settings
    u, token, via = _server(st["voice_url"]), st["voice_token"] or "", st["voice_via"]
    data = struct.pack(f"<{len(pcm)}h", *pcm)
    headers = {"Content-Type": "audio/L16", **({"Authorization": f"Bearer {token}"} if token else {})}
    if not via:
        return await asyncio.to_thread(_direct, u, headers, data)
    tls = u.scheme == "https"
    host, port = u.hostname, u.port or (443 if tls else 80)
    head = "\r\n".join([f"POST {u.path.rstrip('/')}/api/transcribe HTTP/1.1", f"Host: {host}:{port}", "Connection: close",
                         f"Content-Length: {len(data)}", *(f"{k}: {v}" for k, v in headers.items())]) + "\r\n\r\n"
    async with await Stream.tcp(ctx, via, host, port, tls=tls, verify=False) as s:
        await s.send(head.encode())
        for i in range(0, len(data), 48000):      # a few seconds of sound is too much for one write
            await s.send(data[i:i + 48000])
        got = (await s.read_until(b"\r\n\r\n", 45)).decode("latin-1")
        status = int(got.split()[1])
        length = [h.partition(":")[2].strip() for h in got.split("\r\n") if h.lower().startswith("content-length:")]
        body = await s.read_exactly(int(length[0]), 20) if length else await s.read_to_close(20)
    if status != 200:
        raise ValueError(f"http {status}: {body[:120].decode('latin-1')}")
    return str(json.loads(body).get("text") or "")


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
        for attempt in range(4):         # a remote that just stopped listening can miss a call: ask again soon
            try:
                await asyncio.wait_for(ctx.call_on(remote, "remote.display", text=page,
                                                   seconds=hold if last else int(s["page_seconds"]) + 2), 4)
                break
            except (asyncio.TimeoutError, ctx.Error):
                if attempt == 3:
                    return               # the remote went away: nothing to show it on
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
    t_start = time.monotonic()
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
            (ctx.data / "last.json").write_text(repr(info))
    except OSError:
        pass
    if len(pcm) < rate // 4:
        await _show(ctx, remote, "Heard nothing", 6)
        return {"heard": "", "audio": info}
    if rate == 8000:
        pcm, rate = to_16k(pcm), 16000
    pcm = pcm[:30 * rate]
    said: dict = {"audio": {"data": base64.b64encode(wav_bytes(pcm, rate)).decode(), "mime": "audio/wav"}}
    if ctx.settings["voice_url"]:            # a voice server hears it (as the phone app does), the session gets the words
        try:
            t0 = time.monotonic()
            text = await transcribe(ctx, pcm)
            info["stt_seconds"] = round(time.monotonic() - t0, 1)
        except (OSError, ValueError, IndexError, urllib.error.URLError, ctx.Error) as e:
            await _show(ctx, remote, "The voice server didn't answer", 8)
            raise ctx.Error(f"the voice server did not transcribe it: {e}") from None
        info["heard_by"] = "voice server"
        if not text.strip():
            await _show(ctx, remote, "Didn't catch that", 6)
            return {"heard": "", "audio": info}
        said = {"text": text.strip()}
    s = await _session(ctx, remote)
    try:
        s["next"] = (await ctx.call("speech.read", stream=s["stream"], after=s["next"], limit=500))["next"]
    except ctx.Error:                        # the session is gone (closed, or the hub restarted): a new one
        s = await _session(ctx, remote, fresh=True)
    try:
        out = await ctx.call("speech.hear", stream=s["stream"], final=True, **said)
    except ctx.Error as e:
        no_ears = "voice.stt" in str(e)
        await _show(ctx, remote, "The hub has no speech-to-text yet" if no_ears else "That didn't work", 8)
        raise
    heard = (out.get("heard") or "").strip()
    info["to_heard_seconds"] = round(time.monotonic() - t_start, 1)
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
