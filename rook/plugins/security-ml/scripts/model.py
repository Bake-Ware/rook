"""The classifier's maths, pure Python (castling-security: CPU only, a
Raspberry Pi is enough): call features, isolation forests for the per-actor
and per-worker baselines, and gradient-boosted trees for the verdict.

A *call* is a gate input (spec 10.2) plus ``ts``; the journal's call
entries are turned into the same shape for training. A *model* is plain
JSON (the shipped one is ``model/baseline.json``)::

    {"v": 1, "features": [...], "flag_at": 0.9, "doubt_at": 0.5, "odd_at": 0.65,
     "baseline": {...}, "forests": {"*": forest, "actor:<id>": forest, "worker:<name>": forest},
     "gbdt": {"base": logit, "lr": rate, "trees": [tree, ...]}, "trained": {...}}

The boosted trees see what a call *is* (CONTENT: risk, scope, effects,
caller, taint, command tokens, persistence markers); the forests see that
plus how unusual it is for who and where (BEHAVIOUR: time of day, novelty).
Novelty and time can't be learned from synthetic attacks, so they reach the
verdict through the forests: a call the trees doubt is flagged when it is
also unusual for its actor or machine (:func:`score`).

Trees are flat node lists: ``[feature, threshold, left, right]`` for a split
(go left when ``x[feature] < threshold``), ``[-1, value]`` for a leaf (the
leaf's size in a forest, its score in the boosted trees).
"""
from __future__ import annotations

import bisect
import math
import random
import re
import time
from collections import Counter

RISKS = ("read", "write", "exec", "admin")
SCOPES = ("self", "local", "multi-machine", "multi-user", "external")
TOKENS = {
    "exfil": r"\bcurl\b|\bwget\b|\bnc\b|\bncat\b|netcat|\bscp\b|\brsync\b|\bftp\b|/dev/tcp|\bsocat\b"
             r"|invoke-webrequest|\biwr\b|\bupload\b|pastebin|transfer\.sh|ngrok|\bsftp\b|\|\s*ssh\b",
    "priv": r"\bsudo\b|\bsu\b|chmod\s+[0-7]*[67][0-7]{2}|chmod\s+\+s|\bchown\b|setuid|\bpkexec\b|runas"
            r"|nopasswd|/etc/sudoers",
    "destroy": r"rm\s+-[a-z]*r[a-z]*f|rm\s+-[a-z]*f[a-z]*r|\bmkfs|\bdd\s+if=|\bshred\b|wipefs|format\s+[a-z]:"
               r"|del\s+/[sq]|remove-item.+-recurse|--no-preserve-root",
    "recon": r"\bwhoami\b|\bid\b|\buname\b|/etc/passwd|\bprintenv\b|\bnetstat\b|\bss\s+-|\bifconfig\b|\bnmap\b"
             r"|\bps\s+aux|\benv\b|systeminfo|net\s+user",
    "cred": r"id_rsa|id_ed25519|\.ssh/|\.aws/|/etc/shadow|\.gnupg|\.netrc|\.git-credentials|keychain"
            r"|credentials|\.kube/config|\.docker/config|token|passw",
    "obfusc": r"base64\s+-d|base64\s+--decode|\|\s*(ba)?sh\b|\|\s*bash\b|\beval\b|python3?\s+-c|perl\s+-e"
              r"|-enc(odedcommand)?\b|frombase64string|\bxxd\b|\\x[0-9a-f]{2}|iex\s*\(",
}
PERSIST = (r"authorized_keys|crontab(?!\s+-l\b)|/etc/cron|systemctl\s+(--user\s+)?enable|\.service\b|useradd|adduser|usermod"
           r"|net\s+user\s+\S+\s+/add|schtasks|currentversion\\run|\\run\b|launchctl\s+load|launchagents"
           r"|/etc/rc\.local|\.bashrc|\.profile\b|\.zshrc|/etc/sudoers|systemd/system|at\s+now")
_TOK = {k: re.compile(v, re.I) for k, v in TOKENS.items()}
_PERSIST = re.compile(PERSIST, re.I)
_URL = re.compile(r"https?://", re.I)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{1,40}")

# What the call is (the boosted trees judge these) ...
CONTENT = ("risk", "scope", "persists", "sensitive", "destructive", "physical", "taints", "outward",
           "agent", "person_only", "system", "tainted", "has_task", "urls", "ips",
           *(f"tok_{k}" for k in TOKENS), "persist_marks")
# ... and how unusual it is for who and where (the isolation forests add these).
BEHAVIOUR = ("hour_sin", "hour_cos", "novel_cap", "novel_worker", "novel_cap_worker", "novel_tokens",
             "rare_hour")
