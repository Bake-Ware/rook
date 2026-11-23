"""Talking to the house agent from the Deck, the way the firmware remote does it.

The firmware passes on what its Bluetooth remote's microphone sends: Google's
voice-over-BLE frames (ATVV 0.4), 134 bytes each: sequence (2, big-endian),
remote id (1), the IMA ADPCM predictor (2, big-endian) and step index (1) the
frame starts from, then 128 bytes, 256 samples of 4-bit IMA ADPCM, high nibble
first. They leave as remote.voice.part events (base64, a few frames each)
while it is spoken, then remote.voice; the hub's remote-voice plugin decodes
them, hears them and answers with remote.chat. The Deck makes the same frames
from its own microphone (pw-record, 16 kHz mono: codec adpcm16k), so the hub
can't tell it from a remote.

Standard library only (the worker plugin may import it); the recording runs on
a thread, so encoding never holds up a frame."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import threading
import time
from typing import Callable

RATE = 16000
CODEC = "adpcm16k"
SAMPLES = 256                     # a frame's samples
FRAME = 134
FRAMES_PER_PART = 13              # ~1.7 KB a part, as the firmware's (it takes up to 1800 bytes at a time)
MAX_S = 30.0                      # the hub hears at most 30 s

STEPS = (7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45, 50, 55, 60, 66, 73, 80,
         88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
         598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749,
         3024, 3327, 3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635,
         13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767)
INDEX = (-1, -1, -1, -1, 2, 4, 6, 8)


class Encoder:
    """16-bit samples to ATVV frames. The decoder's own state update after each
    nibble, so encoder and decoder never drift apart."""

    def __init__(self, remote_id: int = 0):
        self.pred = 0
        self.index = 0
        self.seq = 0
        self.rid = remote_id & 255

    def frame(self, samples) -> bytes:
        """One frame of exactly SAMPLES samples."""
        assert len(samples) == SAMPLES
        pred, index = self.pred, self.index
        head = bytes([(self.seq >> 8) & 255, self.seq & 255, self.rid, (pred >> 8) & 255, pred & 255, index])
        out = bytearray(SAMPLES // 2)
        steps, ix = STEPS, INDEX
        hi = 0
        for i, s in enumerate(samples):
            step = steps[index]
            diff = s - pred
            n = 0
            if diff < 0:
                n = 8
                diff = -diff
            if diff >= step:
                n |= 4
                diff -= step
            if diff >= step >> 1:
                n |= 2
                diff -= step >> 1
            if diff >= step >> 2:
                n |= 1
            d = step >> 3                                   # the decoder's reconstruction
            if n & 4:
                d += step
            if n & 2:
                d += step >> 1
            if n & 1:
                d += step >> 2
            pred = pred - d if n & 8 else pred + d
            if pred > 32767:
                pred = 32767
            elif pred < -32768:
                pred = -32768
            index += ix[n & 7]
            if index < 0:
                index = 0
            elif index > 88:
                index = 88
            if i & 1:
                out[i >> 1] = hi | n
            else:
                hi = n << 4
        self.pred, self.index = pred, index
        self.seq = (self.seq + 1) & 0xFFFF
        return head + bytes(out)


def mic_command(target: str | None = None) -> list[str]:
    """pw-record: 16 kHz mono 16-bit raw to stdout. target: a PipeWire node (name or
    serial); none: the default input (the Deck's internal microphone, unless a
    headset is in). ROOKMOTE_MIC sets it too."""
    cmd = ["pw-record", "--rate", str(RATE), "--channels", "1", "--format", "s16", "--raw",
           "--media-role", "Communication", "--latency", "40ms"]
    target = target or os.environ.get("ROOKMOTE_MIC")
    if target:
        cmd += ["--target", target]
    return cmd + ["-"]


class Recording:
    """One utterance: the microphone, framed and handed on in parts as it comes.

    on_part(seq, data) for each part (bytes of whole frames), then on_done(summary)
    once: {parts, bytes, ms, seconds, peak, error?}. level is the last ~30 ms's peak
    (0..1) for a meter. stop() ends it; it also ends by itself after MAX_S."""

    def __init__(self, on_part: Callable[[int, bytes], None], on_done: Callable[[dict], None],
                 command: list[str] | None = None, source=None):
        self.on_part, self.on_done = on_part, on_done
        self.level = 0.0
        self.peak = 0
        self.started = time.monotonic()
        self.stopping = threading.Event()
        self.enc = Encoder()
        self.parts = 0
        self.bytes = 0
        self.samples = 0
        self.error = None
        self.proc = None
        self._source = source                      # tests: a file-like of raw s16le audio instead of pw-record
        if source is None:
            cmd = command or mic_command()
            if not shutil.which(cmd[0]):
                raise OSError(f"{cmd[0]} is not installed")
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            self._source = self.proc.stdout
        self.thread = threading.Thread(target=self._run, name="rookmote-mic", daemon=True)
        self.thread.start()

    @property
    def seconds(self) -> float:
        return self.samples / RATE

    def stop(self) -> None:
        self.stopping.set()
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass

    def _run(self) -> None:
        import array
        import sys
        pending = array.array("h")
        frames: list[bytes] = []
        try:
            while not self.stopping.is_set():
                chunk = self._source.read(1024)          # 512 samples: 32 ms
                if not chunk:
                    break
                a = array.array("h")
                a.frombytes(chunk[:len(chunk) // 2 * 2])
                if sys.byteorder != "little":
                    a.byteswap()
                pending.extend(a)
                if a:
                    pk = max(max(a), -min(a))
                    self.level = min(1.0, pk / 20000)
                    self.peak = max(self.peak, pk)
                while len(pending) >= SAMPLES and self.samples + SAMPLES <= MAX_S * RATE:
                    frames.append(self.enc.frame(pending[:SAMPLES]))
                    del pending[:SAMPLES]
                    self.samples += SAMPLES
                    if len(frames) >= FRAMES_PER_PART:
                        self._part(frames)
                        frames = []
                if self.samples + SAMPLES > MAX_S * RATE:
                    pending = array.array("h")
                    break
        except (OSError, ValueError) as e:
            self.error = str(e)
        finally:
            self.stop()
            if len(pending):           # the last bit, padded with silence
                frames.append(self.enc.frame(list(pending) + [0] * (SAMPLES - len(pending))))
                self.samples += len(pending)
            if frames:
                self._part(frames)
            if self.proc:
                try:
                    err = self.proc.stderr.read().decode(errors="replace").strip() if self.proc.stderr else ""
                    code = self.proc.wait(timeout=2)
                    if self.parts == 0 and err and not self.error:
                        self.error = err.splitlines()[-1][:200]
                    elif self.parts == 0 and code not in (0, -15) and not self.error:
                        self.error = f"pw-record exited {code}"
                except (OSError, subprocess.TimeoutExpired):
                    pass
            self.level = 0.0
            out = {"parts": self.parts, "bytes": self.bytes, "ms": int((time.monotonic() - self.started) * 1000),
                   "seconds": round(self.seconds, 2), "peak": self.peak}
            if self.error:
                out["error"] = self.error
            self.on_done(out)

    def _part(self, frames: list[bytes]) -> None:
        data = b"".join(frames)
        self.on_part(self.parts, data)
        self.parts += 1
        self.bytes += len(data)


def part_event(uid: str, seq: int, data: bytes) -> dict:
    return {"id": uid, "seq": seq, "data": base64.b64encode(data).decode()}


def done_event(uid: str, summary: dict, worker: str | None = None) -> dict:
    out = {"id": uid, "parts": summary["parts"], "bytes": summary["bytes"], "codec": CODEC, "ms": summary["ms"]}
    if worker:
        out["worker"] = worker
    return out


def new_id() -> str:
    """As the firmware's: 8 hex digits, odd."""
    return f"{int.from_bytes(os.urandom(4), 'big') | 1:08x}"
