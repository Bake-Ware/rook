#!/usr/bin/env python3
"""Speaker-embedding worker for the voice server (see speaker.py).

Run with the Python of a venv that has sherpa-onnx installed (no torch); the
voice server starts it when VOICE_SPEAKER_PYTHON points at that Python. It must
not import anything from the voice service: it runs outside that venv.

Protocol on stdin/stdout:
  start    -> {"ready": true, "dim": 192, "model": "<file name>", "threads": 2}  (or {"ready": false, "error": ...})
  request     {"bytes": N}  (one JSON line) then N bytes of little-endian int16 mono PCM at 16 kHz
  reply    -> {"ok": true, "embedding": [...], "ms": 12}
              {"ok": false, "error": "..."}  on failure (the worker stays up)

Environment: SPEAKER_MODEL (path to a sherpa-onnx speaker model, e.g.
nemo_en_titanet_small.onnx), SPEAKER_THREADS (ONNX CPU threads, default 2).
"""
import json, os, sys, time

SAMPLE_RATE = 16000


class Embedder:
    """sherpa-onnx SpeakerEmbeddingExtractor; ``embed(float32 16 kHz samples) -> list[float]``."""

    def __init__(self, model, threads=2):
        import sherpa_onnx
        if not model or not os.path.isfile(model):
            raise FileNotFoundError(f"speaker model not found: {model!r}")
        config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=model, num_threads=int(threads), provider="cpu")
        if not config.validate():
            raise RuntimeError(f"invalid speaker model config: {model}")
        self.extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
        self.model, self.threads, self.dim = os.path.basename(model), int(threads), int(self.extractor.dim)
        import numpy as np
        self.embed(np.zeros(SAMPLE_RATE, dtype=np.float32) + 1e-4)   # warm-up: first run allocates

    def ready(self):
        return {"ready": True, "dim": self.dim, "model": self.model, "threads": self.threads}

    def embed(self, samples):
        stream = self.extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        stream.input_finished()
        if not self.extractor.is_ready(stream):
            raise ValueError("audio too short for an embedding")
        return list(self.extractor.compute(stream))


def main():
    out = sys.stdout.buffer
    sys.stdout = sys.stderr      # library prints must not corrupt the pipe

    def send(obj):
        out.write((json.dumps(obj) + "\n").encode())
        out.flush()

    try:
        embedder = Embedder(os.environ.get("SPEAKER_MODEL", ""), int(os.environ.get("SPEAKER_THREADS", "2") or 2))
    except Exception as error:
        send({"ready": False, "error": f"{type(error).__name__}: {error}"[:300]})
        return 1
    send(embedder.ready())
    import numpy as np
    stdin = sys.stdin.buffer
    for line in stdin:
        try:
            n = int(json.loads(line).get("bytes", 0))
            data = stdin.read(n) if n > 0 else b""
            if len(data) != n:
                return 0             # the parent went away mid-request
        except Exception as error:   # out of step with the parent: nothing sane to do but exit
            send({"ok": False, "error": f"bad request: {error}"[:300]})
            return 1
        try:
            started = time.perf_counter()
            samples = np.frombuffer(data[: len(data) // 2 * 2], dtype="<i2").astype(np.float32) / 32768
            vector = embedder.embed(samples)
            send({"ok": True, "embedding": [round(float(v), 6) for v in vector],
                  "ms": int((time.perf_counter() - started) * 1000)})
        except Exception as error:
            send({"ok": False, "error": f"{type(error).__name__}: {error}"[:300]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