FEATURES = CONTENT + BEHAVIOUR
MAX_VOCAB = 2000                          # command tokens kept per actor


# -- features ------------------------------------------------------------------------

def command_text(args, limit: int = 4096) -> str:
    """The text a call's args carry: strings, joined, bounded."""
    out, n = [], 0
    stack = [args]
    while stack and n < limit:
        v = stack.pop()
        if isinstance(v, str):
            out.append(v)
            n += len(v)
        elif isinstance(v, dict):
            stack.extend(reversed(list(v.values())))
        elif isinstance(v, (list, tuple)):
            stack.extend(reversed(v))
    return " ".join(out)[:limit]


def actor(call: dict) -> str:
    c = call.get("caller") or {}
    return c.get("agent") or c.get("person") or "?"


def tokens(text: str) -> set:
    return {w.lower() for w in _WORD.findall(text)}


def new_baseline() -> dict:
    return {"actor_caps": {}, "actor_workers": {}, "worker_caps": {}, "actor_tokens": {}, "actor_hours": {}}


def observe(base: dict, call: dict) -> None:
    """Add a clean call to the baseline."""
    a, cap, w = actor(call), call.get("cap") or "", call.get("worker") or ""
    base["actor_caps"].setdefault(a, {})[cap] = 1
    base["actor_workers"].setdefault(a, {})[w] = 1
    base["worker_caps"].setdefault(w, {})[cap] = 1
    vocab = base["actor_tokens"].setdefault(a, {})
    for t in tokens(command_text(call.get("args"))):
        vocab[t] = vocab.get(t, 0) + 1
    if len(vocab) > MAX_VOCAB * 2:          # keep the most used
        base["actor_tokens"][a] = dict(Counter(vocab).most_common(MAX_VOCAB))
    hours = base["actor_hours"].setdefault(a, [0] * 24)
    hours[_hour(call)] += 1


def _hour(call: dict) -> int:
    return time.localtime(call.get("ts") or time.time()).tm_hour


def features(call: dict, base: dict) -> list:
    risk, scope = call.get("risk"), call.get("scope")
    effects = set(call.get("effects") or ())
    caller, session = call.get("caller") or {}, call.get("session") or {}
    text = command_text(call.get("args"))
    a, cap, w = actor(call), call.get("cap") or "", call.get("worker") or ""
    hour = _hour(call)
    seen = base["actor_tokens"].get(a)
    toks = tokens(text)
    hours = base["actor_hours"].get(a)
    known = bool(base["actor_caps"].get(a))
    return [
        RISKS.index(risk) if risk in RISKS else 3,
        SCOPES.index(scope) if scope in SCOPES else 4,
        *(float(e in effects) for e in ("persists", "sensitive", "destructive", "physical", "taints")),
        float(bool(effects & {"sends_as", "spends"})),
        float(bool(caller.get("agent")) and not str(caller.get("agent")).startswith("system:")),
        float(bool(caller.get("person")) and not caller.get("agent")),
        float(str(caller.get("agent") or "").startswith("system:")),
        float(bool(session.get("tainted"))),
        float(bool(session.get("task"))),
        min(len(_URL.findall(text)), 5),
        min(len(_IP.findall(text)), 5),
        *(min(len(rx.findall(text)), 5) for rx in _TOK.values()),
        min(len(_PERSIST.findall(text)), 5),
        math.sin(hour * math.pi / 12), math.cos(hour * math.pi / 12),
        float(known and cap not in base["actor_caps"][a]),
        float(known and w not in (base["actor_workers"].get(a) or {})),
        float(w in base["worker_caps"] and cap not in base["worker_caps"][w]),
        (sum(t not in seen for t in toks) / len(toks)) if (seen and toks) else 0.0,
        (1.0 - hours[hour] / max(hours)) if hours and sum(hours) >= 20 else 0.0,
    ]


# -- isolation forest -----------------------------------------------------------------

def _c(n: int) -> float:
    """Average path length of an unsuccessful search in a BST of n items."""
    if n <= 1:
        return 0.0
    return 2.0 * (math.log(n - 1) + 0.5772156649) - 2.0 * (n - 1) / n


