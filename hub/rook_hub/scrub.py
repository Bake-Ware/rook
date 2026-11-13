"""Scrubbing the journal on write (L4) and redacting it afterwards (L5).

Every journal entry's text (its why, and every string in its result or event
payload) passes through `scrub` before it is stored:

- a vault value becomes `(secret:<name>)`. The text is scanned with the vault's
  index of keyed hashes (vault.py), so nothing is decrypted;
- text with a known secret shape becomes `(secret shape:<shape>)`.

L4 calls this a reducer, not a guarantee: it misses vault values shorter than
eight characters, values that arrive changed (encoded, escaped, split across
strings, or in another case), secrets of shapes not listed below, and anything
in a dictionary key. `journal.redact` (L5) takes a value out of every entry
already written.
"""

import json
import re

from .errors import HubError

# Known secret shapes, one named pattern each. Kept short and well known.
SHAPES = {
    "private-key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----", re.S),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    "github-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    "aws-access-key-id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack-token": re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
    "anthropic-key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    "openai-key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{32,}"),
    "stripe-key": re.compile(r"\b[sr]k_live_[A-Za-z0-9]{20,}"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
}

REDACTED = "(redacted)"


def _vault_values(text, vault):
    """Replace vault values in one string, using the vault's keyed-hash index."""
    if vault.state != "open" or not vault.index:
        return text
    from .vault import HEAD
    out, i, last = [], 0, len(text) - HEAD
    while i <= last:
        found = vault.index.get(vault.keyed_hash(text[i:i + HEAD]))
        if found:
            for length, full, name in sorted(found, reverse=True):     # longest first
                if vault.keyed_hash(text[i:i + length]) == full:
                    out.append(f"(secret:{name})")
                    i += length
                    break
            else:
                found = None
        if not found:
            out.append(text[i])
            i += 1
    out.append(text[i:])
    return "".join(out)


def scrub_text(text, vault):
    text = _vault_values(text, vault)
    for shape, pattern in SHAPES.items():
        text = pattern.sub(f"(secret shape:{shape})", text)
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
    """The `journal.redact` hub cap (L5): replace a value everywhere in the journal.
    Answers how many entries changed. The value itself is never journaled."""
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
    return {"redacted": changed, "note": "Stream recordings are not kept yet, so there were none to redact."}


def caps():
    return {"journal.redact": (redact, {
        "description": "Take a value out of the whole journal: every entry that holds it gets (redacted) "
                       "in its place. Answers how many entries changed.",
        "schema": {"type": "object", "required": ["value"], "additionalProperties": False,
                   "properties": {"value": {"type": "string", "description": "The exact text to remove."}}},
    })}
