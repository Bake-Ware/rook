"""Semantic search's half that runs here: a small sentence-embedding model, run on this machine's CPU.

The model is a plain BERT encoder (BAAI/bge-small-en-v1.5 by default: 33 million parameters, 384
numbers per text). Its weights are fetched once from Hugging Face into the store's `models` folder;
nothing of Bake's is sent anywhere to do that, and after that it runs offline. The forward pass is
written here in numpy (the only thing this needs beyond the standard library), so there is nothing
else to install. Any BERT-shaped model in safetensors works: change `[search]` in knobs.toml
(all-MiniLM-L6-v2 with pooling "mean" is the usual smaller one).

`fake` is a stand-in for the tests: it hashes words into a vector, so the mechanics can be tested
without the model.
"""

import hashlib
import json
import math
import pathlib
import re
import struct
import unicodedata
import urllib.request

FILES = ("config.json", "vocab.txt", "model.safetensors")


def folder(store_folder, name):
    return pathlib.Path(store_folder) / "models" / name.split("/")[-1]


def fetch(store_folder, name, log=print):
    """Download the model's files once. Only public model files come in; nothing goes out."""
    where = folder(store_folder, name)
    where.mkdir(parents=True, exist_ok=True)
    for f in FILES:
        if not (where / f).exists():
            log(f"fetching {name}/{f} from huggingface.co into {where}")
            tmp = where / (f + ".part")
            urllib.request.urlretrieve(f"https://huggingface.co/{name}/resolve/main/{f}", tmp)
            tmp.rename(where / f)
    return where


class Bert:
    """A BERT encoder in numpy: WordPiece tokens in, one unit-length vector per text out."""

    def __init__(self, where, pooling="cls", max_tokens=512):
        import numpy as np
        self.np = np
        where = pathlib.Path(where)
        cfg = json.loads((where / "config.json").read_text())
        self.layers, self.heads = cfg["num_hidden_layers"], cfg["num_attention_heads"]
        self.eps = cfg.get("layer_norm_eps", 1e-12)
        self.pooling, self.max_tokens = pooling, min(max_tokens, cfg.get("max_position_embeddings", 512))
        self.vocab = {w: i for i, w in enumerate((where / "vocab.txt").read_text(encoding="utf-8").splitlines())}
        self.w = _safetensors(where / "model.safetensors", np)

    # --- tokenizing (BERT's uncased basic tokenizer, then WordPiece) ---
    def tokens(self, text):
        text = "".join(" " if unicodedata.category(c).startswith("C") and c not in "\t\n\r" else c
                       for c in text if ord(c) not in (0, 0xFFFD))
        text = unicodedata.normalize("NFD", text.lower())
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        ids = [self.vocab["[CLS]"]]
        for word in re.findall(r"[^\s\W_]+|[^\s]", text, flags=re.UNICODE):
            ids += self._wordpiece(word)
        ids = ids[:self.max_tokens - 1]
        return ids + [self.vocab["[SEP]"]]

    def _wordpiece(self, word):
        if len(word) > 100:
            return [self.vocab["[UNK]"]]
        out, start = [], 0
        while start < len(word):
            end, piece = len(word), None
            while start < end:
                sub = word[start:end] if start == 0 else "##" + word[start:end]
                if sub in self.vocab:
                    piece = self.vocab[sub]
                    break
                end -= 1
            if piece is None:
                return [self.vocab["[UNK]"]]
            out.append(piece)
            start = end
        return out

    # --- the encoder ---
    def __call__(self, texts):
        np, w = self.np, self.w
        if not texts:
            return np.zeros((0, w["embeddings.word_embeddings.weight"].shape[1]), dtype=np.float32)
        order = sorted(range(len(texts)), key=lambda i: len(texts[i]))
        result = [None] * len(texts)
        for start in range(0, len(order), 32):
            batch = order[start:start + 32]
            ids = [self.tokens(texts[i]) for i in batch]
            for i, v in zip(batch, self._encode(ids)):
                result[i] = v
        return np.stack(result)

    def _encode(self, ids):
        np, w = self.np, self.w
        n, length = len(ids), max(len(i) for i in ids)
        tok = np.zeros((n, length), dtype=np.int64)
        mask = np.zeros((n, length), dtype=np.float32)
        for r, row in enumerate(ids):
            tok[r, :len(row)] = row
            mask[r, :len(row)] = 1
        x = (w["embeddings.word_embeddings.weight"][tok] + w["embeddings.position_embeddings.weight"][:length]
             + w["embeddings.token_type_embeddings.weight"][0])
        x = self._norm(x, "embeddings.LayerNorm")
        bias = (1.0 - mask)[:, None, None, :] * -1e9
        d = x.shape[-1] // self.heads
        for k in range(self.layers):
            p = f"encoder.layer.{k}."
            q, kk, v = (self._lin(x, p + f"attention.self.{m}").reshape(n, length, self.heads, d).transpose(0, 2, 1, 3)
                        for m in ("query", "key", "value"))
            att = q @ kk.transpose(0, 1, 3, 2) / math.sqrt(d) + bias
            att = np.exp(att - att.max(-1, keepdims=True))
            att /= att.sum(-1, keepdims=True)
            ctx = (att @ v).transpose(0, 2, 1, 3).reshape(n, length, -1)
            x = self._norm(x + self._lin(ctx, p + "attention.output.dense"), p + "attention.output.LayerNorm")
            h = _gelu(self._lin(x, p + "intermediate.dense"), np)
            x = self._norm(x + self._lin(h, p + "output.dense"), p + "output.LayerNorm")
        if self.pooling == "mean":
            v = (x * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
        else:
            v = x[:, 0]
        return v / np.linalg.norm(v, axis=-1, keepdims=True)

    def _lin(self, x, name):
        return x @ self.w[name + ".weight"].T + self.w[name + ".bias"]

    def _norm(self, x, name):
        np = self.np
        mu = x.mean(-1, keepdims=True)
        var = ((x - mu) ** 2).mean(-1, keepdims=True)
        return (x - mu) / np.sqrt(var + self.eps) * self.w[name + ".weight"] + self.w[name + ".bias"]


def _gelu(x, np):
    # BERT's exact GELU, x * Phi(x), with erf from Abramowitz and Stegun 7.1.26 (error under 1.5e-7).
    z = x / math.sqrt(2.0)
    s, a = np.sign(z), np.abs(z)
    t = 1.0 / (1.0 + 0.3275911 * a)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t + 0.254829592) * t * np.exp(-a * a)
    return 0.5 * x * (1.0 + s * y)


