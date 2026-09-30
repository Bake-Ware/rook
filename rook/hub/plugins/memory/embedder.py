"""Embedding client for agent memory.

Same wire contract as the knowledge plugin's search (and the worker ``embed``
plugin): ``{"texts": [...]}`` in, ``{"model", "vectors"}`` out (a cap may
return a bare list of vectors). The hub never runs a model itself: the
service is a band capability (``cap://any/embed.text``) or an HTTP endpoint.

Unlike knowledge, memory does not insist on one configured model: each
vector is stored with the model that produced it and only compared with
vectors of the same model, so swapping the embedding worker degrades to
keyword matching until the background indexer has re-embedded the rows.
A failing service is backed off for ``BACKOFF`` seconds so writes and
recalls stay fast when no worker offers ``embed.text``.
"""
from __future__ import annotations

import array
import logging
import math
import time

log = logging.getLogger("rook.hub.plugins.memory")

BACKOFF = 60.0
TIMEOUT = 8.0


class Embedder:
    def __init__(self, resource=None, url: str = "", model: str = "") -> None:
        self.resource = resource
        self.url = url or ""
        self.model = model or ""          # required model; empty = accept what the service reports
        self.last_error: str | None = None
        self.last_model: str | None = None
        self._down_until = 0.0

    @property
    def configured(self) -> bool:
        return self.resource is not None or bool(self.url)

    @property
    def endpoint(self) -> str | None:
        if self.resource is not None:
            return self.resource.url
        return self.url.split("?")[0] if self.url else None

    def available(self) -> bool:
        return self.configured and time.monotonic() >= self._down_until

    async def embed(self, texts: list[str]) -> tuple[str, list[list[float]]] | None:
        """``(model, unit vectors)`` or ``None`` when the service is not
        configured, backed off, or fails (the caller falls back to keywords)."""
        if not texts or not self.available():
            return None
        try:
            if self.resource is not None:
                data = await self.resource.call({"texts": texts}, timeout=TIMEOUT)
            else:
                import aiohttp
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT)) as http:
                    async with http.post(self.url, json={"texts": texts}) as resp:
                        resp.raise_for_status()
                        data = await resp.json()
            model, vectors = self._check(data, len(texts))
        except Exception as e:  # noqa: BLE001 - any failure means "no vectors this time"
            self.last_error = f"{type(e).__name__}: {e}"[:200]
            self._down_until = time.monotonic() + BACKOFF
            log.debug("memory: embedding failed: %s", self.last_error)
            return None
        self.last_error = None
        self.last_model = model
        return model, vectors

    def _check(self, data, n: int) -> tuple[str, list[list[float]]]:
        if isinstance(data, list):
            data = {"model": self.model or "unknown", "vectors": data}
        if not isinstance(data, dict) or not isinstance(data.get("vectors"), list):
            raise ValueError("invalid embedding response")
        model = str(data.get("model") or self.model or "unknown")
        if self.model and model != self.model:
            raise ValueError(f"embedding model mismatch: service has {model!r}, memory wants {self.model!r}")
        vectors = data["vectors"]
        dims = {len(v) for v in vectors if isinstance(v, list)}
        if len(vectors) != n or len(dims) != 1 or 0 in dims:
            raise ValueError("invalid embedding response")
        out = []
        for v in vectors:
            if any(not isinstance(x, (int, float)) or isinstance(x, bool) or not math.isfinite(x) for x in v):
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