def _itree(rows: list, depth: int, limit: int, rng: random.Random, nodes: list) -> int:
    i = len(nodes)
    nodes.append(None)
    if depth >= limit or len(rows) <= 1:
        nodes[i] = [-1, len(rows)]
        return i
    spread = [f for f in range(len(rows[0])) if min(r[f] for r in rows) < max(r[f] for r in rows)]
    if not spread:
        nodes[i] = [-1, len(rows)]
        return i
    f = rng.choice(spread)
    lo, hi = min(r[f] for r in rows), max(r[f] for r in rows)
    t = rng.uniform(lo, hi)
    left = [r for r in rows if r[f] < t]
    right = [r for r in rows if r[f] >= t]
    nodes[i] = [f, round(t, 6), _itree(left, depth + 1, limit, rng, nodes),
                _itree(right, depth + 1, limit, rng, nodes)]
    return i


def forest(rows: list, rng: random.Random, trees: int = 20, sample: int = 64) -> dict:
    n = min(sample, len(rows))
    limit = max(1, math.ceil(math.log2(max(n, 2))))
    out = []
    for _ in range(trees):
        nodes: list = []
        _itree(rng.sample(rows, n), 0, limit, rng, nodes)
        out.append(nodes)
    return {"n": n, "trees": out}


def anomaly(f: dict, x: list) -> float:
    """0..1; above ~0.6 is unusual for this baseline."""
    total = 0.0
    for nodes in f["trees"]:
        i, depth = 0, 0
        node = nodes[0]
        while node[0] >= 0:
            i = node[2] if x[node[0]] < node[1] else node[3]
            node = nodes[i]
            depth += 1
        total += depth + _c(node[1])
    return 2.0 ** (-(total / len(f["trees"])) / max(_c(f["n"]), 1e-9))


# -- gradient-boosted trees -------------------------------------------------------------

def _thresholds(rows: list, f: int, bins: int = 24) -> list:
    vals = sorted({r[f] for r in rows})
    if len(vals) <= bins:
        return [(a + b) / 2 for a, b in zip(vals, vals[1:])]
    step = len(vals) / bins
    return sorted({vals[int(i * step)] for i in range(1, bins)})


def _grow(idx, bins, g, h, thr, depth, limit, min_leaf, lam, nodes) -> int:
    """One boosted tree over binned rows: ``bins[j][f]`` is how many of
    ``thr[f]`` row j's value is at or above, so "x < thr[f][k]" is bin <= k."""
    i = len(nodes)
    nodes.append(None)
    G = sum(g[j] for j in idx)
    H = sum(h[j] for j in idx)
    nodes[i] = [-1, round(-G / (H + lam), 6)]
    if depth >= limit or len(idx) < 2 * min_leaf:
        return i
    best, parent = (1e-9, None, None), G * G / (H + lam)
    for f, ts in enumerate(thr):
        if not ts:
            continue
        hg, hh, hn = [0.0] * (len(ts) + 1), [0.0] * (len(ts) + 1), [0] * (len(ts) + 1)
        for j in idx:
            b = bins[j][f]
            hg[b] += g[j]
            hh[b] += h[j]
            hn[b] += 1
        gl = hl = 0.0
        nl = 0
        for k in range(len(ts)):
            gl, hl, nl = gl + hg[k], hl + hh[k], nl + hn[k]
            if nl < min_leaf or len(idx) - nl < min_leaf:
                continue
            gain = gl * gl / (hl + lam) + (G - gl) ** 2 / (H - hl + lam) - parent
            if gain > best[0]:
                best = (gain, f, k)
    if best[1] is None:
        return i
    _, f, k = best
    left = [j for j in idx if bins[j][f] <= k]
    right = [j for j in idx if bins[j][f] > k]
    nodes[i] = [f, thr[f][k], _grow(left, bins, g, h, thr, depth + 1, limit, min_leaf, lam, nodes),
                _grow(right, bins, g, h, thr, depth + 1, limit, min_leaf, lam, nodes)]
    return i


def boost(rows: list, labels: list, *, trees: int = 100, depth: int = 3, lr: float = 0.2,
          min_leaf: int = 2, lam: float = 1.0) -> dict:
    """Logistic-loss boosted trees; positives weighted to balance the classes
    (the weight scales each row's gradient and hessian)."""
    pos = sum(labels) or 1
    neg = (len(labels) - sum(labels)) or 1
    w = [len(labels) / (2 * pos) if y else len(labels) / (2 * neg) for y in labels]
    p0 = min(max(sum(labels) / len(labels), 1e-3), 1 - 1e-3)
    base = math.log(p0 / (1 - p0))
    f = [base] * len(rows)
    thr = [_thresholds(rows, k) for k in range(len(rows[0]))]
    bins = [[bisect.bisect_right(thr[k], r[k]) for k in range(len(r))] for r in rows]
    out = []
    idx = list(range(len(rows)))
    for _ in range(trees):
        p = [1 / (1 + math.exp(-max(min(v, 50), -50))) for v in f]
        g = [(pi - y) * wi for pi, y, wi in zip(p, labels, w)]
        h = [max(pi * (1 - pi), 1e-6) * wi for pi, wi in zip(p, w)]
        nodes: list = []
        _grow(idx, bins, g, h, thr, 0, depth, min_leaf, lam, nodes)
        out.append(nodes)
        for j in idx:
            f[j] += lr * _leaf(nodes, rows[j])
    return {"base": base, "lr": lr, "trees": out}


