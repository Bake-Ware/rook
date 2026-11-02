"""0.4 MCP keys -> 0.5 door tokens, so every client keeps working when the
names (rook.bakeforge.com, mcp.bakeforge.com) point at the 0.5 hub.

Bake (voice, Nov 1 2026): "can we just migrate the existing... MCP keys...
the tokens, to the new system? So they just pick up when DNS switches."

0.4 kept two kinds of MCP bearer, both checked by ``rook.band_mcp.tokens``:

- **API keys** minted on the Tokens page: ``oauth.json``'s ``api_tokens``
  (the key itself, its name, an ``agent_id``). ``access.db``'s ``tokens``
  table says whose each key is (a key with no row went to the first owner).
- **the static token** (``ROOK_MCP_STATIC_TOKEN``), kept in 0.4's vault as
  ``rook-mcp-static-token``; it acted for the owner.

0.4's claude.ai connector (its OAuth shim) used one of these as client
secret, access token and refresh token, so carrying the keys carries the
connectors too.

Each becomes a ``<data>/tokens.json`` entry the 0.5 hub already reads: the
key's sha256 -> ``{person, agent, label}``. The person is the key's owner;
the agent is the key's name made an agent name ("Claude web" ->
``claude-web``), so the journal still names the client. An agent not in
hub.yaml gets every grant (``*``) beside its token, as ``token.create``
gives a person-minted one; acting for its person it gets only what that
person may (castling-protocol, "Acting on behalf = intersection"). The key
itself is never written anywhere, only its hash. An expired key is left out.
Entries already in ``tokens.json`` are kept as they are.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path

STATIC = "rook-mcp-static-token"           # 0.4's static MCP token, in its vault
STATIC_AGENT = "static"                     # what 0.4's journal called it (agent:static)
AGENT = re.compile(r"[a-z][a-z0-9_-]{0,39}")   # rook/core2/hub/doors/web/tokens.py
GRANTS = {"grants": ["*"]}


def agent_name(label: str, taken: set) -> str:
    """A 0.4 key's name as a 0.5 agent name, unique among ``taken``."""
    base = re.sub(r"[^a-z0-9_-]+", "-", (label or "").casefold()).strip("-_")
    if not base or not base[0].isalpha():
        base = "key-" + base if base else "key"
    base = base[:36].rstrip("-_")
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    taken.add(name)
    return name


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()   # rook.core2.hub.identity.digest


def keys(root: Path, people: list[dict], mapping: list[dict], secrets: list[dict],
         agents: dict | None = None, now: float | None = None) -> tuple[list[dict], list[str]]:
    """``([{digest, person, agent, label, created, rights?}], [notes])`` from a
    0.4 copy: ``people`` as :meth:`Source.people`, ``mapping`` the people
    step's 0.4 id -> 0.5 name, ``secrets`` the copy's vault, ``agents`` the
    0.5 hub.yaml's. Nothing here holds a key's value past the hash."""
    now = time.time() if now is None else now
    agents = agents or {}
    names = {m["from"]: m["name"] for m in mapping}
    owner = next((m["name"] for m in mapping if m.get("preset") == "owner"), None)
    whose = {t["agent_id"]: names.get(p["id"]) for p in people for t in p.get("tokens") or ()}
    out, notes, taken = [], [], {STATIC_AGENT}
    path = _find(root, "oauth.json")
    stored = []
    if path is not None:
        try:
            stored = json.loads(path.read_text(encoding="utf-8")).get("api_tokens") or []
        except (OSError, ValueError, AttributeError) as e:
            notes.append(f"{path.name}: can't read the 0.4 API keys ({e}); none carried over")
    for t in stored:
        if not isinstance(t, dict) or not isinstance(t.get("token"), str) or not t["token"]:
            continue
        label = str(t.get("name") or t.get("id") or "key")
        exp = t.get("expires_at")
        if exp is not None and exp <= now:
            notes.append(f"0.4 API key {label} ({t.get('id')}) had expired: not carried over")
            continue
        person = whose.get(t.get("agent_id")) or owner
        if person is None:
            notes.append(f"0.4 API key {label} ({t.get('id')}): no owner to act for, not carried over")
            continue
        out.append(_entry(t["token"], person, agent_name(label, taken), f"{label} (0.4 key {t.get('id')})",
                          t.get("created_at"), agents))
    static = next((s["value"] for s in secrets if s["name"] == STATIC and s.get("value")), None)
    if static and owner:
        out.append(_entry(static, owner, STATIC_AGENT, "0.4 static token", None, agents))
    elif not static:
        notes.append(f"no {STATIC} in the 0.4 vault: 0.4's static MCP token isn't carried over")
    return _dedupe(out, notes), notes


def put(data: Path, entries: list[dict]) -> dict:
    """Add ``entries`` to ``<data>/tokens.json`` (only hashes); an entry
    already there is kept as it is. Returns ``{written, kept}``."""
    from rook.core2.hub.runtime import _write_private
    path = Path(data) / "tokens.json"
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    written = 0
    for e in entries:
        if e["digest"] in stored:
            continue
        stored[e["digest"]] = {k: v for k, v in e.items() if k != "digest"}
        written += 1
    if written:
        _write_private(path, json.dumps(stored, indent=1))
    return {"written": written, "kept": len(entries) - written}


def carried(data: Path, entries: list[dict]) -> list[str]:
    """The labels of ``entries`` not in ``<data>/tokens.json`` (for --verify)."""
    path = Path(data) / "tokens.json"
    stored = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return [e["label"] for e in entries if e["digest"] not in stored]


def _entry(token: str, person: str, agent: str, label: str, created, agents: dict) -> dict:
    assert AGENT.fullmatch(agent), agent
    return {"digest": digest(token), "person": f"person:{person}", "agent": f"agent:{agent}", "label": label,
            "created": int(created or time.time()), "by": "migrate:0.4",
            **({} if agent in agents else {"rights": dict(GRANTS)})}


def _dedupe(entries: list[dict], notes: list[str]) -> list[dict]:
    seen, out = set(), []
    for e in entries:
        if e["digest"] in seen:
            notes.append(f"{e['label']}: the same key as one above; carried once")
            continue
        seen.add(e["digest"])
        out.append(e)
    return out


def _find(root: Path, name: str) -> Path | None:
    hits = sorted(Path(root).rglob(name), key=lambda p: (len(p.parts), str(p)))
    return hits[0] if hits else None