def _safetensors(path, np):
    raw = pathlib.Path(path).read_bytes()
    (size,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8:8 + size])
    kinds = {"F32": np.float32, "F16": np.float16, "F64": np.float64, "I64": np.int64, "I32": np.int32}
    out = {}
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        a, b = meta["data_offsets"]
        buf = raw[8 + size + a:8 + size + b]
        if meta["dtype"] == "BF16":
            arr = (np.frombuffer(buf, dtype=np.uint16).astype(np.uint32) << 16).view(np.float32)
        else:
            arr = np.frombuffer(buf, dtype=kinds[meta["dtype"]]).astype(np.float32)
        for prefix in ("bert.", "model."):
            if name.startswith(prefix):
                name = name[len(prefix):]
        out[name] = arr.reshape(meta["shape"])
    return out


class Fake:
    """For the tests: each word hashed into one of 256 slots, so texts sharing words are close."""
    name = "fake"

    def __call__(self, texts):
        import numpy as np
        out = np.zeros((len(texts), 256), dtype=np.float32)
        for r, t in enumerate(texts):
            for word in re.findall(r"[a-z0-9]+", t.lower()):
                out[r, int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1
        norm = np.linalg.norm(out, axis=-1, keepdims=True)
        norm[norm == 0] = 1
        return out / norm


_loaded = {}


def embedder(store_folder, knobs, log=print):
    """The embedder the knobs name, loaded once per process. "fake" gives the test stand-in."""
    s = knobs["search"]
    if s["model"] == "fake":
        return Fake()
    key = (str(store_folder), s["model"], s["pooling"])
    if key not in _loaded:
        model = Bert(fetch(store_folder, s["model"], log), pooling=s["pooling"])
        model.name = s["model"]
        _loaded[key] = model
    return _loaded[key]
