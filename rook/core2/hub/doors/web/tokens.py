"""Door tokens for agents: the ``token`` builtin (token.create, token.list,
token.revoke) behind the Tokens page.

Bake (castling-switchover-decisions, "Claude connects itself"): "an agent
can generate a token for the MCP". A token always acts for the person the
caller calls for, so it never gets more than that person may (spec 8.3:
each call gets only what both the person and the agent may). A new agent
name gets the minting agent's rights (every grant, ``*``, when a person
mints it on the dashboard), which the person's rights then narrow; it is kept beside the token in ``<data>/tokens.json`` (only
hashes are kept there) and loaded with it at start.

Move-in: no approval step (castling-switchover-decisions, "Usable first").
TODO(castling:security): minting for an existing agent with wider rights than
the minter; who may mint for whom, once security comes back.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from rook.kit.ctx import KitError

from rook.core2.hub.identity import Caller

AGENT = re.compile(r"[a-z][a-z0-9_-]{0,39}")
NEW_AGENT_RIGHTS = {"grants": ["*"]}
ID_LEN = 12


class Tokens:
    def __init__(self, hub):
        self.hub = hub

    @property
    def _path(self) -> Path:
        return Path(self.hub.data) / "tokens.json"

    def _stored(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}

    def mcp_url(self) -> str | None:
        """Where agents reach the MCP door from outside: its OAuth origin."""
        mcp = (self.hub.cfg.doors or {}).get("mcp") or {}
        origin = mcp.get("oauth")
        return f"{origin.rstrip('/')}/mcp" if isinstance(origin, str) and origin else None

    async def create(self, ctx, agent: str, label: str | None = None) -> dict:
        person = ctx.caller.person
        if not person:
            raise KitError("a token acts for a person: call for one (an agent alone can't mint)")
        if not AGENT.fullmatch(agent or ""):
            raise KitError(f"bad agent name {agent!r}")
        ids = self.hub.identities
        if agent in ids.run_by:
            raise KitError(f"agent {agent!r} is run by {ids.run_by[agent]}: it gets turn tokens, not door tokens")
        rights = None
        if agent not in ids.agents:
            # A new agent gets the minting agent's own rights (never more), or every grant when a
            # person mints from the dashboard; the person's rights narrow it either way.
            minter = (ctx.caller.agent or "").partition(":")
            rights = copy.deepcopy(ids.agents.get(minter[2])) if minter[0] == "agent" else None
            rights = rights or dict(NEW_AGENT_RIGHTS)
            ids.add_agent(agent, rights)
        from rook.core2.hub.runtime import add_token
        caller = Caller(person, f"agent:{agent}")
        token = add_token(self.hub.data, caller, label or "", ids, rights=rights,
                          by=ctx.caller.agent or person)
        self.hub._load_tokens()          # pick it up now (tokens.json changed)
        url = self.mcp_url()
        out = {"token": token, "id": _id_of_token(token), "person": person.partition(":")[2],
               "agent": agent, "new_agent": rights is not None, "mcp_url": url}
        if url:
            out["claude_code"] = (f"claude mcp add --transport http rook {url} "
                                  f"--header \"Authorization: Bearer {token}\"")
            # claude.ai: custom connector at url; Advanced settings: this client id, the token as client secret.
            out["connector"] = {"url": url, "client_id": f"rook-{out['id']}", "client_secret": token}
        return out

    async def list(self, ctx) -> dict:
        person = ctx.caller.person
        rows = []
        for d, rec in self._stored().items():
            if not ctx.caller.owner and rec.get("person") != person:
                continue
            rows.append({"id": d[:ID_LEN], "person": (rec.get("person") or "").partition(":")[2] or None,
                         "agent": (rec.get("agent") or "").partition(":")[2] or None,
                         "label": rec.get("label") or "", "created": rec.get("created"),
                         "by": rec.get("by")})
        rows.sort(key=lambda r: r.get("created") or 0, reverse=True)
        return {"tokens": rows, "mcp_url": self.mcp_url()}

    async def revoke(self, ctx, id: str) -> dict:
        stored = self._stored()
        hits = [d for d in stored if d.startswith(id)]
        if len(hits) != 1:
            raise KitError(f"no token {id}")
        rec = stored[hits[0]]
        if not ctx.caller.owner and rec.get("person") != ctx.caller.person:
            raise KitError(f"no token {id}")
        if rec.get("label") == "first owner" and rec.get("person") and not rec.get("agent"):
            # Never the way an owner gets back in (castling-switchover-decisions: nothing may lock Bake out).
            raise KitError("that is the first owner's token; it can't be revoked here")
        del stored[hits[0]]
        from rook.core2.hub.runtime import _write_private
        _write_private(self._path, json.dumps(stored, indent=1))
        self.hub._load_tokens()          # forgets it now
        return {"revoked": id}


def _id_of_token(token: str) -> str:
    from rook.core2.hub.identity import digest
    return digest(token)[:ID_LEN]
