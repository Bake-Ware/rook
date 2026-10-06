#!/usr/bin/env python3
"""Chatterbox Turbo synthesis worker for the voice server (see tts.py).

Run with the Python of a venv that has torch and chatterbox-tts installed; the
voice server starts it when VOICE_CHATTERBOX_PYTHON points at that Python. It
must not import anything from the voice service: it runs outside that venv.

Protocol on stdin/stdout:
  start  -> {"ready": true, "sr": 24000, "voices": ["default", ...]}  (or {"ready": false, "error": ...})
  request  {"text": "...", "voice": "default"}  (one JSON line)
  reply  -> {"ok": true, "sr": 24000, "bytes": N}  then N bytes of little-endian int16 mono PCM
            {"ok": false, "error": "..."}  on failure

Environment: CHATTERBOX_DEVICE (e.g. cuda:0), CHATTERBOX_VOICES_DIR (optional:
every <name>.wav there, 5-20 s of clean speech, becomes voice "<name>").
"""
import json, os, sys


class Synth:
    def __init__(self, device, voices_dir=None):
        import torch
        from chatterbox.tts_turbo import ChatterboxTurboTTS
        self.torch = torch
        self.model = ChatterboxTurboTTS.from_pretrained(device=device)
        self.sr = int(self.model.sr)
        self.conds = {}
        if getattr(self.model, "conds", None) is not None:
            self.conds["default"] = self.model.conds
        if voices_dir and os.path.isdir(voices_dir):
            for file in sorted(os.listdir(voices_dir)):
                stem, ext = os.path.splitext(file)
                if ext.lower() not in (".wav", ".flac", ".mp3") or not stem.replace("_", "").replace("-", "").isalnum():
                    continue
                try:
                    # Without inference_mode autograd keeps ~270 MB of GPU activations alive per voice.
                    with torch.inference_mode():
                        self.model.prepare_conditionals(os.path.join(voices_dir, file))
                    self.conds[stem.lower()] = self.model.conds
                except Exception as error:   # a bad clip only drops that voice
                    print(f"chatterbox: skipped voice {file}: {error}", file=sys.stderr)
        if not self.conds:
            raise RuntimeError("no Chatterbox voice: the model has no built-in voice and no reference clips loaded")
        with torch.inference_mode():         # warm-up: the first call compiles kernels
            self.synthesize("Hello.", next(iter(self.conds)))

    def ready(self):
        return {"ready": True, "sr": self.sr, "voices": list(self.conds)}

    def synthesize(self, text, voice):
        import numpy as np
        self.model.conds = self.conds.get(voice) or self.conds.get("default") or next(iter(self.conds.values()))
        with self.torch.inference_mode():
            wav = self.model.generate(text)
        return np.asarray(wav.squeeze(0).float().cpu().numpy(), dtype=np.float32), self.sr


def main():
    out = sys.stdout.buffer
    sys.stdout = sys.stderr      # library prints must not corrupt the pipe

    def send(obj, data=b""):
        out.write((json.dumps(obj) + "\n").encode() + data)
        out.flush()

    try:
        synth = Synth(os.environ.get("CHATTERBOX_DEVICE", "cuda:0"), os.environ.get("CHATTERBOX_VOICES_DIR") or None)
    except Exception as error:
        send({"ready": False, "error": f"{type(error).__name__}: {error}"[:300]})
        return 1
    send(synth.ready())
    import numpy as np
    for line in sys.stdin.buffer:
        try:
            request = json.loads(line)
            text = str(request.get("text", "")).strip()
            if not text:
                raise ValueError("empty text")
            samples, sr = synth.synthesize(text[:2000], str(request.get("voice") or "default"))
            pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
            send({"ok": True, "sr": sr, "bytes": len(pcm)}, pcm)
        except Exception as error:
            send({"ok": False, "error": f"{type(error).__name__}: {error}"[:300]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
