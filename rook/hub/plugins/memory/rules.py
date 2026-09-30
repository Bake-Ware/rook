"""Write rules for agent memory, as code.

What to save, when, and what never to save (docs/design/memory.md section 4).
Every candidate passes :func:`screen` before it is stored:

* **Never saved** (rejected): empty text; secrets that the caller refused to
  have masked (vault values, key/token shapes); prompt-injection or
  exfiltration payloads (memory is read back into agent prompts); content
  that is mostly code, diffs or logs (derivable from the repository).
* **Held for review** (confidence lowered below the commit threshold):
  transient state (in progress, right now, temp paths, PIDs, TODO lists) and
  anything that points at code locations or commits rather than stating a
  durable fact.
* **Boosted**: corrections and explicit confirmations ("don't", "always",
  "remember", "the user corrected ..."), which are what memory is for.

The rules are deterministic (regular expressions and counts) so tests and
behaviour are reproducible without a model. Operators tune the thresholds
through settings; the lists themselves change with the code.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field

KINDS = ("profile", "preference", "fact", "episode", "procedure")
SCOPE_KINDS = ("user", "band", "agent")
#: Where a kind lives when the caller names no scope.
DEFAULT_SCOPE = {"profile": "user", "preference": "user", "episode": "user",
                 "fact": "band", "procedure": "band"}
#: Starting confidence by origin, before rule adjustments.
BASE_CONFIDENCE = {"agent": 0.7, "transcript": 0.5, "vault": 0.8, "summary": 0.75}

# Prompt injection / exfiltration: memory is read back into prompts (the
# digest, recall results), so these are refused outright.
_THREATS = [
    (r"ignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|rules|messages)", "prompt_injection"),
    (r"disregard\s+(your|all|any|the)\s+(instructions|rules|guidelines|system prompt)", "prompt_injection"),
    (r"\byou\s+are\s+now\s+(a|an|the|in)\b", "role_hijack"),
    (r"system\s+prompt\s+override", "prompt_injection"),
    (r"do\s+not\s+(tell|inform|show)\s+the\s+(user|operator|human)", "deception"),
    (r"\b(curl|wget)\b[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|PASS)\w*", "exfiltration"),
    (r"\bcat\s+[^\n]*(\.env\b|\.netrc|\.pgpass|id_rsa|id_ed25519|credentials)", "exfiltration"),
    (r"authorized_keys", "persistence"),
]
_INVISIBLE = ("​", "‌", "‍", "⁠", "﻿",
              "‪", "‫", "‬", "‭", "‮")

# Secret shapes. Matches are masked (or the write is refused, per setting).
SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----|$)"),
    re.compile(r"\b(sk|rk|pk)-(ant-|proj-|live-|test-)?[A-Za-z0-9_-]{20,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|token|psk)\b\s*[:=]\s*[\"']?(?!\*\*\*)[^\s\"',;]{6,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{16,}=*"),
]
MASK = "***"

_TRANSIENT = [
    (r"\b(right now|currently|at the moment|for now|just now|in progress|this session|today|tonight)\b", "transient"),
    (r"\b(todo|wip|next step|next steps)\b\s*[:\-]", "task_state"),
    (r"(^|\s)/tmp/\S+", "temp_path"),
    (r"\bpid\s*[:=]?\s*\d{2,}\b", "pid"),
    (r"\b(call[_ ]id|request[_ ]id)\b", "call_state"),
]
_DERIVABLE = [
    (r"\b[0-9a-f]{12,40}\b", "commit_ref"),
    (r"\b[\w./-]+\.(py|js|ts|go|rs|c|h|java|rb|sh|sql|toml|yaml|yml|json):\d+", "code_location"),
]
_CORRECTION = re.compile(
    r"(?i)(^|\b)(don't|do not|never|always|stop|instead|prefer|remember|correct(ed|ion)|"
    r"not like that|that's wrong|actually,)\b")
_CODE_LINE = re.compile(r"^\s*([+-]{1}\s|@@|def |class |import |from \S+ import|\$ |>>> |[{}();]\s*$|"
                        r"(if|for|while|return|const|let|var|function)\b.*[;:{]\s*$)")


@dataclass
class Verdict:
    text: str
    confidence: float
    reject: str | None = None          # reason, when the write is refused
    warnings: list = field(default_factory=list)
    masked: int = 0                     # secrets masked
    signals: list = field(default_factory=list)  # e.g. "correction"

    def to_dict(self) -> dict:
        out = {"confidence": round(self.confidence, 3)}
        if self.reject:
            out["rejected"] = self.reject
        if self.warnings:
            out["warnings"] = list(self.warnings)
        if self.masked:
            out["masked"] = self.masked
        if self.signals:
            out["signals"] = list(self.signals)
        return out


def normalize(text: str) -> str:
    """Whitespace-collapsed NFC text, the stored form."""
    text = unicodedata.normalize("NFC", str(text or ""))
    return " ".join(text.split())


def content_hash(text: str) -> str:
    key = re.sub(r"[^\w]+", " ", normalize(text).lower()).strip()
    return hashlib.sha256(key.encode()).hexdigest()


def tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 2}


def jaccard(a: str, b: str) -> float:
    x, y = tokens(a), tokens(b)
    if not x or not y:
        return 0.0
    return len(x & y) / len(x | y)


def mask_secrets(text: str, vault_values=()) -> tuple[str, int]:
    """Mask vault secret values and secret-shaped strings. Returns the masked
    text and how many were masked."""
    n = 0
    for v in sorted((v for v in vault_values if v and len(v) >= 4), key=len, reverse=True):
        if v in text:
            n += text.count(v)
            text = text.replace(v, MASK)
    for pat in SECRET_PATTERNS:
        text, k = pat.subn(lambda m: _mask_match(m), text)
        n += k
    return text, n


def _mask_match(m: re.Match) -> str:
    s = m.group(0)
    key = re.match(r"(?i)(password|passwd|pwd|secret|api[_-]?key|token|psk)\s*[:=]\s*[\"']?", s)
    if key:
        return key.group(0) + MASK
    if s.lower().startswith("bearer"):
        return "Bearer " + MASK
    return MASK


def threat(text: str) -> str | None:
    for ch in _INVISIBLE:
        if ch in text:
            return f"invisible unicode U+{ord(ch):04X}"
    for pat, name in _THREATS:
        if re.search(pat, text, re.IGNORECASE):
            return name
    return None


def mostly_code(raw: str) -> bool:
    """Code blocks, diffs, stack traces or logs: derivable from the repo."""
    if "```" in raw:
        return True
    lines = [ln for ln in str(raw).splitlines() if ln.strip()]
    if len(lines) < 3:
        return False
    code = sum(1 for ln in lines if _CODE_LINE.match(ln))
    return code / len(lines) >= 0.5


def screen(raw: str, kind: str, *, origin: str = "agent", confidence: float | None = None,
           confirmed: bool = False, secrets: str = "reject", vault_values=(),
           max_chars: int = 600) -> Verdict:
    """Apply the write rules to one candidate. ``secrets`` is ``reject`` (a
    secret refuses the write so the agent rephrases) or ``mask`` (ingest:
    transcripts inevitably quote credentials)."""
    if kind not in KINDS:
        return Verdict("", 0.0, reject=f"kind must be one of {', '.join(KINDS)}")
    text = normalize(raw)
    if not text:
        return Verdict("", 0.0, reject="empty")
    bad = threat(text)
    if bad:
        return Verdict(text, 0.0, reject=f"unsafe content ({bad}): memory is read back into prompts")
    if kind != "episode" and mostly_code(raw):
        return Verdict(text, 0.0, reject="code, diffs or logs are derivable from the repository; "
                                        "save the durable lesson in a sentence instead")
    masked_text, n = mask_secrets(text, vault_values)
    if n and secrets == "reject":
        return Verdict(masked_text, 0.0, masked=n,
                       reject="contains a secret; never store credentials (reference the vault "
                              "entry by name instead)")
    text = masked_text
    warnings: list[str] = []
    signals: list[str] = []
    if len(text) > max_chars:
        cut = text[:max_chars]
        text = cut[:cut.rfind(" ")].rstrip(" ,;:") + " …" if " " in cut else cut
        warnings.append("truncated")
    if confidence is None:
        conf = BASE_CONFIDENCE.get(origin, 0.6)
    else:
        try:
            conf = float(confidence)
        except (TypeError, ValueError):
            conf = BASE_CONFIDENCE.get(origin, 0.6)
    if confirmed:
        conf = 1.0
    elif kind != "episode":
        found = {name for pat, name in _TRANSIENT if re.search(pat, text, re.IGNORECASE)}
        if found:
            warnings += sorted(found)
            conf -= 0.3
        found = {name for pat, name in _DERIVABLE if re.search(pat, text)}
        if found:
            warnings += sorted(found)
            conf -= 0.2
        if kind in ("preference", "profile", "procedure", "fact") and _CORRECTION.search(text):
            conf += 0.15
            signals.append("correction")
    return Verdict(text, max(0.0, min(1.0, conf)), warnings=warnings, masked=n, signals=signals)
