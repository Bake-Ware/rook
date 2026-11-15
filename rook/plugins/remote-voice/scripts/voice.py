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
import os
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
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


# Run on the voice_via worker: fetch the frames from the hub (an address that
# answers once), decode them, ask the voice server, print what it heard. (The same decoding as decode() above.)
AWAY = r'''
import json, ssl, struct, sys, urllib.request
src, url, token = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else ""
# (a name of its own: a front door may turn Python's default user agent away)
with urllib.request.urlopen(urllib.request.Request(src, headers={"User-Agent": "rook-remote-voice"}), timeout=15) as r:
    raw = r.read()
S = (7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,107,118,130,143,157,173,190,209,
     230,253,279,307,337,371,408,449,494,544,598,658,724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,
     2749,3024,3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,13899,15289,16818,18500,
     20350,22385,24623,27086,29794,32767)
I = (-1, -1, -1, -1, 2, 4, 6, 8)
pcm = []
for at in range(0, len(raw) - 133, 134):
    f = raw[at:at + 134]
    pred, idx = struct.unpack_from(">h", f, 3)[0], min(f[5], 88)
    for b in f[6:]:
        for n in (b >> 4, b & 15):
            st = S[idx]
            d = (st >> 3) + (st if n & 4 else 0) + (st >> 1 if n & 2 else 0) + (st >> 2 if n & 1 else 0)
            pred = max(-32768, min(32767, pred - d if n & 8 else pred + d))
            idx = max(0, min(88, idx + I[n & 7]))
            pcm.append(pred)
wide = []
for i, x in enumerate(pcm):
    wide += [x, (x + pcm[i + 1]) // 2 if i + 1 < len(pcm) else x]
wide = wide[:30 * 16000]
headers = {"Content-Type": "audio/L16"}
if token:
    headers["Authorization"] = "Bearer " + token
tls = ssl.create_default_context()
tls.check_hostname = False
tls.verify_mode = ssl.CERT_NONE
req = urllib.request.Request(url, data=struct.pack("<%dh" % len(wide), *wide), method="POST", headers=headers)
with urllib.request.urlopen(req, timeout=40, context=tls) as r:
    print(json.dumps({"text": json.loads(r.read()).get("text") or ""}))
'''


async def transcribe(ctx, frames: bytes, pcm: list[int]) -> str:
    """What the voice server hears: its POST /api/transcribe, the way the
    phone app asks it (audio/L16 at 16 kHz, a bearer key when it wants one).
    The server signs its own certificate, so it is not checked. With
    voice_via a worker on the server's network asks instead (the hub often
    cannot reach a voice server at home). It fetches the frames from the hub,
    from an address that answers once (hub_url/once/...): audio in a call's
    arguments would be kept in the journal, and a big message over the band
    does not arrive. The journal keeps the words (speech.hear's text)."""
    st = ctx.settings
    u, token, via = _server(st["voice_url"]), st["voice_token"] or "", st["voice_via"]
    if not via:
        headers = {"Content-Type": "audio/L16", **({"Authorization": f"Bearer {token}"} if token else {})}
        return await asyncio.to_thread(_direct, u, headers, struct.pack(f"<{len(pcm)}h", *pcm))
    hub = (st["hub_url"] or "").strip().rstrip("/")
    if not hub:
        raise ValueError("voice_via needs hub_url: the address the worker fetches the audio from")
    folder = ctx.data / "once"
    last: Exception | None = None
    for attempt in range(2):                 # a lost call: hand it out again
        name = os.urandom(24).hex()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / name).write_bytes(frames)
            got = await asyncio.wait_for(ctx.call_on(via, "shell.exec", timeout=50, argv=[
                "python3", "-c", AWAY, f"{hub}/once/remote-voice/{name}", u.geturl() + "/api/transcribe",
                *([token] if token else [])]), 55)
            if got.get("code") != 0:
                raise ValueError((got.get("stderr") or "").strip()[-200:] or f"exit {got.get('code')}")
            return str(json.loads(got.get("stdout") or "{}").get("text") or "")
        except (asyncio.TimeoutError, ctx.Error, ValueError, OSError) as e:
            last = e
        finally:
            for left in (folder / name, folder / (name + ".taken")):
                try:
                    left.unlink(missing_ok=True)
                except OSError:
                    pass
    raise ValueError(f"{type(last).__name__}: {last}")


PLAIN: set[str] = set()      # remotes whose firmware has no conversation view (remote.chat): plain text for them


async def _show(ctx, remote: str, text: str, hold: int = 60, who: str = "note") -> None:
    """One more line of the conversation on the remote's screen: what the person
    said (who you), what the agent said (agent), or a note about it. A remote
    whose firmware has no conversation view gets it as plain text for hold seconds."""
    for attempt in range(4):             # a remote that just stopped listening can miss a call: ask again soon
        try:
            if remote in PLAIN:
                shown = f"> {text}" if who == "you" else text
                await asyncio.wait_for(ctx.call_on(remote, "remote.display", text=shown[:6000], seconds=hold), 4)
            else:
                await asyncio.wait_for(ctx.call_on(remote, "remote.chat", who=who, text=text[:700]), 4)
            return
        except asyncio.TimeoutError:
            pass
        except ctx.Error as e:
            if remote not in PLAIN and "unknown cap" in str(e).lower():
                PLAIN.add(remote)        # an older remote: say it the old way, now
            # anything else: the remote went away; after the last try there is nothing to show it on


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
    if p.get("replay") and (ctx.data / "last.raw").is_file():      # the utterance before, again (to test with)
        u = {"parts": {0: (ctx.data / "last.raw").read_bytes()}}
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
            text = await transcribe(ctx, raw, pcm)
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
    await _show(ctx, remote, heard, 20 if handoff else 60, "you")
    if say:
        await _show(ctx, remote, say, 60, "agent")
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
        await _show(ctx, remote, reply or "(no answer yet)", 120 if reply else 8, "agent" if reply else "note")
    return {"heard": heard, "say": say, "reply": reply, "handoff": handoff, "audio": info}
