"""embed.text: text embeddings as a band capability.

The hub never runs models. Plugins that need embeddings (``memory``,
``knowledge``) reach one through the resource ``cap://any/embed.text``, and
this plugin answers it on a worker that can afford a model:

    embed.text(texts=[...]) -> {"model": "...", "vectors": [[...], ...], "dim": N}

That is the same ``{texts} -> {model, vectors}`` shape the knowledge
plugin's HTTP embedding service uses, so either can serve either plugin.

Backends, tried in order when ``backend`` is ``auto``:

* ``sentence-transformers`` (PyTorch; uses a GPU when there is one);
* ``fastembed`` (ONNX Runtime, CPU): ``pip install 'rook[embed]'``.

Neither ships in the worker bundle. Without one installed ``available()`` is
false and the worker does not announce the cap. ``mode`` decides where it
loads: ``auto`` (default) only on workers whose facts report a GPU, ``on``
anywhere a backend is installed (a CPU box with fastembed), ``off`` never.
The model loads on the first call, in a thread, so start-up stays fast.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import math
import threading

from ..plugin import Plugin, capability, setting

log = logging.getLogger("rook.worker.plugins.embed")

DEFAULT_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TEXTS = 64
MAX_CHARS = 8000


def _st_loader(model: str):
    from sentence_transformers import SentenceTransformer  # type: ignore
    m = SentenceTransformer(model)

    def encode(texts):
        return [list(map(float, v)) for v in m.encode(list(texts), normalize_embeddings=True)]
    return encode


def _fastembed_loader(model: str):
    from fastembed import TextEmbedding  # type: ignore
    m = TextEmbedding(model_name=model)

    def encode(texts):
        return [list(map(float, v)) for v in m.embed(list(texts))]
    return encode


#: backend name -> (import name used to check it is installed, loader(model) -> encode(texts))
BACKENDS = {
    "sentence-transformers": ("sentence_transformers", _st_loader),
    "fastembed": ("fastembed", _fastembed_loader),
}


def installed(backend: str) -> bool:
    spec = BACKENDS.get(backend)
    if spec is None:
        return False
    try:
        return importlib.util.find_spec(spec[0]) is not None
    except (ImportError, ValueError):
        return False


def _has_gpu() -> bool:
    try:
        from ...core.facts import local_facts
        return bool(local_facts().get("gpu"))
    except Exception:  # noqa: BLE001
        return False


class EmbedPlugin(Plugin):
    NAMESPACE = "embed"
    SETTINGS = (
        setting("mode", str, default="auto", scope="worker", env="ROOK_EMBED_MODE",
                choices=("auto", "on", "off"), apply="restart", label="Serve embed.text",
                help="auto: only on workers with a GPU; on: wherever a backend is installed "
                     "(fastembed runs on CPU); off: never."),
        setting("backend", str, default="auto", scope="worker", env="ROOK_EMBED_BACKEND",
                choices=("auto", "sentence-transformers", "fastembed"), apply="restart",
                label="Backend"),
        setting("model", str, default=DEFAULT_MODEL, scope="worker", env="ROOK_EMBED_TEXT_MODEL",
                apply="restart", label="Model",
                help="Reported as `model` in every reply; clients compare vectors only within "
                     "one model."),
    )

    def __init__(self) -> None:
        super().__init__()
        self._encode = None
        self._backend: str | None = None
        self._load_lock = threading.Lock()

    def _pick_backend(self) -> str | None:
        want = self.settings.get("backend", "auto")
        order = list(BACKENDS) if want == "auto" else [want]
        return next((b for b in order if installed(b)), None)

    def available(self) -> bool:
        mode = self.settings.get("mode", "auto")
        if mode == "off":
            return False
        self._backend = self._pick_backend()
        if self._backend is None:
            return False
        return mode == "on" or _has_gpu()

    def _load(self):
        with self._load_lock:
            if self._encode is None:
                backend = self._backend or self._pick_backend()
                if backend is None:
                    raise RuntimeError("no embedding backend installed")
                model = self.settings.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
                log.info("embed: loading %s with %s", model, backend)
                self._encode = BACKENDS[backend][1](model)
                self._backend = backend
            return self._encode

    @capability("text", risk="read")
    async def text(self, texts: list) -> dict:
        """Embed up to 64 texts: {model, vectors (unit length), dim}.

        Same shape as the knowledge embedding service; used by the hub through
        cap://any/embed.text."""
        if isinstance(texts, str):
            texts = [texts]
        if not isinstance(texts, list) or not texts or len(texts) > MAX_TEXTS:
            raise ValueError(f"texts must be a list of 1-{MAX_TEXTS} strings")
        clean = [str(t)[:MAX_CHARS] for t in texts]
        encode = await asyncio.to_thread(self._load)
        vectors = await asyncio.to_thread(encode, clean)
        out = []
        for v in vectors:
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            out.append([round(x / norm, 6) for x in v])
        model = self.settings.get("model", DEFAULT_MODEL) or DEFAULT_MODEL
        return {"model": model, "vectors": out, "dim": len(out[0]) if out else 0}


PLUGIN = EmbedPlugin
