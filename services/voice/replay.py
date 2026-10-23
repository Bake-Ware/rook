"""Replay set for the voice server: synthetic turns streamed like the phone does.

    python -m services.voice.replay make --url https://host:8900 --out DIR
    python -m services.voice.replay run  --url wss://host:8900/ws --dir DIR [--insecure] [--only a,b]

``make`` renders the cases' speech with the server's own TTS (``POST /api/voice``):
the user lines in a voice other than the assistant's, the echo lines in the
assistant's voice passed through a crude phone channel (band-pass, 8 kHz round
trip, noise, -10 dB). Noise and silence are generated here. Files are 16 kHz
mono WAV plus ``manifest.json``; keep them outside Git.

``run`` plays every case over a protocol-2 socket in real time (20 ms frames,
as the Android client sends them, with ``aec`` and ``barge: confirm``), acks
playback as if played at once, and checks what the server did: no turn for
silence, noise and echo; one turn per question; a merged turn for a split
question; ``interrupt`` for a real barge-in and ``resume`` for a false one.
Prints one JSON line per case and exits non-zero if any required check failed.
Cases marked ``informational`` (the echo guard while it is in shadow mode)
are reported but never fail the run.
"""
import argparse
import asyncio
import contextlib
import io
import json
import os
import ssl
import sys
import time
import uuid
import wave

import numpy as np

SR = 16000
FRAME = 320        # samples per 20 ms frame

USER_VOICE = os.environ.get('VOICE_REPLAY_USER_VOICE', 'am_michael')

# (name, steps, expect). A step is ('say', text) in the user's voice, ('echo', text)
# in the assistant's voice through the phone channel, ('silence', s) or
# ('noise', s, dBFS). Steps prefixed 'barge_' are sent while the reply plays.
CASES = [
    ('silence', [('silence', 3.0)], {'turns': 0}),
    ('noise_low', [('noise', 2.5, -45)], {'turns': 0}),
    ('noise_burst', [('noise', 0.7, -18), ('silence', 1.5)], {'turns': 0}),
    ('hum', [('hum', 2.0, -25)], {'turns': 0}),
    ('question_capital', [('say', 'What is the capital of France?')],
     {'turns': 1, 'stt': 'france', 'reply': 'paris'}),
    ('question_math', [('say', 'What is seven times eight?')], {'turns': 1, 'reply': '56|fifty'}),
    ('question_joke', [('say', 'Tell me a short joke.')], {'turns': 1}),
    ('split_question', [('say', 'What is the tallest mountain'), ('silence', 0.8), ('say', 'in Africa?')],
     {'last_stt': 'mountain.*africa', 'reply': 'kilimanjaro'}),
    ('barge_real', [('say', 'Tell me a long story about a dragon and a knight.'),
                    ('barge_say', 'Stop. What is two plus two?')],
     {'interrupt': True, 'last_stt': 'two plus two|2 \\+ 2|2\\+2', 'reply': 'four|4'}),
    ('barge_noise', [('say', 'Tell me a long story about a lighthouse keeper.'), ('barge_noise', 0.6, -20)],
     {'interrupt': False, 'resume': True}),
    ('echo_during_reply', [('say', 'Tell me a long story about a sleepy cat.'),
                           ('barge_echo', 'Once upon a time there was a very sleepy cat.')],
     {'interrupt': False, 'informational': True}),
]


# --- audio helpers ---------------------------------------------------------------
def to16k(pcm, rate):
    x = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768
    if rate == SR:
        return x
    n = int(len(x) * SR / rate)
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def phone_channel(x, rng):
    """Speaker -> room -> phone mic, roughly: 300-3400 Hz, 8 kHz round trip, noise, -10 dB."""
    spec = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(len(x), 1 / SR)
    spec[(freqs < 300) | (freqs > 3400)] = 0
    y = np.fft.irfft(spec, len(x))
    y = np.interp(np.arange(len(y)), np.arange(0, len(y), 2), y[::2])
    y = y * 10 ** (-10 / 20)
    rms = np.sqrt(np.mean(y * y)) or 1e-6
    y = y + rng.normal(0, rms / 10 ** (15 / 20), len(y))
    return y.astype(np.float32)


def noise(seconds, dbfs, rng):
    y = rng.normal(0, 1, int(seconds * SR)).astype(np.float32)
    return y / np.sqrt(np.mean(y * y)) * 10 ** (dbfs / 20)


def hum(seconds, dbfs):
    t = np.arange(int(seconds * SR)) / SR
    y = np.sin(2 * np.pi * 60 * t) + .5 * np.sin(2 * np.pi * 120 * t) + .25 * np.sin(2 * np.pi * 180 * t)
    return (y / np.sqrt(np.mean(y * y)) * 10 ** (dbfs / 20)).astype(np.float32)


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype('<i2').tobytes()
    with wave.open(str(path), 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SR); f.writeframes(pcm)


