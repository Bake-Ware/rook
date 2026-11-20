"""Semantic search's half that runs here: sentence embeddings, made on this machine's CPU in numpy.

Two kinds of model, chosen by the search knob (see [models] in knobs.toml):
- "bert": a BERT encoder (BAAI/bge-small-en-v1.5 by default: 33 million parameters, 384 numbers per
  text; bge-base: 110 million, 768). The forward pass is written here in numpy.
- "static": a table of one vector per token, averaged per text and nothing else at search time
  (model2vec: minishlab/blink-8M, potion-base-8M: 30,000 tokens by 256 numbers, about 31 MB).

Their files are fetched once from Hugging Face into the models folder (a knob); nothing of Bake's is
sent anywhere to do that, and after that they run offline. numpy is the only thing needed beyond the
standard library. The weights are mapped from the file, not copied.

`fake` is a stand-in for the tests: it hashes words into a vector, so the mechanics can be tested
without a model.
"""

import hashlib
import json
import math
import pathlib
import re
import struct
import unicodedata
import urllib.request

FILES = {"bert": ("config.json", "vocab.txt", "model.safetensors"),
         "static": ("config.json", "tokenizer.json", "model.safetensors")}


def folder(models_folder, repo):
    return pathlib.Path(models_folder).expanduser() / repo.split("/")[-1]


def fetch(models_folder, repo, kind="bert", log=print):
    """Download the model's files once. Only public model files come in; nothing goes out."""
    where = folder(models_folder, repo)
    where.mkdir(parents=True, exist_ok=True)
    for f in FILES[kind]:
        if not (where / f).exists():
            log(f"fetching {repo}/{f} from huggingface.co into {where}")
            tmp = where / (f + ".part")
            urllib.request.urlretrieve(f"https://huggingface.co/{repo}/resolve/main/{f}", tmp)
            tmp.rename(where / f)
    return where


class WordPiece:
    """BERT's uncased basic tokenizer, then WordPiece: text in, token ids out (no special tokens)."""

    def __init__(self, vocab, unk="[UNK]"):
        self.vocab, self.unk = vocab, vocab.get(unk)

    def ids(self, text):
        text = "".join(" " if unicodedata.category(c).startswith("C") and c not in "\t\n\r" else c
                       for c in text if ord(c) not in (0, 0xFFFD))
        text = unicodedata.normalize("NFD", text.lower())
        text = "".join(c for c in text if unicodedata.category(c) != "Mn")
        out = []
        for word in re.findall(r"[^\s\W_]+|[^\s]", text, flags=re.UNICODE):
            out += self._wordpiece(word)
        return out

    def _wordpiece(self, word):
        if len(word) > 100:
            return [self.unk]
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
                return [self.unk]
            out.append(piece)
            start = end
        return out


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
        self.wp = WordPiece(self.vocab)
        self.w = _safetensors(where / "model.safetensors", np)

    def tokens(self, text):
        ids = [self.vocab["[CLS]"]] + self.wp.ids(text)
        return ids[:self.max_tokens - 1] + [self.vocab["[SEP]"]]

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
    """The tensors, mapped from the file rather than read and copied, so the model's weights are paged in
    from disk once (half the memory of reading the file and converting it)."""
    import mmap
    with open(path, "rb") as f:
        mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
    (size,) = struct.unpack("<Q", mm[:8])
    header = json.loads(mm[8:8 + size])
    kinds = {"F32": np.float32, "F16": np.float16, "F64": np.float64, "I64": np.int64, "I32": np.int32}
    out = {}
    for name, meta in header.items():
        if name == "__metadata__":
            continue
        a, b = meta["data_offsets"]
        if meta["dtype"] == "BF16":
            raw = np.frombuffer(mm, dtype=np.uint16, count=(b - a) // 2, offset=8 + size + a)
            arr = (raw.astype(np.uint32) << 16).view(np.float32)
        else:
            dtype = kinds[meta["dtype"]]
            arr = np.frombuffer(mm, dtype=dtype, count=(b - a) // np.dtype(dtype).itemsize, offset=8 + size + a)
            if dtype != np.float32 and dtype not in (np.int64, np.int32):
                arr = arr.astype(np.float32)
        for prefix in ("bert.", "model."):
            if name.startswith(prefix):
                name = name[len(prefix):]
        out[name] = arr.reshape(meta["shape"])
    return out


class Static:
    """A static embedding table (model2vec): the token vectors of a text averaged, then made unit length.
    Unknown tokens are left out, as model2vec does. No transformer runs at search time."""

    def __init__(self, where):
        import numpy as np
        self.np = np
        where = pathlib.Path(where)
        tok = json.loads((where / "tokenizer.json").read_text(encoding="utf-8"))
        vocab = tok["model"]["vocab"]
        vocab = vocab if isinstance(vocab, dict) else {w: i for i, (w, *_rest) in enumerate(vocab)}
        self.wp = WordPiece(vocab, tok["model"].get("unk_token", "[UNK]"))
        cfg = json.loads((where / "config.json").read_text())
        self.normalize = cfg.get("normalize", True)
        w = _safetensors(where / "model.safetensors", np)
        self.table = w["embeddings"]
        self.weights = w.get("weights")

    def __call__(self, texts):
        np = self.np
        out = np.zeros((len(texts), self.table.shape[1]), dtype=np.float32)
        for r, t in enumerate(texts):
            ids = [i for i in self.wp.ids(t) if i != self.wp.unk]
            if not ids:
                continue
            rows = self.table[ids]
            if self.weights is not None:
                rows = rows * self.weights[ids][:, None]
            v = rows.mean(0)
            if self.normalize:
                n = np.linalg.norm(v)
                v = v / n if n else v
            out[r] = v
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


def embedder(store_folder, knobs, log=print, repo=None):
    """The embedder the knobs name (or `repo`), loaded once per process. None for keyword search.
    "fake" gives the test stand-in."""
    from .knobs import search
    s = search(knobs)
    kind = s.get("kind", "bert")
    if kind == "keyword":
        return None
    if kind == "hybrid":
        repo, kind = s["static"], "static"
    repo = repo or s["repo"]
    if repo == "fake":
        return Fake()
    key = (repo, s.get("pooling"))
    if key not in _loaded:
        where = fetch(s["models_folder"], repo, kind, log)
        model = Static(where) if kind == "static" else Bert(where, pooling=s["pooling"])
        model.name = repo
        _loaded[key] = model
    return _loaded[key]
