"""Optional embeddings for memory: a band capability, an HTTP service, or an
OpenAI-compatible ``/v1/embeddings`` endpoint. The hub never runs a model.

Wire shapes:

* ``cap://<worker|any>/embed.text`` and ``http(s)://.../embed``: the knowledge
  plugin's contract, ``{"texts": [...]}`` in, ``{"model", "vectors"}`` out
  (a bare list of vectors is accepted too);
* OpenAI-compatible: ``POST <base>/embeddings`` with ``{"model", "input"}``,
  ``{"data": [{"embedding": [...]}]}`` out. Used with the home agent's own
  endpoint and key when ``memory.embed_model`` is set.

Sources are tried in order; the first that answers wins. Each vector is
stored with the model that produced it and only compared with vectors of
the same model, so changing the embedder degrades to keyword matching until
the consolidation job has re-embedded the rows. A failing source is backed
off for ``BACKOFF`` seconds so chat replies stay fast when nothing serves
embeddings. Ported from Bake-Ware/rook PR #31 (embedder.py).
"""

from __future__ import annotations

import array
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

log = logging.getLogger("rook.hub.plugins.memory")

BACKOFF = 60.0
TIMEOUT = 8.0


@dataclass
class Source:
    """One place embeddings can come from. ``call(texts) -> data`` returns
    the service's raw reply; ``shape`` says how to read it."""
    name: str                                   # shown on the status line (no credentials)
    call: Callable[[list], Awaitable[Any]]
    shape: str = "rook"                         # rook | openai
    model: str = ""                             # required model; "" = what the service reports
    down_until: float = field(default=0.0)


class Embedder:
    def __init__(self, sources: list[Source] | None = None) -> None:
        self.sources = list(sources or [])
        self.last_error: str | None = None
        self.last_model: str | None = None
        self.last_source: str | None = None

    @property
    def configured(self) -> bool:
        return bool(self.sources)

    def describe(self) -> list[str]:
        return [s.name for s in self.sources]

    def available(self) -> bool:
        now = time.monotonic()
        return any(now >= s.down_until for s in self.sources)

    async def embed(self, texts: list[str]) -> tuple[str, list[list[float]]] | None:
        """``(model, unit vectors)`` from the first source that answers, or
        ``None`` (the caller falls back to keywords)."""
        if not texts:
            return None
        now = time.monotonic()
        for src in self.sources:
            if now < src.down_until:
                continue
            try:
                data = await src.call(list(texts))
                model, vectors = check(data, len(texts), src.shape, src.model)
            except Exception as e:  # noqa: BLE001 - any failure means "not this source now"
                self.last_error = f"{src.name}: {type(e).__name__}: {e}"[:200]
                src.down_until = time.monotonic() + BACKOFF
                log.debug("memory: embedding via %s failed: %s", src.name, self.last_error)
                continue
            self.last_error, self.last_model, self.last_source = None, model, src.name
            return model, vectors
        return None


def check(data: Any, n: int, shape: str = "rook", want: str = "") -> tuple[str, list[list[float]]]:
    if shape == "openai":
        rows = data.get("data") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("invalid embedding response")
        rows = sorted(rows, key=lambda r: r.get("index", 0) if isinstance(r, dict) else 0)
        data = {"model": (data.get("model") or want), "vectors": [
            r.get("embedding") if isinstance(r, dict) else None for r in rows]}
    if isinstance(data, list):
        data = {"model": want or "unknown", "vectors": data}
    if not isinstance(data, dict) or not isinstance(data.get("vectors"), list):
        raise ValueError("invalid embedding response")
    model = str(data.get("model") or want or "unknown")
    if want and model != want and shape != "openai":
        raise ValueError(f"embedding model mismatch: the service has {model!r}, memory wants {want!r}")
    vectors = data["vectors"]
    dims = {len(v) for v in vectors if isinstance(v, list)}
    if len(vectors) != n or len(dims) != 1 or 0 in dims:
        raise ValueError("invalid embedding response")
    out = []
    for v in vectors:
        if any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x)
               for x in v):
            raise ValueError("invalid embedding response")
        out.append(unit(v))
    return model, out


def unit(v) -> list[float]:
    norm = math.sqrt(sum(float(x) * float(x) for x in v)) or 1.0
    return [float(x) / norm for x in v]


def pack(v: list[float]) -> bytes:
    return array.array("f", v).tobytes()


def unpack(blob: bytes) -> array.array:
    a = array.array("f")
    a.frombytes(blob)
    return a


def cosine(a, b) -> float:
    """Dot product of two unit vectors."""
    return sum(x * y for x, y in zip(a, b))