def read_wav(path):
    with wave.open(str(path), 'rb') as f:
        return to16k(f.readframes(f.getnframes()), f.getframerate())


# --- make --------------------------------------------------------------------------
async def make(url, out, insecure=False):
    import httpx
    os.makedirs(out, exist_ok=True)
    rng = np.random.default_rng(7)
    base = url.rstrip('/')
    async with httpx.AsyncClient(verify=not insecure, timeout=60) as client:
        voices = (await client.get(base + '/voices')).json()
        assistant = voices.get('default')
        async def tts(text, voice):
            for _ in range(10):
                r = await client.post(base + '/api/voice', json={'text': text, 'voice': voice})
                if r.status_code != 503:
                    break
                await asyncio.sleep(1)
            r.raise_for_status()
            with wave.open(io.BytesIO(r.content), 'rb') as f:
                return to16k(f.readframes(f.getnframes()), f.getframerate())
        manifest = []
        for name, steps, expect in CASES:
            files = []
            for i, step in enumerate(steps):
                kind = step[0].removeprefix('barge_')
                if kind == 'silence':
                    files.append({'step': step[0], 'silence': step[1]})
                    continue
                if kind == 'say':
                    audio = await tts(step[1], USER_VOICE)
                elif kind == 'echo':
                    audio = phone_channel(await tts(step[1], assistant), rng)
                elif kind == 'noise':
                    audio = noise(step[1], step[2], rng)
                else:
                    audio = hum(step[1], step[2])
                path = os.path.join(out, f'{name}-{i}.wav')
                write_wav(path, audio)
                files.append({'step': step[0], 'wav': os.path.basename(path)})
            manifest.append({'name': name, 'steps': files, 'expect': expect})
    with open(os.path.join(out, 'manifest.json'), 'w') as f:
        json.dump({'assistant_voice': assistant, 'user_voice': USER_VOICE, 'cases': manifest}, f, indent=1)
    print(f'wrote {len(manifest)} cases to {out} (assistant voice {assistant}, user voice {USER_VOICE})')


