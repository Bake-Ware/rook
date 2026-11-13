"""Scrubbing the journal on write (L4) and redacting it afterwards (L5).

Every journal entry's text (its why, and every string in its result or event
payload) passes through `scrub` before it is stored:

- a vault value becomes `(secret:<name>)`. The text is scanned with the vault's
  in-memory index of 6-character pieces (vault.py), so nothing is decrypted;
- text with a known secret shape becomes `(secret shape:<shape>)`.

L4 calls this a reducer, not a guarantee: it misses vault values shorter than
eight characters, values that arrive changed (encoded, escaped, split across
strings, or in another case), secrets of shapes not listed below, and anything
in a dictionary key. `journal.redact` (L5) takes a value out of every entry
already written, and from stream recordings.
"""

import json
import re

from .errors import HubError

# Known secret shapes, one named pattern each, kept short and well known. Each also
# names the text it cannot match without, so its pattern runs only when that is there.
SHAPES = {
    "private-key": (("PRIVATE KEY-----",),
                    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
    "jwt": (("eyJ",), r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "github-token": (("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"),
                     r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    "aws-access-key-id": (("AKIA", "ASIA"), r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": (("xox",), r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "anthropic-key": (("sk-ant-",), r"\bsk-ant-[A-Za-z0-9_-]{20,}"),     # before openai-key: both start sk-
    "openai-key": (("sk-",), r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "stripe-key": (("_live_",), r"\b[sr]k_live_[A-Za-z0-9]{20,}"),
    "google-api-key": (("AIza",), r"\bAIza[0-9A-Za-z_-]{35}"),
}
_SHAPES = [(name, hints, re.compile(pattern, re.S)) for name, (hints, pattern) in SHAPES.items()]

REDACTED = "(redacted)"


def _vault_values(text, vault):
    """Replace vault values in one string. Looks up the 6-character piece at every
    3rd position in the vault's index and confirms each hit by comparing the text."""
    index = vault.index
    if vault.state != "open" or not index:
        return text
    from .vault import GRAM, STRIDE
    hits = [i for i in range(0, len(text) - GRAM + 1, STRIDE) if text[i:i + GRAM] in index]
    found = set()
    for i in hits:
        for offset, by_length in index[text[i:i + GRAM]].items():
            start = i - offset
            if start < 0:
                continue
            for length, values in by_length.items():
                name = values.get(text[start:start + length])
                if name is not None:
                    found.add((start, -length, name))
    out, position = [], 0
    for start, minus_length, name in sorted(found):     # leftmost first, and the longest there
        if start >= position:
            out += [text[position:start], f"(secret:{name})"]
            position = start - minus_length
    out.append(text[position:])
    return "".join(out)


def scrub_text(text, vault):
    text = _vault_values(text, vault)
    for name, hints, pattern in _SHAPES:
        if any(hint in text for hint in hints):
            text = pattern.sub(f"(secret shape:{name})", text)
    return text


def scrub(value, vault):
    """`value` with every string in it scrubbed (L4). Keys are left as they are."""
    if isinstance(value, str):
        return scrub_text(value, vault)
    if isinstance(value, dict):
        return {k: scrub(v, vault) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, vault) for v in value]
    return value


def _replace(value, old):
    if isinstance(value, str):
        return value.replace(old, REDACTED)
    if isinstance(value, dict):
        return {k: _replace(v, old) for k, v in value.items()}
    if isinstance(value, list):
        return [_replace(v, old) for v in value]
    return value


def redact(hub, args, actor):
    """The `journal.redact` hub cap (L5): replace a value everywhere in the journal and
    in stream recordings. Answers how many journal entries and how many stream
    recordings changed. The value itself is never journaled."""
    value = args.get("value")
    if not isinstance(value, str) or not value:
        raise HubError("bad_message", "args.value is required and must be a string.",
                       "Give the exact text to take out of the journal.")
    escaped = json.dumps(value)[1:-1]
    changed = 0
    with hub._lock, hub._db:
        hub.auth.actor(**actor)
        for row in hub._db.execute(
                "SELECT id, why, result FROM journal WHERE instr(coalesce(why, ''), ?) OR instr(result, ?) OR instr(result, ?)",
                (value, value, escaped)).fetchall():
            why = row["why"].replace(value, REDACTED) if row["why"] else row["why"]
            result = json.dumps(_replace(json.loads(row["result"]), value))
            if why != row["why"] or result != row["result"]:
                hub._db.execute("UPDATE journal SET why = ?, result = ? WHERE id = ?", (why, result, row["id"]))
                changed += 1
        raw = value.encode("utf-8")
        streams = set()
        for row in hub._db.execute("SELECT stream, position, data FROM stream_data WHERE instr(data, ?)",
                                   (raw,)).fetchall():
            hub._db.execute("UPDATE stream_data SET data = ? WHERE stream = ? AND position = ?",
                            (row["data"].replace(raw, REDACTED.encode()), row["stream"], row["position"]))
            streams.add(row["stream"])
    return {"redacted": changed, "recordings": len(streams)}


def caps():
    return {"journal.redact": (redact, {
        "description": "Take a value out of the whole journal and every stream recording: wherever it is, "
                       "(redacted) takes its place. Answers how many journal entries and how many stream "
                       "recordings changed.",
        "schema": {"type": "object", "required": ["value"], "additionalProperties": False,
                   "properties": {"value": {"type": "string", "description": "The exact text to remove."}}},
    })}