def _leaf(nodes: list, x: list) -> float:
    node = nodes[0]
    while node[0] >= 0:
        node = nodes[node[2] if x[node[0]] < node[1] else node[3]]
    return node[1]


def predict(gb: dict, x: list) -> float:
    v = gb["base"] + gb["lr"] * sum(_leaf(t, x) for t in gb["trees"])
    return 1 / (1 + math.exp(-max(min(v, 50), -50)))


# -- the model ----------------------------------------------------------------------------

def unusual(model: dict, call: dict, x: list) -> float:
    """How unusual the call is for its actor and its worker (the higher of
    the two forests' scores; the shared forest stands in for a newcomer)."""
    forests = model["forests"]
    glob = forests["*"]
    return max(anomaly(forests.get(f"actor:{actor(call)}", glob), x),
               anomaly(forests.get(f"worker:{call.get('worker') or ''}", glob), x))


def score(model: dict, call: dict) -> dict:
    """{p, unusual, verdict, confidence, reason}. Flag when the trees are sure
    (``p >= flag_at``), or when they are doubtful (``p >= doubt_at``) and the
    call is also unusual for this actor or machine (``unusual >= odd_at``).
    The reason is the strongest sign in the call."""
    x = features(call, model["baseline"])
    p = predict(model["gbdt"], x[:len(CONTENT)])
    odd = unusual(model, call, x)
    named = dict(zip(FEATURES, x))
    flag = p >= model["flag_at"] or (p >= model["doubt_at"] and odd >= model["odd_at"])
    if not flag:
        return {"p": p, "unusual": odd, "verdict": "allow", "confidence": round(1 - p, 4), "reason": "other"}
    if named["persist_marks"] or named["persists"]:
        reason = "persistence"
    elif named["tok_exfil"] or named["ips"]:
        reason = "exfiltration"
    else:
        reason = "anomaly"
    return {"p": p, "unusual": odd, "verdict": "flag", "confidence": round(max(p, odd), 4), "reason": reason}


def train(clean: list, attacks: list, *, seed: int = 7, keys: int = 16, min_rows: int = 32,
          flag_at: float = 0.9, doubt_at: float = 0.5, odd_at: float = 0.65) -> dict:
    """A model from clean calls (oldest first) and attack calls. The forests
    learn the clean calls' behaviour, novelty taken as it was when each
    happened (so new-but-ordinary is normal); the trees learn clean against
    attack from what the calls are."""
    rng = random.Random(seed)
    base = new_baseline()
    xs = []
    for call in clean:
        xs.append(features(call, base))
        observe(base, call)
    forests = {"*": forest(xs, rng)}
    by_actor, by_worker = {}, {}
    for call, x in zip(clean, xs):
        by_actor.setdefault(actor(call), []).append(x)
        by_worker.setdefault(call.get("worker") or "", []).append(x)
    for prefix, groups in (("actor", by_actor), ("worker", by_worker)):
        top = sorted(groups.items(), key=lambda kv: -len(kv[1]))[:keys]
        for k, rows in top:
            if len(rows) >= min_rows:
                forests[f"{prefix}:{k}"] = forest(rows, rng)
    n = len(CONTENT)
    rows = [x[:n] for x in xs] + [features(c, base)[:n] for c in attacks]
    gbdt = boost(rows, [0] * len(xs) + [1] * len(attacks))
    return {"v": 1, "features": list(FEATURES), "flag_at": flag_at, "doubt_at": doubt_at, "odd_at": odd_at,
            "baseline": base, "forests": forests, "gbdt": gbdt}


def evaluate(model: dict, locked: list, held_out: list) -> dict:
    """The promotion checks: every locked attack flagged; how many clean calls
    held out from training would have been flagged."""
    missed = [i for i, c in enumerate(locked) if score(model, c)["verdict"] != "flag"]
    fp = sum(score(model, c)["verdict"] == "flag" for c in held_out)
    return {"locked": len(locked), "missed": missed, "held_out": len(held_out),
            "false_positives": fp, "fp_rate": fp / len(held_out) if held_out else 0.0}