# --- run ---------------------------------------------------------------------------
class Session:
    def __init__(self, ws):
        self.ws = ws
        self.events = []
        self.audio = {}            # turn -> samples received
        self.first_audio = {}      # turn -> monotonic time
        self.sr = 24000
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        with contextlib.suppress(Exception):
            async for raw in self.ws:
                now = time.monotonic()
                if isinstance(raw, bytes):
                    if raw[:4] != b'RK2A':
                        continue
                    turn = int.from_bytes(raw[4:8], 'big')
                    self.audio[turn] = self.audio.get(turn, 0) + (len(raw) - 8) // 2
                    self.first_audio.setdefault(turn, now)
                    # Acknowledge as if played on arrival (the phone acks its play head).
                    await self.ws.send(json.dumps({'type': 'playback', 'turn': turn, 'frames': self.audio[turn]}))
                    continue
                event = json.loads(raw)
                event['_t'] = now
                self.events.append(event)
                if event.get('type') == 'audio_sr':
                    self.sr = event.get('sr', self.sr)

    def kinds(self, kind, since=0.0):
        return [e for e in self.events if e.get('type') == kind and e['_t'] >= since]

    async def stream(self, audio):
        """Real-time 20 ms frames; returns the time the last frame was sent."""
        pcm = (np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes()
        start = time.monotonic()
        for i in range(0, len(pcm) - FRAME * 2 + 1, FRAME * 2):
            await self.ws.send(pcm[i:i + FRAME * 2])
            await asyncio.sleep(max(0.0, start + (i // (FRAME * 2) + 1) * .02 - time.monotonic()))
        return time.monotonic()

    def playing_until(self):
        """When the received audio of the newest turn finishes playing, by the clock."""
        if not self.first_audio:
            return 0.0
        turn = max(self.first_audio)
        return self.first_audio[turn] + self.audio[turn] / self.sr

    async def idle(self, timeout=45):
        """Wait until the server finished the turn and its audio has played."""
        end = time.monotonic() + timeout
        quiet_since = time.monotonic()
        count = len(self.events)
        while time.monotonic() < end:
            await asyncio.sleep(.2)
            if len(self.events) != count or time.monotonic() < self.playing_until():
                count, quiet_since = len(self.events), time.monotonic()
            elif time.monotonic() - quiet_since > 2.5:
                return True
        return False


def load_audio(directory, step):
    if 'silence' in step:
        return np.zeros(int(step['silence'] * SR), dtype=np.float32)
    return read_wav(os.path.join(directory, step['wav']))


async def run_case(url, headers, tls, directory, case):
    import websockets
    import re
    ws = await websockets.connect(url, additional_headers=headers, max_size=8 * 1024 * 1024,
                                  **({'ssl': tls} if tls else {}))
    s = Session(ws)
    await ws.send(json.dumps({'type': 'hello', 'protocol': 2, 'conversation': str(uuid.uuid4()), 'aec': True,
                              'barge': 'confirm', 'pipeline': 'front_background', 'activity': True,
                              'client': 'rook-replay'}))
    result = {'case': case['name']}
    try:
        await asyncio.sleep(1.0)
        began = time.monotonic()
        speech_end = barge_t = None
        for step in case['steps']:
            audio = load_audio(directory, step)
            if step['step'].startswith('barge_'):
                # Wait for the reply's audio, let it play a moment, then talk over it like the phone does.
                end = time.monotonic() + 30
                while not s.first_audio and time.monotonic() < end:
                    await asyncio.sleep(.05)
                await asyncio.sleep(1.2)
                barge_t = time.monotonic()
                result['barge_at_ms'] = int((barge_t - began) * 1000)
                await ws.send(json.dumps({'type': 'speech_start', 'hold': True}))
            speech_end = await s.stream(audio)
        # Trailing silence so the server's endpointing can close the utterance.
        await s.stream(np.zeros(int(1.2 * SR), dtype=np.float32))
        await s.idle()
        stts = [e['text'] for e in s.kinds('stt', began)]
        replies = ' '.join(e['text'] for e in s.kinds('assistant_delta', began))
        result.update(stt=stts, reply=replies[:300], interrupts=len(s.kinds('interrupt', barge_t)) if barge_t else 0,
                      resumes=len(s.kinds('resume', began)),
                      turns=len({e.get('turn') for e in s.kinds('stt', began)}))
        firsts = sorted(s.first_audio.values())
        if speech_end and firsts:
            after = [t for t in firsts if t > speech_end - 0.05]
            if after:
                result['first_audio_after_speech_ms'] = int((after[0] - speech_end) * 1000)
        stt_events = s.kinds('stt', began)
        if stt_events and speech_end:
            result['stt_after_speech_ms'] = int((stt_events[-1]['_t'] - speech_end) * 1000)
        expect, failures = case['expect'], []
        if 'turns' in expect and result['turns'] != expect['turns']:
            failures.append(f"turns {result['turns']} != {expect['turns']}")
        if 'stt' in expect and not any(re.search(expect['stt'], t.lower()) for t in stts):
            failures.append('stt missing ' + expect['stt'])
        if 'last_stt' in expect and not (stts and re.search(expect['last_stt'], stts[-1].lower())):
            failures.append('last stt missing ' + expect['last_stt'])
        if 'reply' in expect and not re.search(expect['reply'], replies.lower()):
            failures.append('reply missing ' + expect['reply'])
        if 'interrupt' in expect and bool(result['interrupts']) != expect['interrupt']:
            failures.append(f"interrupt {bool(result['interrupts'])} != {expect['interrupt']}")
        if expect.get('resume') and not result['resumes']:
            failures.append('no resume')
        result['ok'] = not failures
        if failures:
            result['failures'] = failures
        if expect.get('informational'):
            result['informational'] = True
    finally:
        await ws.close()
        s.reader.cancel()
    return result


async def run(url, directory, insecure=False, only=None, token=None):
    with open(os.path.join(directory, 'manifest.json')) as f:
        manifest = json.load(f)
    tls = None
    if url.startswith('wss://') and insecure:
        tls = ssl.create_default_context(); tls.check_hostname = False; tls.verify_mode = ssl.CERT_NONE
    headers = {'Authorization': 'Bearer ' + token} if token else {}
    results = []
    for case in manifest['cases']:
        if only and case['name'] not in only:
            continue
        try:
            result = await run_case(url, headers, tls, directory, case)
        except Exception as error:
            result = {'case': case['name'], 'ok': False, 'failures': [f'{type(error).__name__}: {error}'[:200]]}
        results.append(result)
        print(json.dumps(result), flush=True)
    required = [r for r in results if not r.get('informational')]
    failed = [r['case'] for r in required if not r['ok']]
    lat = [r['first_audio_after_speech_ms'] for r in results if 'first_audio_after_speech_ms' in r
           and r['case'].startswith('question')]
    print(json.dumps({'summary': True, 'cases': len(results), 'failed': failed,
                      'first_audio_after_speech_ms': lat}), flush=True)
    return not failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    sub = parser.add_subparsers(dest='cmd', required=True)
    m = sub.add_parser('make')
    m.add_argument('--url', required=True)
    m.add_argument('--out', required=True)
    m.add_argument('--insecure', action='store_true')
    r = sub.add_parser('run')
    r.add_argument('--url', required=True)
    r.add_argument('--dir', required=True)
    r.add_argument('--insecure', action='store_true')
    r.add_argument('--only', default='')
    args = parser.parse_args(argv)
    if args.cmd == 'make':
        asyncio.run(make(args.url, args.out, args.insecure))
        return 0
    ok = asyncio.run(run(args.url, args.dir, args.insecure, set(filter(None, args.only.split(','))),
                         os.environ.get('VOICE_TOKEN')))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
