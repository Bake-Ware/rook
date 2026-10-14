"""Write rules for home agent memory, as code (docs/design/memory.md 4).

Every candidate passes :func:`screen` before it is stored. The rules are
deterministic (regular expressions and counts) so behaviour is reproducible
without a model:

* **Secrets are never stored.** Known vault values are first turned into
  ``{{secret:<name>}}`` references by the same reverse masking the rest of
  Rook uses (:func:`rook.band_mcp.secret_mask.scrub`): a reference is safe to
  keep. Then the deny patterns (key and token shapes, "password is ...",
  card numbers, plus the operator's own list) are checked: a deliberate write
  that matches is refused with a reason; an automatic one (an episode
  summary) has the match replaced by ``[redacted]``.
* **Nothing that would steer a prompt.** Memory is read back into the home
  agent's prompt, so prompt-injection and exfiltration phrases and invisible
  Unicode are refused (redacted in automatic writes).
* **No code, diffs or logs** outside episodes: save the lesson in a sentence.
* **Transient state** ("right now", TODO lists, temp paths) lowers the
  confidence; corrections and explicit rules ("always", "never", "from now
  on") raise it.

:func:`extract` finds what a person said that is worth remembering (explicit
"remember ..." requests, "my X is Y", preferences, corrections) and turns it
into third-person statements about them.

Ideas and lists ported from Bake-Ware/rook PR #31 (rules.py, ingest.py).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

KINDS = ("profile", "fact", "episode", "procedure")
#: Plain-language names the dashboard and the prompt use.
KIND_LABELS = {"profile": "About people", "fact": "Facts", "episode": "Conversations",
               "procedure": "Skills"}
SHARED = "shared"
#: Where a kind lives when the writer names no scope: about a person, or shared.
PERSONAL_KINDS = ("profile", "episode")
REDACTED = "[redacted]"

# -- prompt injection and exfiltration ------------------------------------------
_THREATS = [
    (r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|messages)", "prompt injection"),
    (r"disregard\s+(your|all|any|the)\s+(instructions|rules|guidelines|system prompt)", "prompt injection"),
    (r"\byou\s+are\s+now\s+(a|an|the|in)\b", "role hijack"),
    (r"system\s+prompt\s+override", "prompt injection"),
    (r"\bnew\s+(system\s+)?instructions\s*:", "prompt injection"),
    (r"do\s+not\s+(tell|inform|show)\s+the\s+(user|operator|human)", "deception"),
    (r"\b(curl|wget)\b[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|PASS)\w*", "exfiltration"),
    (r"\bcat\s+[^\n]*(\.env\b|\.netrc|\.pgpass|id_rsa|id_ed25519|credentials)", "exfiltration"),
    (r"authorized_keys", "persistence"),
]
_THREAT_RES = [(re.compile(p, re.I), name) for p, name in _THREATS]
_INVISIBLE = re.compile("[​‌‍⁠﻿‪-‮⁦-⁩]")

# -- secrets: the built-in deny list (operators add their own) ---------------------
DENY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", "private key"),
    (r"\b(?:sk|rk|pk)-(?:ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}", "API key"),
    (r"\bgh[pousr]_[A-Za-z0-9]{30,}", "GitHub token"),
    (r"\bgithub_pat_[A-Za-z0-9_]{40,}", "GitHub token"),
    (r"\bxox[abprs]-[A-Za-z0-9-]{10,}", "Slack token"),
    (r"\bAKIA[0-9A-Z]{16}\b", "AWS key"),
    (r"\bAIza[0-9A-Za-z_-]{35}\b", "Google API key"),
    (r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}", "JSON web token"),
    (r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|token|psk|pin)\b\s*[:=]\s*[\"']?"
     r"(?!\{\{secret:)[^\s\"',;]{4,}", "credential"),
    (r"(?i)\b(?:password|passcode|passphrase|pin(?: code| number)?|wi-?fi key)\s+(?:is|was|=)\s+"
     r"[\"']?(?!\{\{secret:)\S{3,}", "credential"),
    (r"(?i)\b(?:door|garage|gate|alarm|lock|safe|access|security|entry|unlock)\s+code\s+"
     r"(?:is|was|=|:)\s*[\"']?(?!\{\{secret:)\S{3,}", "access code"),
    (r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*", "bearer token"),
    (r"\b(?:\d[ -]?){13,19}\b", "card number"),
    (r"\b\d{3}-\d{2}-\d{4}\b", "national ID number"),
)

_TRANSIENT = [
    (r"\b(right now|currently|at the moment|for now|just now|in progress|this session|tonight)\b", "transient"),
    (r"\b(todo|wip|next step|next steps)\b\s*[:\-]", "task state"),
    (r"(^|\s)/tmp/\S+", "temp path"),
    (r"\bpid\s*[:=]?\s*\d{2,}\b", "process id"),
]
_CORRECTION = re.compile(
    r"(?i)(^|\b)(don't|do not|never|always|stop|instead|prefer|from now on|remember|"
    r"correct(ed|ion)|not like that|that's wrong|actually)\b")
_CODE_LINE = re.compile(r"^\s*([+-]\s|@@|def |class |import |from \S+ import|\$ |>>> |[{}();]\s*$|"
                        r"(if|for|while|return|const|let|var|function)\b.*[;:{]\s*$)")


@dataclass
class Verdict:
    text: str
    confidence: float
    reject: str | None = None          # why the write is refused
    warnings: list = field(default_factory=list)
    redacted: int = 0                   # deny-pattern matches replaced (automatic writes)
    references: int = 0                 # vault values turned into {{secret:}} references
    signals: list = field(default_factory=list)

    def to_dict(self) -> dict:
        out: dict = {"confidence": round(self.confidence, 3)}
        if self.reject:
            out["rejected"] = self.reject
        for k in ("warnings", "signals"):
            if getattr(self, k):
                out[k] = list(getattr(self, k))
        for k in ("redacted", "references"):
            if getattr(self, k):
                out[k] = getattr(self, k)
        return out


def normalize(text: str) -> str:
    """Whitespace-collapsed NFC text: the stored form."""
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


def content_hash(text: str) -> str:
    key = re.sub(r"[^\w{}:]+", " ", normalize(text).lower()).strip()
    return hashlib.sha256(key.encode()).hexdigest()[:32]


STOPWORDS = frozenset("the and for that this with from are was were has have had you your user "
                      "users they them their its not but can will would should could about into "
                      "than then there here what when where which who how all any does did "
                      "is an of to in on at do my me we our it be am".split())
_STOP = STOPWORDS


def tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2 and t not in _STOP}


def jaccard(a: str, b: str) -> float:
    x, y = tokens(a), tokens(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def compile_deny(extra=()) -> list[tuple[re.Pattern, str]]:
    """The built-in deny list plus the operator's patterns (bad regexes are
    skipped, not fatal: the built-in list still applies)."""
    out = [(re.compile(p), name) for p, name in DENY_PATTERNS]
    for p in extra or ():
        try:
            out.append((re.compile(str(p)), "a pattern on your deny list"))
        except re.error:
            continue
    return out


def _vault_scrub(text: str) -> str:
    """Known vault values become ``{{secret:name}}`` references (the hub's
    installed masker; a no-op without a vault)."""
    try:
        from ....band_mcp.secret_mask import scrub
    except Exception:  # noqa: BLE001 - a hub without the MCP package still screens by pattern
        return text
    out = scrub(text)
    return out if isinstance(out, str) else text


#: A vault reference, ``{{secret:name}}`` (with optional encoding tags): safe to
#: keep, and never itself a deny-pattern match ("password: {{secret:x}}").
_STUB = re.compile(r"\{\{secret:[a-z0-9][a-z0-9._-]{0,63}(?:\|[a-z0-9]+)*\}\}")


def deny_hits(text: str, deny) -> list[str]:
    probe = _STUB.sub(" ", text)
    return [name for pat, name in deny if pat.search(probe)]


def redact(text: str, deny) -> tuple[str, int]:
    """Replace deny-pattern and injection matches outside vault references."""
    n = 0
    parts = _STUB.split(text)
    stubs = _STUB.findall(text)
    out = []
    for part in parts:
        for pat, _name in list(deny) + _THREAT_RES:
            part, k = pat.subn(REDACTED, part)
            n += k
        part, k = _INVISIBLE.subn("", part)
        out.append(part)
        n += k
    joined = out[0]
    for stub, part in zip(stubs, out[1:]):
        joined += stub + part
    return joined, n


def threat(text: str) -> str | None:
    if _INVISIBLE.search(text):
        return "invisible characters"
    for pat, name in _THREAT_RES:
        if pat.search(text):
            return name
    return None


def mostly_code(raw: str) -> bool:
    if "```" in raw:
        return True
    lines = [ln for ln in str(raw).splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    return sum(1 for ln in lines if _CODE_LINE.match(ln)) / len(lines) >= 0.5


def clip(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    cut = text[:n - 1]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut).rstrip(" ,;:") + "…"


def screen(raw: str, kind: str, *, confidence: float = 0.7, pinned: bool = False,
           automatic: bool = False, deny=None, max_chars: int = 500) -> Verdict:
    """Apply the write rules to one candidate. ``automatic`` writes (episode
    summaries, consolidation) are redacted instead of refused, so one bad
    sentence does not lose a whole conversation."""
    if kind not in KINDS:
        return Verdict("", 0.0, reject=f"kind must be one of {', '.join(KINDS)}")
    deny = deny if deny is not None else compile_deny()
    raw = _vault_scrub(str(raw or ""))
    refs = raw.count("{{secret:")
    text = normalize(raw)
    if not text:
        return Verdict("", 0.0, reject="nothing to remember")
    redacted = 0
    if automatic:
        text, redacted = redact(text, deny)
        text = normalize(text)
    else:
        hits = deny_hits(text, deny)
        if hits:
            return Verdict("", 0.0, reject=f"looks like a secret ({hits[0]}); memory never stores "
                                           "secrets. Keep it in Secrets and refer to it as "
                                           "{{secret:<name>}}.")
        bad = threat(text)
        if bad:
            return Verdict("", 0.0, reject=f"unsafe content ({bad}): memory is read back into "
                                           "the agent's prompt")
    if kind != "episode" and mostly_code(raw):
        return Verdict("", 0.0, reject="code, diffs and logs are not remembered; save the lesson "
                                       "in a sentence instead")
    warnings: list[str] = []
    signals: list[str] = []
    if len(text) > max_chars:
        text = clip(text, max_chars)
        warnings.append("shortened")
    conf = 1.0 if pinned else float(confidence)
    if not pinned and kind != "episode":
        found = sorted({name for pat, name in _TRANSIENT if re.search(pat, text, re.I)})
        if found:
            warnings += found
            conf -= 0.3
        if _CORRECTION.search(text):
            conf += 0.15
            signals.append("correction")
    if redacted:
        warnings.append("redacted")
    return Verdict(text, max(0.0, min(1.0, conf)), warnings=warnings, redacted=redacted,
                   references=refs, signals=signals)


# -- what a person said that is worth remembering ------------------------------------

_PRONOUNS = [
    (r"\bI am\b", "the user is"), (r"\bI'm\b", "the user is"), (r"\bI've\b", "the user has"),
    (r"\bI'll\b", "the user will"), (r"\bI'd\b", "the user would"), (r"\bI\b", "the user"),
    (r"\bmyself\b", "themselves"), (r"\bmine\b", "the user's"), (r"\bmy\b", "the user's"),
    (r"\bme\b", "the user"),
]
_PRONOUN_RES = [(re.compile(p, re.I), r) for p, r in _PRONOUNS]

#: (pattern, kind, slot (None or a group number or a fixed name), confidence, signal)
_EXTRACT = [
    (re.compile(r"(?i)^(?:please\s+)?remember\s+(?:that\s+)?(.{6,300})$"), None, None, 0.9, "explicit"),
    (re.compile(r"(?i)^(?:(?:hi|hey|hello)[,!. ]+)?my name is\s+([\w .'-]{2,40})$"), "profile", "name", 0.9, "explicit"),
    (re.compile(r"(?i)^(?:please\s+)?call me\s+([\w .'-]{2,40})$"), "profile", "name", 0.9, "explicit"),
    (re.compile(r"(?i)^(?:from now on|going forward|in future),?\s+(.{6,300})$"), "profile", None, 0.85, "correction"),
    (re.compile(r"(?i)^(?:please\s+)?(?:always|never)\s+(.{6,300})$"), "profile", None, 0.8, "correction"),
    (re.compile(r"(?i)^my\s+((?:[a-z'-]+\s){0,3}[a-z'-]+)\s+(?:is|are)\s+(.{2,200})$"), "profile", 1, 0.75, "statement"),
    (re.compile(r"(?i)^I\s+(live|work)\s+(in|at|for|from)\s+(.{2,200})$"), "profile",
     lambda m: f"{m.group(1).lower()}s {m.group(2).lower()}", 0.75, "statement"),
    (re.compile(r"(?i)^I\s+(?:really\s+)?(?:prefer|like|love|hate|dislike|don't like|do not like|"
                r"always|never|usually|use)\s+(.{3,200})$"), "profile", None, 0.7, "preference"),
]
_SLOT_RE = re.compile(r"(?i)^(?:the user's|my|the)\s+((?:[a-z'-]+\s){0,3}[a-z'-]+)\s+(?:is|are)\b")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


_ADVERBS = frozenset("always never usually really also often sometimes rarely mostly "
                     "generally".split())
_IRREGULAR = {"am": "is", "are": "is", "have": "has", "do": "does", "don't": "doesn't",
              "go": "goes", "was": "was", "were": "was", "can": "can", "will": "will",
              "would": "would", "should": "should", "could": "could", "must": "must",
              "might": "might", "may": "may", "is": "is", "has": "has", "does": "does",
              "doesn't": "doesn't", "can't": "can't", "won't": "won't"}


def _third(verb: str) -> str:
    low = verb.lower()
    if low in _IRREGULAR:
        return _IRREGULAR[low]
    if re.search(r"(s|sh|ch|x|z|o)$", low):
        return verb + "es"
    if re.search(r"[^aeiou]y$", low):
        return verb[:-1] + "ies"
    return verb + "s"


def _conjugate(m: re.Match) -> str:
    words = m.group(1).split(" ")
    for i, w in enumerate(words):
        if w.lower() not in _ADVERBS:
            words[i] = _third(w)
            break
    return "the user " + " ".join(words)


def third_person(text: str) -> str:
    """"My wife's birthday is May 3" -> "The user's wife's birthday is May 3";
    "I prefer tea" -> "The user prefers tea"."""
    text = re.sub(r"^I\s+((?:(?:" + "|".join(_ADVERBS) + r")\s+)*[A-Za-z']+)",
                  _conjugate, text.strip(), flags=re.I)
    for pat, rep in _PRONOUN_RES:
        text = pat.sub(rep, text)
    text = re.sub(r"\bthe user (am|are)\b", "the user is", text)
    return text[:1].upper() + text[1:]


def _slot(s: str) -> str | None:
    s = re.sub(r"\s+", " ", s.lower().replace("favorite", "favourite").replace("color", "colour")).strip()
    return s if s and s not in ("user", "users") else None


def slot_of(text: str) -> str | None:
    """The attribute a statement sets ("name", "favourite colour"), so a new
    value for it supersedes the old one."""
    m = _SLOT_RE.match(text.strip())
    if not m:
        return None
    return _slot(m.group(1))


def forget_request(text: str) -> str | None:
    """"Forget that my wife's birthday is May 3" -> what to forget."""
    m = re.match(r"(?i)^(?:please\s+)?forget\s+(?:that|about|what I said about)\s+(.{3,300}?)[.!]?$",
                 normalize(text))
    return m.group(1) if m else None


def extract(text: str, limit: int = 4) -> list[dict]:
    """Candidates from one message a person wrote, as ``{kind, text, slot,
    confidence, signal}`` in the person's third person. Questions are
    skipped; at most ``limit`` per message."""
    out: list[dict] = []
    seen: set[str] = set()
    text = normalize(text)
    if not text or len(text) > 4000 or text.startswith(("/", "[tool", "<")):
        return out
    text = re.sub(r"(?i)^@[\w-]+[,:]?\s+", "", text)
    for sentence in _SENTENCE.split(text):
        s = sentence.strip().rstrip(".! ")
        if len(s) < 6 or s.endswith("?") or forget_request(s):
            continue
        for pat, kind, slot, conf, signal in _EXTRACT:
            m = pat.match(s)
            if not m:
                continue
            body = s if signal != "explicit" or kind else m.group(1)
            if kind is None:   # "remember that ...": about the person when it says I/my
                kind = "profile" if re.search(r"(?i)\b(I|my|me|I'm|mine)\b", body) else "fact"
            if signal == "explicit" and kind == "profile" and slot == "name":
                body = f"The user's name is {m.group(1).strip()}"
            if signal == "correction":
                body = "The user asked: " + body[:1].lower() + body[1:]
            fact = third_person(body.rstrip(".! ")) + "."
            if isinstance(slot, int):
                slot = _slot(m.group(slot))
            elif callable(slot):
                slot = slot(m)
            slot = slot or slot_of(fact)
            key = fact.lower()
            if key in seen:
                break
            seen.add(key)
            out.append({"kind": kind, "text": fact, "slot": slot, "confidence": conf,
                        "signal": signal})
            break
        if len(out) >= limit:
            break
    return out
